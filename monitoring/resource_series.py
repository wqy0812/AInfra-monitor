"""Bounded numeric resource series derived from fresh monitoring observations."""
import math
from urllib.parse import quote


def key(label):
    return quote(str(label), safe='').replace('.', '%2E')


def resource_values(telemetry, metric):
    result = {}

    def add(group, label, value, scale=1):
        try:
            value = float(value) if value is not None and value != '' else None
        except (TypeError, ValueError):
            value = None
        result.setdefault(group, {})[key(label)] = value * scale if value is not None and math.isfinite(value) else None

    for gpu in (telemetry.get('gpus') or [])[:32]:
        device = str(gpu.get('device', '未知设备'))
        for group, field, scale in [('gpu_utilization', 'HCU use (%)', 1), ('gpu_temperature', 'Temperature (Sensor junction) (C)', 1), ('gpu_power', 'Average Graphics Package Power (W)', 1)]:
            add(group, device, gpu.get(field), scale)
        for field, label in [('vram Total Used Memory (MiB)', '已用'), ('vram Total Memory (MiB)', '总量')]:
            add('gpu_memory', device + ' · ' + label, gpu.get(field), 1 / 1024)
        try:
            used, total = float(gpu.get('vram Total Used Memory (MiB)')), float(gpu.get('vram Total Memory (MiB)'))
            ratio = used / total * 100 if total > 0 else None
        except (TypeError, ValueError):
            ratio = None
        add('gpu_memory_ratio', device, ratio)
    add('service_requests', '累计请求数', metric.get('requests') if metric.get('request_scope') == 'all' else None)
    for row in (metric.get('rank_gauges') or [])[:256]:
        name = row.get('name', '').removeprefix('sglang:')
        group = {'num_running_reqs': 'queue', 'num_queue_reqs': 'queue', 'token_usage': 'token_usage', 'cache_hit_rate': 'rank_cache', 'hicache_host_used_tokens': 'hicache_tokens', 'hicache_host_total_tokens': 'hicache_tokens'}.get(name)
        if not group:
            continue
        label = {'num_running_reqs': '运行', 'num_queue_reqs': '排队', 'hicache_host_used_tokens': '已用', 'hicache_host_total_tokens': '总量'}.get(name, '')
        ranks = ' / '.join(k + '=' + str(v) for k, v in sorted((row.get('labels') or {}).items()) if k != 'model_name')
        add(group, ranks + (' · ' + label if label else ''), row.get('value'), 100 if group in ('token_usage', 'rank_cache') else 1)
    for source, value in list((metric.get('cache_sources') or {}).items())[:32]:
        add('cache_tokens', source, value)
    for rank in ((metric.get('hicache') or {}).get('ranks') or [])[:256]:
        label = ' / '.join(k + '=' + str(v) for k, v in sorted((rank.get('labels') or {}).items()) if k != 'model_name')
        add('hicache_rank_ratio', label, rank.get('ratio'), 100)
    return result


def store_resources(store):
    result = {'segments': {}, 'segment_ratio': {}}
    for segment in (store.get('segments') or [])[:64]:
        label = str(segment.get('segment', '未知存储段'))
        for field, title in [('used', '已用'), ('total', '总量')]:
            value = segment.get(field)
            result['segments'][key(label + ' · ' + title)] = value / 2**30 if isinstance(value, (int, float)) and math.isfinite(value) else None
        value = segment.get('ratio')
        result['segment_ratio'][key(label)] = value * 100 if isinstance(value, (int, float)) and math.isfinite(value) else None
    return result


def resource_paths(point):
    paths = []
    for root in ('nodes.prefill', 'nodes.decode', 'mooncake'):
        obj = point
        for part in root.split('.'):
            obj = obj.get(part) or {}
        for group, values in (obj.get('resources') or {}).items():
            for label in values:
                paths.append(root + '.resources.' + group + '.' + label)
    return paths
