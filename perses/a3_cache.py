"""Presentation policy for existing A3 prefix-cache ratios; no query changes."""
import copy


def configure(document):
    if (document['metadata'].get('project'), document['metadata']['name']) != ('a3-monitoring', 'a3-cache'):
        return document
    document = copy.deepcopy(document)
    panels = document['spec']['panels']
    definitions = (
        ('p1', '本地前缀缓存命中率（%）', '本地', 'ratio'),
        ('p2', '外部 KV 前缀缓存命中率（%）', '外部 KV', 'external_ratio'),
    )
    for key, title, label, path in definitions:
        panel = panels[key]['spec']
        assert all('cache_60s.' + path in q['spec']['plugin']['spec']['query'] for q in panel['queries'])
        old = panel['display'].get('description', '')
        coverage = old[old.index('\n\nA3 引擎采集范围'):] if '\n\nA3 引擎采集范围' in old else ''
        panel['display'] = {'name': title, 'description': (
            '最近约 60 秒' + label + '前缀命中 Token ÷ 对应查询 Token × 100%。'
            '每个角色合并实例时按 Token 加权，不平均实例百分比；本地与外部 KV 的分母不同，两种命中率不能相加。'
            '\n\n角色筛选支持 Prefill / Decode；图例路径中的 prefill、decode 表示角色，各角色分别展示。'
            'code-eval 实时监控使用 Prefill 口径。无查询、缺样、过期或计数重置留空，有效 0% 保留。'
            '\n\n外部 KV 表示连接器跨实例前缀共享，不代表 Mooncake 内存或 SSD 分层命中率。' + coverage)}
        panel['plugin']['spec']['yAxis'].update(min=0, max=100)
        for query in panel['queries']:
            query['spec']['plugin']['spec']['seriesNameFormat'] = label + ' · {{path}}'
    # Move the existing two panels to the first row, retaining all other panels.
    layout = next(layout for layout in document['spec']['layouts']
                  if any(item['content']['$ref'].endswith('/p1') for item in layout['spec']['items']))
    items = layout['spec']['items']
    by_key = {item['content']['$ref'].rsplit('/', 1)[1]: item for item in items}
    ordered = ['p1', 'p2'] + [key for key in by_key if key not in ('p1', 'p2')]
    for index, key in enumerate(ordered):
        by_key[key].update(x=index % 2 * 12, y=index // 2 * 8, width=12, height=8)
    assert 'p2' in by_key
    layout['spec']['items'] = [by_key[key] for key in ordered]
    from a3_mooncake import configure as configure_mooncake
    return configure_mooncake(document)
