"""Trim panels operators do not read; idempotent, applied after generation.

Removes ratio panels that repeat a value panel, overlapping or data-quality
panels, sample-count companions and most monitoring-pipeline internals. Merged
and moved panels reuse the stored queries. Raw collection is unchanged.
"""
import copy

PROJECTS = ('dcu-monitoring', 'a3-monitoring', 'xpu-monitoring')
HOSTS = {'dcu-monitoring': 'hosts-dcu', 'a3-monitoring': 'a3-hosts', 'xpu-monitoring': 'hosts-xpu'}
# Emptied by the trim; release deletes them from the server.
RETIRED_DASHBOARDS = ('gateway',)
PROFILE_DROPS = 'overview-gateway-dropped'
RESULTS = (('generation-3', '客户端取消'), ('generation-4', '客户端断开'), ('generation-5', '未知结果'))
REFERENCE_COLOR = '#9E9E9E'

# (project, dashboard) -> {retired panel: panel moved into its slot, or None}
RETIRED = {}
for _project, _hosts in HOSTS.items():
    RETIRED[(_project, _hosts)] = {'core-extra-memory-ratio': None, 'core-p2': 'core-extra-fs-free'}
    RETIRED[(_project, 'accelerator-resources')] = {'core-memory-ratio': None, 'core-memory-total': None}
    RETIRED[(_project, 'gateway-requests')] = {
        'generation-2': None, 'generation-3': None, 'generation-4': None, 'generation-5': None,
        'extra-ended': None, 'extra-usage': None}
    RETIRED[(_project, 'gateway-generation')] = {'live-waiting': None, 'live-unknown': None}
    RETIRED[(_project, 'monitoring-health')] = dict.fromkeys((
        'overview-extra-scrape-duration', 'overview-extra-derived-valid', 'extra-up', 'extra-cpu',
        'extra-memory', 'extra-data', 'extra-ingest', 'extra-parse'))
for _role in ('prefill', 'decode'):
    # DCU core-kv and bn-*-kv-usage both plot sglang:token_usage; the capacity panel holds the Token values.
    RETIRED[('dcu-monitoring', 'backend-' + _role)] = {
        'core-kv': 'bn-' + _role + '-kv-capacity', 'bn-' + _role + '-kv-usage': None, 'bn-' + _role + '-swa-usage': None}
RETIRED[('dcu-monitoring', 'cache-store')] = {'p2': None, 'extra-segment-ratio': None}
for _project in ('dcu-monitoring', 'xpu-monitoring'):
    # Decode Token repeats output Token throughput, which also covers Prefill.
    RETIRED[(_project, 'backend-performance')] = {'extra-latency-samples': None, 'overview-p3': None}
RETIRED[('xpu-monitoring', 'cache-store')] = {'p1': None, 'p0': None}
RETIRED[('a3-monitoring', 'a3-cache')] = {'p0': None}  # same source as backend core-kv


def keys(layout):
    return [item['content']['$ref'].rsplit('/', 1)[1]
            for item in sorted(layout['spec']['items'], key=lambda x: (x['y'], x['x']))]


def regrid(layout, order):
    top = min((item['y'] for item in layout['spec']['items']), default=0)
    layout['spec']['items'] = [{'x': i % 2 * 12, 'y': top + i // 2 * 8, 'width': 12, 'height': 8,
                                'content': {'$ref': '#/spec/panels/' + key}} for i, key in enumerate(order)]


def query(panel, index=0):
    return panel['spec']['queries'][index]['spec']['plugin']['spec']['query']


def merge_generation_results(d):
    """Plot the ended-result numerators as rates beside the total instead of separate ratios."""
    panels = d['spec']['panels']
    if d['metadata']['name'] != 'gateway-requests' or 'generation-3' not in panels:
        return
    total = panels['generation-0']
    separator = ') / on(environment) (('
    for key, label in RESULTS:
        expression = query(panels[key])
        assert expression.startswith('100 * ((') and expression.count(separator) == 1, key
        added = copy.deepcopy(total['spec']['queries'][0])
        spec = added['spec']['plugin']['spec']
        spec['query'] = expression[len('100 * (('):expression.index(separator)]
        spec['seriesNameFormat'] = label
        total['spec']['queries'].append(added)
    total['spec']['queries'][0]['spec']['plugin']['spec']['seriesNameFormat'] = '全部结束'
    display = total['spec']['display']
    scope = display['description'].rsplit('\n\n', 1)[-1]
    display['name'] = '网关 · 生成结束结果（请求/秒）'
    display['description'] = (
        '指标含义\n最近 1 分钟进入下游生成阶段的请求结束速率，含流式与非流式。「全部结束」包含正常完成、错误、'
        '客户端取消、客户端断开和未知结果，不是成功请求速率；其余三条是其中按结束结果拆出的速率，后端错误见'
        '「后端错误速率」。客户端取消不能单独判定是用户操作还是上游超时；客户端断开通常与向客户端写入失败有关；'
        '未知结果不能按正常完成解释。\n\n' + scope)


def add_total_reference(d):
    """Fold the constant per-card total into the used panel as one reference line."""
    panels = d['spec']['panels']
    if d['metadata']['name'] != 'accelerator-resources' or 'core-memory-total' not in panels:
        return
    used = panels['core-memory-used']
    reference = copy.deepcopy(panels['core-memory-total']['spec']['queries'][0])
    spec = reference['spec']['plugin']['spec']
    spec['query'] = 'max(' + spec['query'] + ')'
    spec['seriesNameFormat'] = '单卡总量'
    used['spec']['queries'].append(reference)
    chart = used['spec']['plugin']['spec']
    chart.setdefault('querySettings', []).append(
        {'queryIndex': len(used['spec']['queries']) - 1, 'colorMode': 'fixed', 'colorValue': REFERENCE_COLOR})
    used['spec']['display']['description'] = used['spec']['display']['description'].replace(
        '要判断显存是否接近耗尽，还需结合该卡总容量。', '灰线为单卡总量，用于判断余量。', 1)
    if '灰线' not in used['spec']['display']['description']:
        parts = used['spec']['display']['description'].split('\n\n', 1)
        parts[0] += '灰线为单卡总量，用于判断余量。'
        used['spec']['display']['description'] = '\n\n'.join(parts)


def move_profile_drops(dashboards):
    """Keep only profile event drops from the gateway profile dashboard, under collection health."""
    by_key = {(d['metadata']['project'], d['metadata']['name']): d for d in dashboards}
    for project in PROJECTS:
        source, health = by_key.get((project, 'gateway')), by_key.get((project, 'monitoring-health'))
        if not source or not health or PROFILE_DROPS in health['spec']['panels']:
            continue
        panel = copy.deepcopy(source['spec']['panels']['p6'])
        panel['spec']['display']['name'] = '网关画像事件丢弃速率（事件/秒）'
        health['spec']['panels'][PROFILE_DROPS] = panel
        layout = next(l for l in health['spec']['layouts'] if 'overview-p0' in keys(l))
        regrid(layout, keys(layout) + [PROFILE_DROPS])


def retire(document):
    retired = dict(RETIRED.get((document['metadata'].get('project'), document['metadata']['name']), {}))
    retired.update((k, None) for k in document['spec']['panels'] if k.endswith('-samples'))
    if not retired.keys() & document['spec']['panels'].keys():
        return document
    d = copy.deepcopy(document)
    spec = d['spec']
    merge_generation_results(d)
    add_total_reference(d)
    groups = [keys(layout) for layout in spec['layouts']]
    for key, keeper in retired.items():
        if key not in spec['panels']:
            continue
        del spec['panels'][key]
        if keeper in spec['panels']:
            for order in groups:
                if keeper in order:
                    order.remove(keeper)
        for order in groups:
            if key in order:
                index = order.index(key)
                order[index:index + 1] = [keeper] if keeper in spec['panels'] else []
    layouts = []
    for layout, order in zip(spec['layouts'], groups):
        if order:
            regrid(layout, order)
            layouts.append(layout)
    spec['layouts'] = layouts
    return d


def apply(resources):
    result = dict(resources)
    dashboards = copy.deepcopy(resources['dashboards'])
    move_profile_drops(dashboards)
    result['dashboards'] = [retire(d) for d in dashboards if d['metadata']['name'] not in RETIRED_DASHBOARDS]
    return result
