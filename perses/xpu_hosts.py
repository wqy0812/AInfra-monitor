"""Adapt a live DCU host dashboard and attach measured XPU card gauges."""
import json
import re

EMPTY = 'vector(0) unless on() vector(0)'


def configure(source):
    text = json.dumps(source, ensure_ascii=False)
    for old, new in [('dcu-monitoring', 'xpu-monitoring'), ('hosts-dcu', 'hosts-xpu'),
                     ('dcu-pd', 'xpu-pd'), ('dcu1', 'xpu-2'), ('dcu2', 'xpu-1'), ('DCU', 'XPU')]:
        text = text.replace(old, new)
    document = json.loads(text)
    document['metadata'] = {'name': 'hosts-xpu', 'project': 'xpu-monitoring'}
    document['spec']['display']['description'] = (
        'XPU 主机指标来自 Node Exporter；主机图表布局、单位与统计口径对齐 DCU。'
        '卡硬件暂未接入。缺失或无效观测留空；默认 1 小时 / 15 秒刷新。')
    for panel in document['spec']['panels'].values():
        queries = panel['spec']['queries']
        hardware = any(re.search(r'\bdcu_\w+', q['spec']['plugin']['spec']['query']) for q in queries)
        if hardware:
            panel['spec']['display']['description'] = 'XPU 卡硬件监控暂未接入；本面板有意留空，No data 不表示零。'
        for query in queries:
            spec = query['spec']['plugin']['spec']
            if hardware:
                spec['query'] = EMPTY
                continue
            def selector(match):
                labels = re.sub(r'job\s*(?:=~|!~|!=|=)\s*"[^"]*"\s*,?', '', match.group(1))
                labels = labels.strip(',')
                return '{' + labels + ',job="node-xpu"}'
            spec['query'] = re.sub(r'\{([^{}]*)\}', selector, spec['query'])
    for variable in document['spec'].get('variables', []):
        spec = variable['spec']
        if spec['name'] == 'node':
            values = [{'label': '全部', 'value': '.*'},
                      {'label': 'Prefill / xpu-2', 'value': 'xpu-2'},
                      {'label': 'Decode / xpu-1', 'value': 'xpu-1'}]
        else:
            values = [{'label': '全部（卡硬件未接入）', 'value': '.*'}]
        spec['defaultValue'] = '.*'
        spec['plugin'] = {'kind': 'StaticListVariable', 'spec': {'values': values}}
    from xpu_hardware import configure as configure_hardware
    return configure_hardware(document)
