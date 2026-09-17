"""Add A3 hardware panels without rewriting existing host panels."""
import copy
from generate import panel, variable
from project_queries import Queries, SOURCE


def gauge(metric, maximum=None):
    q = Queries('a3-vllm', 'npu-a3', ',node=~"$node"')
    s = q.s('npu_chip_info_' + metric, ',id=~"$device"')
    # The exporter supplies asynchronous device observation timestamps. Keep
    # those timestamps; an HTTP success must not refresh a frozen observation.
    bounds = f'({s} >= 0) and ({s} < +Inf)'
    if maximum is not None:
        bounds += f' and ({s} <= {maximum})'
    return (f'({s} and ({bounds}) and (time() - timestamp({s}) >= 0)'
            f' and (time() - timestamp({s}) < 15)'
            f' and on({SOURCE}) ({q.source()}))')


def extend(document):
    d = copy.deepcopy(document)
    assert d['metadata']['project'] == 'a3-monitoring'
    assert d['metadata']['name'] == 'a3-hosts'
    spec = d['spec']
    spec['display']['name'] = 'A3 · 主机与 NPU'
    device = variable('device', 'NPU 芯片', [{'value': '.*', 'label': '全部'}] +
                      [{'value': str(i), 'label': 'NPU ' + str(i)} for i in range(16)])
    spec['variables'] = [v for v in spec.get('variables', []) if v['spec']['name'] != 'device'] + [device]
    used, total = gauge('hbm_used_memory'), gauge('hbm_total_memory')
    valid_used = f'(({used}) <= ({total}))'
    definitions = [
        ('utilization', 'NPU 利用率', gauge('utilization', 100), '%',
         'AI Core 利用率，按 exporter 原始芯片 ID 分线；不与 DCU HCU 的硬件执行单元定义等同。'),
        ('memory-used', 'NPU 显存已用', valid_used + ' / 1024', 'GiB',
         '芯片 HBM 已用容量；源单位 MiB，除以 1024 转为 GiB。不是 vLLM KV Cache 占用。'),
        ('temperature', 'NPU 温度', gauge('temperature'), '°C', 'exporter 上报的芯片温度。'),
        ('power', 'NPU 功耗', gauge('power'), 'W',
         'exporter 按芯片 ID 上报的功耗读数，保留原始粒度，不跨芯片相加为整机功耗。'),
        ('memory-total', 'NPU 显存总量', f'(({total}) > 0) / 1024', 'GiB',
         '芯片 HBM 总容量；源单位 MiB，除以 1024 转为 GiB。'),
        ('memory-ratio', 'NPU 显存占用比例', f'100 * ({valid_used}) / (({total}) > 0)', '%',
         '同一节点、同一芯片 HBM 已用 / 总量；总量必须大于零且已用量不能超过总量。'),
    ]
    for suffix, title, query, unit, meaning in definitions:
        desc = (meaning + '\n\n按 A3 节点及 NPU 芯片 ID 展示，每节点 16 个芯片。'
                '\n每 5 秒抓取，保留 exporter 原始观测时间；观测超过 15 秒、抓取失败、'
                '缺失或非法数据留空，有效零值保留，曲线不跨空值连接。'
                '\n历史从接入时开始积累；默认 1 小时、15 秒刷新。')
        p = panel(title, [(query, '{{node}} · NPU {{id}}')], unit, desc)
        if unit == '%':
            p['spec']['plugin']['spec']['yAxis']['max'] = 100
        spec['panels']['npu-' + suffix] = p
    # Place hardware alongside the five basic host panels, before diagnostics.
    items = spec['layouts'][0]['spec']['items']
    keys = [x['content']['$ref'].split('/')[-1] for x in items
            if not x['content']['$ref'].split('/')[-1].startswith('npu-')]
    pivot = next((i for i, k in enumerate(keys) if k.startswith('extra-')), len(keys))
    keys[pivot:pivot] = ['npu-' + x[0] for x in definitions]
    spec['layouts'][0]['spec']['items'] = [
        {'x': i % 2 * 12, 'y': i // 2 * 8, 'width': 12, 'height': 8,
         'content': {'$ref': '#/spec/panels/' + k}} for i, k in enumerate(keys)]
    return d
