"""Read-only counter deltas, explicitly bounded by recorded counter lifetimes.

VM evaluates the summaries; raw scrape history is never downloaded or persisted.
No increase/rate function may choose a baseline from an earlier lifetime.
"""
import asyncio
from collections import defaultdict
import json
import math

HISTOGRAMS = ('request_bytes', 'prompt_tokens',
              'completion_tokens', 'first_increment_seconds', 'request_duration_seconds')
COUNTERS = ('requests_routed_total', 'requests_ended_total', 'prompt_tokens_total',
            'completion_tokens_total', 'cached_tokens_total', 'cache_prompt_tokens_total',
            'cache_usage_known_total')
EXTRA = ('requests_started_total', 'usage_known_total', 'usage_missing_total',
         'token_pairs_total',
         'profile_parse_failures_total')
VOLATILE = ('profile_dropped_events_total', 'profile_write_errors_total')
NAMES = COUNTERS + EXTRA + tuple(n + suffix for n in HISTOGRAMS
                                 for suffix in ('_bucket', '_sum', '_count'))
GAUGES = ('profile_counter_start_time_seconds', 'profile_group_start_time_seconds',
          'profile_start_time_seconds')
BASELINE_LOOKBACK = 60
FRESHNESS = 15
MAX_LIFETIMES = 256
MAX_SERIES = 20000


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def key(labels):
    return tuple(sorted(labels.items()))


def epoch(value):
    # VM scrape timestamps have millisecond precision. Float serialization may
    # shift a preserved epoch by fractions of a microsecond on a normal restart.
    value = number(value)
    return round(value, 3) if value is not None and value > 0 else None


def metric_selector(labels, names):
    labels = dict(labels)
    labels.pop('request_scope', None)
    # First-increment histograms share the new all-request counter lifetime.
    # General metrics use all; only this histogram uses streaming.
    ordinary = [n for n in names if not n.startswith('first_increment_seconds_')]
    streaming = [n for n in names if n.startswith('first_increment_seconds_')]
    parts = ['__name__=~' + json.dumps('aigate_(' + '|'.join(names) + ')')]
    if ordinary and streaming:
        parts.append('request_scope=~"all|streaming"')
    else:
        labels['request_scope'] = 'streaming' if streaming else 'all'
    parts += [k + '=' + json.dumps(v, ensure_ascii=False) for k, v in sorted(labels.items())]
    return '{' + ','.join(parts) + '}'


def summary_rows(rows):
    result = {}
    for row in rows:
        labels = dict(row['metric'])
        expected = 'streaming' if labels.get('__name__', '').startswith('aigate_first_increment_seconds_') else 'all'
        if labels.get('request_scope') != expected:
            continue
        rollup = labels.pop('rollup')
        result.setdefault(key(labels), {})[rollup] = number(row['value'][1])
    return result


def counter_delta(stats, baseline, born_in_window, end):
    last = stats.get('last_over_time')
    minimum = stats.get('min_over_time')
    stamp = stats.get('tlast_over_time')
    first = stats.get('first_over_time')
    increase = stats.get('increase_prometheus')
    if increase is None and stats.get('count_over_time') == 1:
        increase = 0
    if last is None or minimum is None or stamp is None or first is None or increase is None:
        return None, 'missing_sample'
    # Even decreases_over_time can consult a pre-window sample in VM. Compare
    # the strictly in-window increase with last-first to detect internal resets.
    if last < 0 or minimum < 0 or not math.isclose(increase,last-first,rel_tol=1e-10,abs_tol=1e-9):
        return None, 'unexplained_counter_reset'
    if end - stamp > FRESHNESS:
        return None, 'unobserved_lifecycle_tail'
    if born_in_window:
        return last, None
    if baseline is None or baseline.get('last_over_time') is None:
        return None, 'missing_boundary_baseline'
    value = baseline['last_over_time']
    if minimum < value:
        return None, 'unexplained_counter_reset'
    return last - value, None


class CounterWindow:
    def __init__(self, service, vm, start, end, backend='', model='', environment='dcu-pd'):
        self.service, self.vm = service, vm
        self.start, self.end = start, end
        self.backend, self.model = backend, model
        if environment not in ('dcu-pd', 'a3-vllm', 'xpu-pd'):
            raise ValueError('Unknown profile environment')
        self.environment = environment
        self.slots = asyncio.Semaphore(4)
        self.issues = set()
        self.trend_issues = []
        self.lifetimes = []
        self.totals = {}

    async def query(self, expression, at):
        async with self.slots:
            r = await self.service.client.get(self.vm + '/api/v1/query', params={
                'query': expression, 'time': at, 'latency_offset': '1ms', 'nocache': '1'})
            r.raise_for_status()
            data = r.json()
            if data.get('status') != 'success':
                raise ValueError('Counter summary query failed')
            rows = data['data']['result']
            if len(rows) > MAX_SERIES:
                raise ValueError('Counter summary series limit exceeded')
            return rows

    async def summaries(self, labels, names, left, right):
        if right <= left:
            return {}
        samples = metric_selector(labels, names) + '[' + str(right-left) + 's]'
        expression = ('aggr_over_time(("min_over_time","last_over_time",'
                      '"first_over_time","count_over_time","tlast_over_time"),'
                      + samples + ') keep_metric_names')
        rows, increments = await asyncio.gather(
            self.query(expression, right),
            self.query('increase_prometheus('+samples+') keep_metric_names', right))
        for row in increments:
            row['metric']['rollup'] = 'increase_prometheus'
        return summary_rows(rows + increments)

    async def discover(self):
        expression = ('count_values_over_time("profile_epoch",'
                      + metric_selector({'job': 'aigate', 'environment': self.environment}, GAUGES)
                      + '[' + str(self.end-self.start+BASELINE_LOOKBACK)
                      + 's]) keep_metric_names')
        groups = defaultdict(set)
        for row in await self.query(expression, self.end):
            labels = dict(row['metric'])
            if labels.get('environment') != self.environment or labels.get('request_scope') != 'all':
                continue
            birth = epoch(labels.pop('profile_epoch'))
            name = labels.pop('__name__')
            if birth is None or birth > self.end:
                continue
            if 'backend' in labels or 'model' in labels:
                if self.backend and labels.get('backend') != self.backend:
                    continue
                if self.model and labels.get('model') != self.model:
                    continue
            elif name == 'aigate_profile_group_start_time_seconds':
                continue
            groups[(name, key(labels))].add(birth)
        for (name, identity), births in groups.items():
            labels = dict(identity)
            if name != 'aigate_profile_group_start_time_seconds':
                labels.update(backend='', model='')
            names = VOLATILE if name == 'aigate_profile_start_time_seconds' else NAMES
            ordered = sorted(births)
            for i, birth in enumerate(ordered):
                upper = ordered[i+1] - .001 if i+1 < len(ordered) else self.end
                if name == 'aigate_profile_group_start_time_seconds':
                    target = key({k:v for k,v in identity if k not in ('backend', 'model')})
                    boots = groups.get(('aigate_profile_counter_start_time_seconds', target), ())
                    later_boots = [t for t in boots if t > birth]
                    if later_boots:
                        # A reset ends the old group even if the new group is not
                        # created until its first request several minutes later.
                        upper = min(upper, min(later_boots)-.001)
                if upper <= self.start:
                    continue
                self.lifetimes.append((labels, names, birth, min(upper, self.end)))
        if len(self.lifetimes) > MAX_LIFETIMES:
            raise ValueError('Counter lifetime limit exceeded')

    async def segment(self, labels, names, birth, end):
        left = max(self.start, birth)
        summaries = await self.summaries(labels, names, left, end)
        born_in_window = birth >= self.start
        baseline = {} if born_in_window else await self.summaries(
            labels, names, max(birth, self.start-BASELINE_LOOKBACK), self.start)
        baseline = {k:v for k,v in baseline.items()
                    if v.get('tlast_over_time') is not None
                    and self.start-v['tlast_over_time'] <= FRESHNESS}
        deltas = {}
        # A missing trailing series must not silently drop a prior contribution.
        for identity in summaries.keys() | baseline.keys():
            value, issue = counter_delta(summaries.get(identity, {}), baseline.get(identity),
                                         born_in_window, end)
            name = dict(identity)['__name__'].removeprefix('aigate_')
            if issue:
                self.issues.add((name, issue))
                # Missing shutdown samples only invalidate the tail, not the
                # healthy history or a later counter lifetime. Other faults
                # remain conservative within their affected segment.
                invalid_start = left
                if issue == 'unobserved_lifecycle_tail':
                    invalid_start = summaries[identity]['tlast_over_time']
                self.trend_issues.append((name, issue, invalid_start, end))
            deltas[identity] = value
        # Validate each source/lifetime before aggregation: errors from two
        # sources must not accidentally cancel out into plausible totals.
        families = defaultdict(list)
        for identity, value in deltas.items():
            labels = dict(identity)
            name = labels.pop('__name__').removeprefix('aigate_')
            if name.endswith('_bucket'):
                upper = labels.pop('le')
                families[(name[:-7], key(labels))].append((identity, upper, value))
        for (name, labels), rows in families.items():
            count_key = key(dict(labels, __name__='aigate_'+name+'_count'))
            buckets = [{'labels': {'le':upper}, 'value':v} for _,upper,v in rows]
            problem = histogram_problem(buckets, deltas.get(count_key))
            if problem:
                self.issues.add((name, problem))
                for identity,_,_ in rows:
                    deltas[identity] = None
                for suffix in ('_count', '_sum'):
                    deltas[key(dict(labels, __name__='aigate_'+name+suffix))] = None
        for identity, value in deltas.items():
            if identity not in self.totals:
                self.totals[identity] = value
            elif value is None or self.totals[identity] is None:
                self.totals[identity] = None
            else:
                self.totals[identity] += value

    async def read(self):
        await self.discover()
        await asyncio.gather(*(self.segment(*life) for life in self.lifetimes))
        # The all-request origin also marks the start of this population policy.
        # First-increment names/scope existed before that origin; those legacy
        # samples must not poison a query spanning the policy change.
        source_key = lambda labels: key({k:v for k,v in labels.items()
                                        if k not in ('backend', 'model', 'request_scope')})
        policy_starts = {}
        for labels, names, birth, _ in self.lifetimes:
            if names == NAMES and not labels.get('backend') and not labels.get('model'):
                source = source_key(labels)
                policy_starts[source] = min(policy_starts.get(source, birth), birth)
        # Data predating the first recorded lifetime is not known to start at zero.
        first = {}
        for labels, names, birth, _ in self.lifetimes:
            identity = (key(labels), names)
            first[identity] = min(first.get(identity, birth), birth)
        async def check_unassigned(identity, birth):
            labels, names = identity
            left = max(self.start, policy_starts.get(source_key(dict(labels)), self.start))
            if birth <= left:
                return
            for series in await self.summaries(dict(labels), names, left, birth-.001):
                self.totals[series] = None
                name = dict(series)['__name__'].removeprefix('aigate_')
                self.issues.add((name, 'missing_lifecycle'))
                self.trend_issues.append((name, 'missing_lifecycle', left, birth))
        await asyncio.gather(*(check_unassigned(identity, birth) for identity,birth in first.items()))
        return self

    def aggregate(self, name, by=()):
        groups = {}
        for identity, value in self.totals.items():
            labels = dict(identity)
            if labels['__name__'] != 'aigate_' + name:
                continue
            group = tuple((k, labels[k]) for k in by if k in labels)
            if group not in groups:
                groups[group] = value
            elif groups[group] is None or value is None:
                groups[group] = None
            else:
                groups[group] += value
        return [{'labels': dict(k), 'value': v} for k, v in sorted(groups.items())]

    def trend_valid(self, at, width, metric):
        # Retain unknown totals, but only mask rolling rates whose lookbehind
        # overlaps the faulty interval. Unlocalized faults still fail closed.
        localized = {(name, reason) for name, reason, _, _ in self.trend_issues}
        if any(name == metric and (name, reason) not in localized
               for name, reason in self.issues):
            return False
        if any(name == metric and at >= left and at-width-FRESHNESS <= right
               for name, _, left, right in self.trend_issues):
            return False
        lives = [life for life in self.lifetimes if life[0].get('backend')]
        return (any(birth <= at <= end for _,_,birth,end in lives)
                and not any(at-width-FRESHNESS < birth <= at for _,_,birth,_ in lives)
                and not any(at-width-FRESHNESS < end < at for _,_,_,end in lives))


def histogram_valid(buckets, count):
    return histogram_problem(buckets, count) is None


def histogram_problem(buckets, count):
    if count is None or not buckets or any(r['value'] is None for r in buckets):
        return 'incomplete_histogram'
    if count < 0:
        return 'inconsistent_histogram'
    previous = 0
    ordered = sorted((float(r['labels']['le']), r['value']) for r in buckets)
    for upper, value in ordered:
        if math.isnan(upper) or value is None or value < previous or value > count:
            return 'inconsistent_histogram'
        previous = value
    if ordered[-1][0] != math.inf:
        return 'incomplete_histogram'
    if not math.isclose(previous, count, rel_tol=1e-9, abs_tol=1e-9):
        return 'inconsistent_histogram'
    return None


def quantile(buckets, q):
    ordered = sorted((float(r['labels']['le']), r['value']) for r in buckets)
    if not ordered or ordered[-1][1] is None or ordered[-1][1] <= 0:
        return None
    target = ordered[-1][1] * q
    lower = previous = 0
    for upper, count in ordered:
        if count >= target:
            if math.isinf(upper):
                return lower
            return lower + (upper-lower) * (target-previous)/(count-previous) if count > previous else upper
        lower, previous = upper, count
    return None


def profile_values(window, backend='', model=''):
    values = {name: window.aggregate(name) for name in COUNTERS}
    mappings = {'outcomes': ('requests_ended_total', ('outcome',)),
                'usage_known': ('usage_known_total', ('field',)),
                'usage_missing': ('usage_missing_total', ('field',)),
                'token_pairs': ('token_pairs_total', ('input_le', 'output_le')),
                'parse_failures': ('profile_parse_failures_total', ('reason',)),
                'dropped_events': ('profile_dropped_events_total', ()),
                'write_errors': ('profile_write_errors_total', ())}
    values.update({key: window.aggregate(name, by) for key, (name, by) in mappings.items()})
    if not backend and not model:
        values['requests_started_total'] = window.aggregate('requests_started_total')
    for name in HISTOGRAMS:
        by = ()
        counts = window.aggregate(name+'_count', by)
        buckets = window.aggregate(name+'_bucket', by+('le',))
        sums = window.aggregate(name+'_sum', by)
        groups = {key(r['labels']): r['value'] for r in counts}
        sum_index = {key(r['labels']): r['value'] for r in sums}
        for group, count in groups.items():
            selected = [r for r in buckets if key({k:v for k,v in r['labels'].items() if k!='le'}) == group]
            problem = histogram_problem(selected, count)
            valid = problem is None
            if problem:
                window.issues.add((name, problem))
                for row in selected:
                    row['value'] = None
            labels = dict(group)
            total = sum_index.get(group)
            mean = total/count if valid and count and total is not None else None
            values.setdefault(name+'_mean', []).append({'labels': labels, 'value': mean})
            for q in (.5, .95, .99):
                values.setdefault(name+'_p'+str(int(q*100)), []).append({
                    'labels': labels, 'value': quantile(selected, q) if valid else None})
        # An orphan bucket is incomplete even when the corresponding count vanished.
        for row in buckets:
            if key({k:v for k,v in row['labels'].items() if k!='le'}) not in groups:
                row['value'] = None
                window.issues.add((name, 'incomplete_histogram'))
        values[name+'_buckets'] = buckets
    return values
