"""Per-project at-a-glance dashboard derived from the stored detail panels.

Each panel aggregates an existing, already gated query to at most four lines
(by role or node). Detail dashboards stay the drill-down. Regeneration rebuilds
the summary from its sources, so it never drifts from them.
"""
import copy
import re

from generate import panel
from project_queries import Queries

NAME = 'summary'
NODES = {'dcu-monitoring': ('dcu1', 'dcu2'), 'a3-monitoring': ('a3-1', 'a3-2'), 'xpu-monitoring': ('xpu-2', 'xpu-1')}
ENV = {'dcu-monitoring': 'dcu-pd', 'a3-monitoring': 'a3-vllm', 'xpu-monitoring': 'xpu-pd'}
HOSTS = {'dcu-monitoring': 'hosts-dcu', 'a3-monitoring': 'a3-hosts', 'xpu-monitoring': 'hosts-xpu'}
VARIABLE = re.compile(r'\$(role|node|device)\b')
REFERENCE_COLOR = '#9E9E9E'
ROLES = (('prefill', 'Prefill'), ('decode', 'Decode'))


def unfiltered(query):
    return VARIABLE.sub('.*', query)


def path_role(query):
    for role, label in ROLES:
        query = f'label_replace({query}, "role", "{label}", "path", "nodes[.]{role}[.].*")'
    return query


def node_role(query, project):
    for (_, label), node in zip(ROLES, NODES[project]):
        query = f'label_replace({query}, "role", "{label}", "node", "{node}")'
    return query


class Sources:
    def __init__(self, dashboards, project):
        self.dashboards = {d['metadata']['name']: d for d in dashboards if d['metadata']['project'] == project}

    def queries(self, dashboard, key):
        return [q['spec']['plugin']['spec'] for q in self.dashboards[dashboard]['spec']['panels'][key]['spec']['queries']]

    def query(self, dashboard, key, index=0, legend=None):
        specs = self.queries(dashboard, key)
        if legend is not None:
            matches = [s for s in specs if s.get('seriesNameFormat', '').endswith(legend)]
            assert len(matches) == 1, (dashboard, key, legend)
            return matches[0]['query']
        return specs[index]['query']


def chart(title, queries, desc, settings=None):
    p = panel(title, queries, desc=desc)
    p['spec']['plugin']['spec']['yAxis'].pop('label', None)
    if settings:
        p['spec']['plugin']['spec']['querySettings'] = settings
    return p


def percentile(query, kind):
    pattern = 'percentiles.' + kind + '.(p50|p95|p99)'
    assert pattern in query, kind
    return path_role(unfiltered(query.replace(pattern, 'percentiles.' + kind + '.p95')))


def backend_panels(project, src):
    performance = 'backend-performance'
    panels = {
        'backend-ttft': chart('首 Token 延迟 TTFT P95（秒）', [(percentile(src.query(performance, 'core-ttft'), 'ttft'), '{{role}}')],
                              '后端原生 TTFT 的 P95，按 Prefill / Decode 分线。P50 / P99 与明细见「后端请求性能」。'),
        'backend-e2e': chart('端到端延迟 E2E P95（秒）', [(percentile(src.query(performance, 'core-e2e'), 'e2e'), '{{role}}')],
                             '后端原生 E2E 的 P95，按 Prefill / Decode 分线。P50 / P99 与明细见「后端请求性能」。'),
        'backend-output': chart('后端输出 Token 吞吐（Token/秒）', [(path_role(unfiltered(src.query(performance, 'core-output'))), '{{role}}')],
                                '服务侧输出 Token 速率，按角色分线；P/D 分离时 Prefill 侧通常只有首个 Token。'),
    }
    if project == 'a3-monitoring':
        # The derived A3 queue paths are keyed by engine; read the raw per-instance gauge instead.
        waiting = []
        for (_, label), node in zip(ROLES, NODES[project]):
            q = Queries(ENV[project], 'vllm-a3', ',node="' + node + '"')
            waiting.append(('max(' + q.gauge('vllm:num_requests_waiting') + ')', label))
    else:
        waiting = [(f'max({src.query("backend-" + role, "core-queue", legend="排队")})', label) for role, label in ROLES]
    panels['backend-queue'] = chart('排队请求 · 单实例最大（请求）', waiting,
                                    '各实例或 rank 排队请求数的最大值，按角色分线；不把复制 rank 相加。明细见 Prefill / Decode 诊断。')
    if project == 'dcu-monitoring':
        kv = [(f'max({src.query("backend-" + role, "bn-" + role + "-kv-capacity", legend=suffix)})', label + ' ' + name)
              for role, label in ROLES for suffix, name in (('活跃', '已用'), ('总容量', '总量'))]
        panels['backend-kv'] = chart('KV 池 · 单 rank 最大已用与总量（Token）', kv,
                                     '各 rank KV 池活跃占用与总容量的最大值，按角色分线；已用接近总量说明 KV 池将满。逐 rank 明细见 Prefill / Decode 诊断。')
    else:
        kv = [(f'max({src.query("backend-" + role, "core-kv")})', label) for role, label in ROLES]
        panels['backend-kv'] = chart('KV 池占用 · 单实例最大（%）', kv,
                                     '各实例 KV 池占用比例的最大值，按角色分线；接近 100% 说明 KV 池将满。明细见 Prefill / Decode 诊断。')
    return panels


def cache_panel(project, src):
    if project == 'dcu-monitoring':
        queries = [(path_role(unfiltered(src.query('cache-store', 'p0'))), '{{role}}')]
        desc = '最近约 60 秒模型前缀缓存命中 Token 占查询 Token 的比例，按角色分线。分层与容量见「缓存与存储」。'
    elif project == 'a3-monitoring':
        queries = [(path_role(unfiltered(src.query('a3-cache', key))), label + ' · {{role}}')
                   for key, label in (('p1', '本地'), ('p2', '外部 KV'))]
        desc = '最近约 60 秒本地与外部 KV 前缀缓存命中率，按角色分线；两者分母不同，不能相加。明细见「缓存与存储」。'
    else:
        queries = [(src.query('cache-store', 'cache-hit-window'), 'Prefill')]
        desc = '最近一分钟 Prefill 缓存复用 Token 占新增计算与复用 Token 之和的比例。明细见「缓存与存储」。'
    return chart('前缀缓存命中率（%）', queries, desc)


def build(project, dashboards):
    src = Sources(dashboards, project)
    env = ENV[project]
    gateway = {
        'gateway-arrivals': chart('网关请求到达速率（请求/秒）', [(src.query('gateway-requests', 'gateway-arrivals'), '到达')],
                                  '网关完成解析、进入画像的请求速率，含流式与非流式。明细见「网关请求流量与质量」。'),
        'gateway-errors': chart('网关后端错误速率（次/秒）', [(src.query('gateway-requests', 'generation-1'), '后端错误')],
                                '网关记录的下游生成错误速率；高于 0 即有错误。结束结果拆分见「网关请求流量与质量」。'),
        'gateway-inflight': chart('网关在途请求（请求）', [(src.query('gateway-generation', 'gateway-inflight'), '在途')],
                                  '当前尚未结束的请求数；持续上升说明后端处理不过来。处理阶段见「网关在途请求诊断」。'),
        'gateway-wait-max': chart('等待首个有效输出 · 最长等待（秒）', [(src.query('gateway-generation', 'live-wait-max'), '最长等待')],
                                  '当前仍在等待首个有效输出的请求中最长的等待时间；持续升高说明有请求卡在后端。'),
    }
    backend = backend_panels(project, src)
    used, total = src.queries('accelerator-resources', 'core-memory-used')
    hardware = {
        'cache-hit': cache_panel(project, src),
        'accelerator-utilization': chart('加速卡平均利用率（%）', [(node_role(f'avg by(node) ({unfiltered(src.query("accelerator-resources", "core-utilization"))})', project), '{{role}} · {{node}}')],
                                         '每个节点所有加速卡利用率的平均值。逐卡明细见「加速卡资源」。'),
        'accelerator-memory': chart('加速卡显存 · 单卡最大已用（GiB）', [
            (node_role(f'max by(node) ({unfiltered(used["query"])})', project), '{{role}} · {{node}}'),
            (unfiltered(total['query']), '单卡总量')],
            '每个节点显存已用最多的那张卡；灰线为单卡总量。逐卡明细见「加速卡资源」。',
            [{'queryIndex': 1, 'colorMode': 'fixed', 'colorValue': REFERENCE_COLOR}]),
    }
    host_cpu = unfiltered(src.query(HOSTS[project], 'core-p0'))
    host_memory = src.queries(HOSTS[project], 'core-p1')
    up = src.query('monitoring-health', 'overview-p0')
    host = {
        'host-cpu': chart('CPU 使用率（%）', [(node_role(host_cpu, project), '{{role}} · {{node}}')],
                          '主机 CPU 忙碌比例，按节点分线。负载、I/O 等待与磁盘网络见「主机资源」。'),
        'host-memory': chart('主机内存（GiB）', [(node_role(unfiltered(s['query']), project), '{{role}} · {{node}} ' + name)
                                               for s, name in zip(host_memory, ('已用', '总量'))],
                             '主机已用内存与总量，按节点分线。'),
        'scrape-down': chart('异常采集目标数（个）', [(f'count(({up}) == 0) or (count({up}) * 0)', '异常目标')],
                             '本环境当前抓取失败的采集目标数；0 为正常，没有任何新鲜采集时留空。具体目标见「采集与监控健康」。'),
    }
    d = {'kind': 'Dashboard', 'metadata': {'name': NAME, 'project': project}, 'spec': {
        'display': {'name': '总览', 'description': '一眼定位：每图按角色或节点聚合，最多 4 条线；发现异常后到对应看板查看明细。由生成器根据明细面板重建。'},
        'duration': '1h', 'refreshInterval': '15s', 'variables': [], 'panels': {}, 'layouts': []}}
    for title, group in (('网关', gateway), ('后端', backend), ('缓存与加速卡', hardware), ('主机与采集', host)):
        d['spec']['panels'].update(group)
        d['spec']['layouts'].append({'kind': 'Grid', 'spec': {'display': {'title': title}, 'items': [
            {'x': i % 2 * 12, 'y': i // 2 * 8, 'width': 12, 'height': 8, 'content': {'$ref': '#/spec/panels/' + key}}
            for i, key in enumerate(group)]}})
    assert not VARIABLE.search('\n'.join(q['spec']['plugin']['spec']['query'] for p in d['spec']['panels'].values() for q in p['spec']['queries']))
    assert all(env in q['spec']['plugin']['spec']['query'] for p in d['spec']['panels'].values() for q in p['spec']['queries'])
    return d


def apply(resources):
    result = dict(resources)
    dashboards = [d for d in resources['dashboards'] if d['metadata']['name'] != NAME]
    projects = [p for p in NODES if any(d['metadata']['project'] == p for d in dashboards)]
    result['dashboards'] = dashboards + [build(p, dashboards) for p in projects]
    return copy.deepcopy(result)
