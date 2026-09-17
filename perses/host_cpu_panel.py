"""Read CPU materialized by monitoring-api; preserve other dashboard content."""
import copy
import json

SCHEMA = 'host-cpu-v1'


def expression(field='cpu'):
    assert field in ('cpu', 'cpu_iowait')
    labels = '{environment="a3-vllm",schema="' + SCHEMA + '",path=~' + json.dumps(r'nodes\.(prefill|decode)\.' + field) + ',node=~"$node"}'
    value, valid = 'monitoring_chart_value' + labels, 'monitoring_chart_valid' + labels
    return (f'({value} and ({valid} == 1) and (min_over_time({valid}[$__interval]) == 1)'
            f' and (count_over_time({valid}[$__interval]) >= ($__interval / 5))'
            f' and (time() - timestamp({value}) >= 0) and (time() - timestamp({value}) < 15))')


def extend(document):
    d = copy.deepcopy(document)
    assert (d['metadata']['project'], d['metadata']['name']) == ('a3-monitoring', 'a3-hosts')
    for key, title, field, meaning in [
        ('p0', '主机 CPU', 'cpu', '保持最近 1 分钟的 CPU 忙碌比例口径，扣除 idle 和 iowait。'),
        ('extra-iowait', 'CPU I/O 等待', 'cpu_iowait', '保持各逻辑 CPU 最近 1 分钟 iowait 增长率的平均比例；与主机忙碌率分别计算。'),
    ]:
        p = d['spec']['panels'][key]['spec']
        assert p['display']['name'] == title and len(p['queries']) == 1
        q = p['queries'][0]['spec']['plugin']['spec']
        q['query'] = expression(field)
        q['seriesNameFormat'] = '{{node}}'
        note = ('\n\nCPU 数据来源\nmonitoring-api 每 5 秒计算新增时间点并保存主机聚合值；' + meaning +
                '缺样、重置、重启、过期或计算失败时留空。聚合历史从发布后开始积累。')
        p['display']['description'] = p['display'].get('description', '').split('\n\nCPU 数据来源\n')[0] + note
    return d
