"""XPU exporter gauges, using observed MiB / percent / Celsius / Watt units."""
import copy

PANELS = {
    'p5': ('utilization', '%', 1, 100),
    'p6': ('memused', 'GiB', 1024, None),
    'p7': ('temp', '°C', 1, None),
    'p8': ('powerUsage', 'W', 1, None),
    'extra-vram-total': ('memtotal', 'GiB', 1024, None),
    'extra-vram-ratio': ('memutil', '%', 1, 100),
}


def expression(metric, divisor=1, maximum=None):
    selector = '{environment="xpu-pd",job="xpu-hardware",node=~"$node",devid=~"$device"}'
    source = 'node_xpu_' + metric + selector
    up = 'up{environment="xpu-pd",job="xpu-hardware",node=~"$node"}'
    valid = f'({source} >= 0)'
    if maximum is not None:
        valid += f' and ({source} <= {maximum})'
    valid += (f' and (time() - timestamp({source}) < 15)'
              f' and (count_over_time({source}[$__interval]) >= ($__interval / 5))'
              f' and on(job,instance,environment) (({up} == 1)'
              f' and (time() - timestamp({up}) < 15)'
              f' and (min_over_time({up}[$__interval]) == 1)'
              f' and (count_over_time({up}[$__interval]) >= ($__interval / 5)))')
    return '(' + valid + ')' + (f' / {divisor}' if divisor != 1 else '')


def configure(source):
    document = copy.deepcopy(source)
    document['spec']['display']['description'] = (
        'XPU 主机指标来自 Node Exporter，卡硬件来自 xpu_exporter；按节点和卡号分线。'
        '缺失、采集失败或过期观测留空，有效零值保留；默认 1 小时 / 15 秒刷新。')
    for key, (metric, unit, divisor, maximum) in PANELS.items():
        panel = document['spec']['panels'][key]['spec']
        assert len(panel['queries']) == 1, key
        query = panel['queries'][0]['spec']['plugin']['spec']
        query['query'] = expression(metric, divisor, maximum)
        query['seriesNameFormat'] = '{{node}} · XPU {{devid}}'
        panel['display']['description'] = (
            f'指标：node_xpu_{metric}，单位：{unit}。按节点与卡号 devid 展示瞬时观测。'
            + ('Exporter 的 MiB 除以 1024 转为 GiB。' if divisor == 1024 else '')
            + ('直接使用 exporter 的百分数，不再次乘以 100。' if unit == '%' else '')
            + '每 5 秒采集，默认每 15 秒刷新。有效零值保留；负值、缺样、超过 15 秒的采样和失败抓取留空。'
            + ('超过 100% 的值留空。' if maximum else '')
            + '新鲜度依据抓取时间，exporter 未提供独立设备观测时间。')
    for variable in document['spec']['variables']:
        spec = variable['spec']
        if spec['name'] == 'device':
            spec['defaultValue'] = '.*'
            spec['plugin'] = {'kind': 'StaticListVariable', 'spec': {'values':
                [{'label': '全部', 'value': '.*'}] +
                [{'label': 'XPU ' + str(i), 'value': str(i)} for i in range(8)]}}
    return document
