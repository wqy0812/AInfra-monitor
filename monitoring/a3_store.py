"""A3 Master capacity observations, independent of vLLM engine health."""
import bisect

from .cache_metrics import capacity, finite

JOB = 'mooncake-a3'
METRICS = ('master_allocated_bytes', 'master_total_capacity_bytes',
           'master_allocated_file_size_bytes', 'master_total_file_capacity_bytes')
UNAVAILABLE = 'A3 未暴露 Store 内存/SSD 分层查询计数'


def observe(groups, tick):
    sources = []
    for identity, (times, samples) in groups.items():
        if identity[0] != JOB:
            continue
        index = bisect.bisect_right(times, tick) - 1
        if index >= 0 and 0 <= tick - times[index] < 10:
            sources.append((times[index], samples[times[index]]))
    ts, rows = sources[0] if len(sources) == 1 else (None, [])
    healthy = [r['value'] for r in rows if r['name'] == 'up'] == [1]

    def single(name):
        values = [r['value'] for r in rows if r['name'] == name]
        return values[0] if healthy and len(values) == 1 and finite(values[0]) else None

    data = {
        'capacity': capacity(single(METRICS[0]), single(METRICS[1])),
        'ssd_capacity': capacity(single(METRICS[2]), single(METRICS[3])),
        'query_60s': {'ratio': None, 'reason': UNAVAILABLE},
        'tier_query_60s': {'memory': None, 'ssd': None, 'reason': UNAVAILABLE},
    }
    for key in ('capacity', 'ssd_capacity'):
        valid_zero = data[key]['used'] == data[key]['total'] == 0
        if data[key]['reason'] and not valid_zero:
            data[key].update(used=None, total=None)
    available = sum(data[key]['used'] is not None and data[key]['total'] is not None
                    for key in ('capacity', 'ssd_capacity'))
    status = 'ok' if healthy and available == 2 else 'partial' if healthy and available else 'error'
    return {'status': status, 'observed_at': ts, 'data': data,
            'error': None if status == 'ok' else 'Mooncake 容量指标缺失、重复或已过期'}
