"""Scoped, idempotent rewrites; publication is separate from candidate generation."""
import copy
import hashlib
import json
import re

from acceleration_catalog import targets
from dashboard_columns import panel_index, source_identity

PROJECTS = ('a3-monitoring', 'dcu-monitoring', 'xpu-monitoring')
RESULTS = (('client_cancelled', '客户端取消'), ('client_disconnected', '客户端断开'), ('unknown', '未知结果'))
BATCHES = ('generation-results', 'gateway-latency', 'gateway-tokens', 'a3-histograms')
PAIR = re.compile(r'\((?P<a>[\w:]+_bucket\{[^{}]+\}) > ignoring\(le\) (?P<b>[\w:]+_bucket\{[^{}]+\})\) or '
                  r'\(rate\((?P=a)\[1m\]\) > ignoring\(le\) rate\((?P=b)\[1m\]\)\)')
LE = re.compile(r'\ble="([^"\\]+)"')
MAPPED = re.compile(r'\((?P<a>[\w:]+_bucket\{[^{}]+\}) > label_map\((?P<b>[\w:]+_bucket\{[^{}]+\}), "le", (?P<m>(?:"[^"\\]+", )*"[^"\\]+")\)\) or '
                    r'\(rate\((?P=a)\[1m\]\) > label_map\(rate\((?P=b)\[1m\]\), "le", (?P=m)\)\)')


def batch_targets(batch):
    if batch not in BATCHES:
        raise ValueError('Unknown query batch: ' + batch)
    if batch == 'a3-histograms':
        return {(p, d, k) for p, d, k, g in targets() if g == 'a3'}
    keys = {'generation-results': ('generation-0',), 'gateway-latency': ('extra-first', 'extra-duration'),
            'gateway-tokens': ('extra-input', 'extra-output')}[batch]
    return {(p, 'gateway-requests', k) for p in PROJECTS for k in keys}


def kind_for(identity):
    identity = source_identity(*identity)
    for batch in BATCHES:
        if tuple(identity) in batch_targets(batch):
            return 'generation-results' if batch == 'generation-results' else 'histogram-monotonic'
    raise ValueError('Unknown query rewrite target: ' + repr(identity))


def plugin(query):
    if query['spec']['plugin']['kind'] != 'PrometheusTimeSeriesQuery':
        raise ValueError('Unexpected query plugin')
    return query['spec']['plugin']['spec']


def generation_expression(expression):
    values = set(re.findall(r'\bresult="([^"]+)"', expression))
    if values != {'client_cancelled'} or 'perses_order' in expression or 'perses_series' in expression:
        raise ValueError('Unexpected generation result selector')
    for text in ('sum by(environment)', 'count by(environment)', 'unless on(environment)'):
        if expression.count(text) != 1:
            raise ValueError('Unexpected generation aggregation: ' + text)
    expression = expression.replace('result="client_cancelled"', 'result=~"' + '|'.join(r for r, _ in RESULTS) + '"')
    for op in ('sum by', 'count by', 'unless on'):
        expression = expression.replace(op + '(environment)', op + '(environment,result)')
    for i, (result, label) in enumerate(RESULTS, 1):
        expression = ('label_replace(label_replace((' + expression + '), "perses_series", '
                      + json.dumps(label, ensure_ascii=False) + ', "result", "' + result
                      + '"), "perses_order", "%02d", "result", "%s")' % (i, result))
    return 'sort_by_label(label_del((' + expression + '), "result"), "perses_order")'


def generation(panel):
    result = copy.deepcopy(panel)
    queries = result['spec']['queries']
    if len(queries) not in (2, 4):
        raise ValueError('Expected four original or two rewritten generation queries')
    total = plugin(queries[0])
    # The total is the exact same guarded template with no result matcher.
    total_expr = total['query']
    if 'result=' in total_expr or 'aigate_generation_requests_ended_total{' not in total_expr:
        raise ValueError('Unexpected generation total')
    original = re.sub(r'(aigate_generation_requests_ended_total\{[^{}]*?)(?=,request_scope=|\})',
                      r'\1,result="client_cancelled"', total_expr)
    expected = generation_expression(original)
    settings = result['spec']['plugin']['spec'].get('querySettings', [])
    if any(s.get('queryIndex') != 0 for s in settings):
        raise ValueError('Per-result rendering override requires explicit migration')
    if total.get('seriesNameFormat') != '全部结束':
        raise ValueError('Unexpected total legend')
    options = lambda q: {k: v for k, v in plugin(q).items() if k not in ('query', 'seriesNameFormat')}
    if any(options(q) != options(queries[0]) for q in queries[1:]):
        raise ValueError('Generation query options differ')
    if len(queries) == 2:
        if plugin(queries[1])['query'] != expected or plugin(queries[1]).get('seriesNameFormat') != '{{perses_series}}':
            raise ValueError('Unknown rewritten generation template')
        return result
    for query, (value, label) in zip(queries[1:], RESULTS):
        spec = plugin(query)
        if (options(query) != options(queries[0]) or spec.get('seriesNameFormat') != label
                or spec['query'] != original.replace('result="client_cancelled"', 'result="' + value + '"')):
            raise ValueError('Generation queries are not identical guarded templates')
    plugin(queries[1]).update(query=expected, seriesNameFormat='{{perses_series}}')
    result['spec']['queries'] = queries[:2]
    return result


def mapped_block(template, bounds):
    # Exact whitelist avoids collisions from unmapped smallest/unknown buckets.
    select = lambda values: template.replace('le=BOUND', 'le=~' + json.dumps('|'.join(re.escape(v) for v in values)))
    left, right = select(bounds[:-1]), select(bounds[1:])
    mapping = ', '.join(json.dumps(v) for pair in zip(bounds[1:], bounds[:-1]) for v in pair)
    return (f'({left} > label_map({right}, "le", {mapping})) or '
            f'(rate({left}[1m]) > label_map(rate({right}[1m]), "le", {mapping}))')


def histogram(expression):
    pairs = list(PAIR.finditer(expression))
    if not pairs:
        mapped = list(MAPPED.finditer(expression))
        if len(mapped) != 1 or '> ignoring(le)' in expression:
            raise ValueError('Unknown histogram comparison template')
        match = mapped[0]
        values = json.loads('[' + match['m'] + ']')
        bounds = [values[1]] + values[::2]
        template = re.sub(r'\ble=~"(?:\\.|[^"\\])*"', 'le=BOUND', match['a'])
        if mapped_block(template, bounds) != match[0]:
            raise ValueError('Unknown mapped bucket sequence')
        validate_bounds(bounds)
        return expression
    if 'label_map(' in expression or expression.count('> ignoring(le)') != len(pairs) * 2:
        raise ValueError('Mixed or unsupported histogram comparisons')
    bounds, template = [], None
    for i, pair in enumerate(pairs):
        if i and expression[pairs[i-1].end():pair.start()] != ' or ':
            raise ValueError('Non-contiguous histogram comparisons')
        a, b = [LE.search(pair[key]) for key in ('a', 'b')]
        if not a or not b:
            raise ValueError('Missing literal bucket boundary')
        ta, tb = [LE.sub('le=BOUND', pair[key]) for key in ('a', 'b')]
        if ta != tb or (template is not None and template != ta):
            raise ValueError('Bucket selectors differ beyond le')
        template = ta
        if not bounds:
            bounds.append(a[1])
        if bounds[-1] != a[1]:
            raise ValueError('Non-adjacent bucket chain')
        bounds.append(b[1])
    validate_bounds(bounds)
    return expression[:pairs[0].start()] + mapped_block(template, bounds) + expression[pairs[-1].end():]


def validate_bounds(bounds):
    numbers = [float(x) for x in bounds]
    if len(bounds) < 2 or bounds[-1] != '+Inf' or any(not a < b for a, b in zip(numbers, numbers[1:])):
        raise ValueError('Invalid histogram boundaries')


def rewrite(panel, kind):
    if kind == 'generation-results':
        return generation(panel)
    if kind != 'histogram-monotonic' or len(panel['spec']['queries']) != 1:
        raise ValueError('Unexpected rewrite kind or histogram query count')
    result = copy.deepcopy(panel)
    spec = plugin(result['spec']['queries'][0])
    spec['query'] = histogram(spec['query'])
    return result


def apply(resources, entries):
    result = copy.deepcopy(resources)
    selected = {}
    for entry in entries:
        identity = source_identity(*entry[:3]) if len(entry) == 4 else tuple(entry[:3])
        if len(entry) != 4 or kind_for(identity) != entry[3] or identity in selected:
            raise ValueError('Invalid or duplicate rewrite entry')
        selected[identity] = entry[3]
    changes = []
    for d in result['dashboards']:
        for key, before in list(d['spec']['panels'].items()):
            identity = source_identity(d['metadata']['project'], d['metadata']['name'], key)
            if identity not in selected:
                continue
            after = rewrite(before, selected[identity])
            if after != before:
                d['spec']['panels'][key] = after
                changes.append(dict(project=identity[0], dashboard=d['metadata']['name'], panel=key,
                                    kind=selected[identity], before=before, after=after))
    return result, changes


def prepare(resources, batch):
    targets_ = batch_targets(batch)
    present = panel_index(resources).keys()
    if not targets_ <= present:
        raise ValueError('Missing batch panels: ' + repr(targets_ - present))
    return apply(resources, [list(t) + [kind_for(t)] for t in sorted(targets_)])


def replace_catalog(previous, resources):
    """Replace only the eight A3 expressions; keep every unrelated entry byte-equivalent."""
    candidate, changes = prepare(resources, 'a3-histograms')
    by_id = {'/'.join(source_identity(c['project'], c['dashboard'], c['panel'])): c for c in changes}
    if len(by_id) != 8:
        raise ValueError('Expected eight original A3 histogram panels')
    result = copy.deepcopy(previous)
    for entry in result['panels']:
        if entry['id'] not in by_id:
            continue
        change = by_id.pop(entry['id'])
        if entry['expression'] != plugin(change['before']['spec']['queries'][0])['query']:
            raise ValueError('Live expression differs from previous catalog')
        entry['expression'] = plugin(change['after']['spec']['queries'][0])['query']
        entry.pop('revision')
        entry['revision'] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    if by_id:
        raise ValueError('A3 panel missing from catalog')
    validate_revision_catalog(previous, result, 'a3')
    return result, candidate, changes


def validate_revision_catalog(previous, candidate, group):
    if group != 'a3' or {k: v for k, v in previous.items() if k != 'panels'} != {k: v for k, v in candidate.items() if k != 'panels'}:
        raise ValueError('Only A3 expression replacement with unchanged steps is supported')
    if len(previous['panels']) != len(candidate['panels']):
        raise ValueError('Catalog membership changed')
    expected = {'/'.join(t) for t in batch_targets('a3-histograms')}
    changed, seen = set(), set()
    for old, new in zip(previous['panels'], candidate['panels']):
        if old['id'] != new['id'] or old['id'] in seen:
            raise ValueError('Catalog order or identity changed')
        seen.add(old['id'])
        if old['id'] not in expected:
            if old != new:
                raise ValueError('Unrelated catalog entry changed')
            continue
        for entry in (old, new):
            body = {k: v for k, v in entry.items() if k != 'revision'}
            if entry['revision'] != hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest():
                raise ValueError('Catalog revision digest mismatch')
        body = copy.deepcopy(old)
        body['expression'] = histogram(old['expression'])
        body['revision'] = new['revision']
        if body != new or new['revision'] == old['revision'] or new['group'] != group:
            raise ValueError('Unexpected A3 revision change')
        changed.add(old['id'])
    if changed != expected:
        raise ValueError('Incomplete A3 replacement')
