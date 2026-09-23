"""XPU PD adapter. Native prefix hit rate is supported; hardware and cache tiers are not integrated."""
import copy
from .replay import replay as sglang_replay, latest_rows
from . import xpu_cache


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
        for role, node in snap['nodes'].items():
            node['telemetry'] = {'status': 'not_integrated', 'data': {}, 'reason': '硬件监控暂未接入'}
            data = node.get('metrics', {}).get('data')
            if data is not None:
                native = xpu_cache.from_rows(latest_rows(groups, 'sglang-prefill', snap['ts'])[1]) if role == 'prefill' else None
                data.update(hicache=None, cache_60s=native, cache_hit_ratio=native['ratio'] if native else None, cache_sources={}, cache_schema=xpu_cache.SCHEMA if native else 'unavailable')
                data['rank_gauges'] = [r for r in data.get('rank_gauges', []) if r['name'] not in ('sglang:hicache_host_used_tokens', 'sglang:hicache_host_total_tokens')]
    for point in points:
        point['environment'] = 'xpu-pd'
        point['mooncake'] = {k: None for k in ('capacity', 'ssd_capacity', 'query_60s', 'tier_query_60s')}
        point['mooncake']['resources'] = {}
        for role, node in point['nodes'].items():
            native = xpu_cache.from_rows(latest_rows(groups, 'sglang-prefill', point['ts'])[1]) if role == 'prefill' else None
            node.update(cpu=None, hicache=None, cache_60s=native)
            for group in ('rank_cache', 'cache_tokens', 'hicache_tokens', 'hicache_rank_ratio'):
                node.get('resources', {}).pop(group, None)
    return snaps, points
