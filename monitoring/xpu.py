"""XPU PD adapter. Hardware and cache tiers are intentionally not integrated."""
import copy
from .replay import replay as sglang_replay


def replay(groups, start, end):
    groups = copy.deepcopy(groups)
    # xSGL exports engine_type=unified for both roles. The scrape job supplies
    # the actual deployment role; normalize only scheduler decode calculations.
    for rows in groups.get('sglang-decode', ([], {}))[1].values():
        for row in rows:
            if row['name'] in ('sglang:num_running_reqs', 'sglang:realtime_tokens_total') and row['labels'].get('engine_type') == 'unified':
                row['labels']['engine_type'] = 'decode'
    snaps, points = sglang_replay(groups, start, end)
    for snap in snaps:
        snap['environment'] = 'xpu-pd'
        snap['mooncake'] = {'status': 'not_integrated', 'data': {}, 'reason': '未接入缓存存储监控'}
        for node in snap['nodes'].values():
            node['telemetry'] = {'status': 'not_integrated', 'data': {}, 'reason': '硬件监控暂未接入'}
            data = node.get('metrics', {}).get('data')
            if data is not None:
                data.update(hicache=None, cache_60s=None, cache_hit_ratio=None, cache_sources={}, cache_schema='unavailable')
                data['rank_gauges'] = [r for r in data.get('rank_gauges', []) if r['name'] not in ('sglang:cache_hit_rate', 'sglang:hicache_host_used_tokens', 'sglang:hicache_host_total_tokens')]
    for point in points:
        point['environment'] = 'xpu-pd'
        point['mooncake'] = {k: None for k in ('capacity', 'ssd_capacity', 'query_60s', 'tier_query_60s')}
        point['mooncake']['resources'] = {}
        for node in point['nodes'].values():
            node.update(cpu=None, hicache=None, cache_60s=None)
            for group in ('rank_cache', 'cache_tokens', 'hicache_tokens', 'hicache_rank_ratio'):
                node.get('resources', {}).pop(group, None)
    return snaps, points
