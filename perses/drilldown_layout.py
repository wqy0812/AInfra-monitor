"""Drill-down presentation: collapse diagnostic groups and draw fewer lines per chart.

Idempotent and applied after the trim. Accelerated panels keep their exact
expressions; only non-accelerated queries are reshaped here.
"""
import copy
import json
import re

from project_queries import Queries
from summary_dashboard import node_role

# Dashboard name -> group titles collapsed by default; Perses renders them only when opened.
COLLAPSED = {
    'backend-performance': ('专属诊断',),
    'backend-prefill': ('专属诊断',),
    'backend-decode': ('专属诊断',),
    'a3-hosts': ('专属诊断',),
    'gateway-requests': ('Token 与 Usage',),
    'a3-cache': ('Mooncake 容量与对象', 'Mooncake 驱逐与写入清理'),
    'cache-store': ('Store 查询', 'HiCache 与分片明细', 'HiCache 预取等待'),
}
# Titles for untitled groups, so detail rows can collapse. The last group takes unlisted panels.
GROUPS = {
    ('dcu-monitoring', 'cache-store'): [('命中与容量', ['p0', 'p1', 'p3', 'p4', 'p5']),
                                        ('Store 查询', ['p6', 'p7', 'extra-store-rate']),
                                        ('HiCache 与分片明细', [])],
}
TITLES = {('a3-monitoring', 'a3-cache'): '前缀缓存'}
HOSTS = ('hosts-dcu', 'a3-hosts', 'hosts-xpu')
DEVICES = 'lo|loop.*|ram.*|veth.*|docker.*|br-.*'
# Also drop LVM/device-mapper volumes (their I/O repeats the physical disk) and container overlay links.
FEWER_DEVICES = DEVICES + '|dm-.*|cali.*|flannel.*|cni.*|tunl.*|vxlan.*|virbr.*|kube-ipvs.*|nodelocaldns'
A3_PREFIX = {'extra-prefix_cache_': 'prefix_cache_', 'extra-external_prefix_cache_': 'external_prefix_cache_'}
# Match the configured collection scope, including endpoints absent from the query window.
A3_ENGINE_TARGETS = {'a3-1': ('122.209.21.24', 4), 'a3-2': ('122.209.21.25', 16)}


def order(d):
    return [item['content']['$ref'].rsplit('/', 1)[1]
            for layout in d['spec']['layouts']
            for item in sorted(layout['spec']['items'], key=lambda x: (x['y'], x['x']))]


def grid(keys):
    return [{'x': i % 2 * 12, 'y': i // 2 * 8, 'width': 12, 'height': 8, 'content': {'$ref': '#/spec/panels/' + key}}
            for i, key in enumerate(keys)]


def regroup(d, groups):
    keys = order(d)
    listed = {key for _, members in groups for key in members}
    layouts = []
    for index, (title, members) in enumerate(groups):
        chosen = [k for k in members if k in keys] + ([k for k in keys if k not in listed] if index == len(groups) - 1 else [])
        if chosen:
            layouts.append({'kind': 'Grid', 'spec': {'display': {'title': title}, 'items': grid(chosen)}})
    d['spec']['layouts'] = layouts


def collapse(d):
    closed = COLLAPSED.get(d['metadata']['name'], ())
    for layout in d['spec']['layouts']:
        display = layout['spec'].get('display')
        if display and display.get('title') in closed:
            display['collapse'] = {'open': False}


def host_lines(d):
    panels = d['spec']['panels']
    load = panels['core-extra-load']['spec']
    load['queries'] = [q for q in load['queries'] if q['spec']['plugin']['spec']['seriesNameFormat'].endswith('· 5m')] or load['queries']
    load['display']['description'] = load['display']['description'].replace('1/5/15 分钟系统负载', '5 分钟平均系统负载', 1)
    for panel in panels.values():
        for q in panel['spec']['queries']:
            spec = q['spec']['plugin']['spec']
            spec['query'] = spec['query'].replace('device!~"' + DEVICES + '"', 'device!~"' + FEWER_DEVICES + '"')
    if 'core-p3' in panels:
        display = panels['core-p3']['spec']['display']
        display['description'] = display['description'].replace(
            '逻辑盘与其底层物理盘可能重复承载同一 I/O，不应把所有曲线直接相加。',
            '排除 dm 逻辑卷等虚拟设备，避免与物理盘重复计数。', 1)


def a3_prefix_rate(metric):
    nodes = []
    for node, (address, count) in A3_ENGINE_TARGETS.items():
        ports = '|'.join(str(7100 + engine) for engine in range(count))
        instances = re.escape(address) + ':(' + ports + ')'
        q = Queries('a3-vllm', 'vllm-a3', ',node=' + json.dumps(node) + ',instance=~' + json.dumps(instances))
        # Counting the exact endpoint set also catches engines absent for longer than 1m.
        # Each endpoint must expose one series; duplicates cannot replace a missing instance.
        complete = (f'count by(environment,node) (count by(environment,node,instance) '
                    f'({q.gauge(metric)}) == 1) == {count}')
        nodes.append(f'({q.rate(metric, group="environment,node")}) and on(environment,node) ({complete})')
    return ' or '.join('(' + query + ')' for query in nodes)


def a3_prefix_lines(d):
    for key, prefix in A3_PREFIX.items():
        panel = d['spec']['panels'].get(key)
        if not panel:
            continue
        for query, (suffix, label) in zip(panel['spec']['queries'], (('queries_total', '查询'), ('hits_total', '命中'))):
            spec = query['spec']['plugin']['spec']
            # Sum engines per node; a node with any incomplete engine stays blank rather than partial.
            spec['query'] = node_role(a3_prefix_rate('vllm:' + prefix + suffix), 'a3-monitoring')
            spec['seriesNameFormat'] = '{{role}} · {{node}} · ' + label
        display = panel['spec']['display']
        display['description'] = display['description'].replace(
            '前缀缓存按 Token 统计的查询/命中速率；', '前缀缓存按 Token 统计的查询/命中速率，按节点汇总各引擎；任一引擎采集不完整时该节点留空；', 1)


def present(document):
    d = copy.deepcopy(document)
    project, name = d['metadata']['project'], d['metadata']['name']
    if (project, name) in GROUPS:
        regroup(d, GROUPS[(project, name)])
    title = TITLES.get((project, name))
    if title:
        first = d['spec']['layouts'][0]['spec']
        first.setdefault('display', {}).setdefault('title', title)
    if name in HOSTS:
        host_lines(d)
    if project == 'a3-monitoring' and name == 'a3-cache':
        a3_prefix_lines(d)
    collapse(d)
    return d


def apply(resources):
    result = dict(resources)
    result['dashboards'] = [present(d) for d in resources['dashboards']]
    return result
