"""XPU's native representative-rank prefix hit gauge, matching its Perses panel."""
import asyncio
import math
import httpx

SCHEMA = 'xpu-native-prefix-v1'
SCOPE = 'environment="xpu-pd",job="sglang-prefill"'
METRIC = 'sglang:cache_hit_rate{' + SCOPE + ',tp_rank="0",pp_rank="0"}'
UP = 'up{' + SCOPE + '}'


def cache(value=None):
    valid = isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
    return {'ratio': value if valid else None, 'semantics': SCHEMA,
            'reason': None if valid else '原生前缀缓存命中率缺失、过期或采集失败'}


def from_rows(rows):
    values = [r['value'] for r in rows if r['name'] == 'sglang:cache_hit_rate'
              and r['labels'].get('tp_rank') == '0' and r['labels'].get('pp_rank') == '0']
    return cache(values[0] if len(values) == 1 else None)


async def history(query, start, end, step, timeout=4):
    # Query retained raw gauges directly: earlier chart materialization blanked them.
    # Explicit freshness and scrape health prevent stale samples crossing outages.
    value = f'default_rollup({METRIC}[15s]) and on(instance) (default_rollup({UP}[15s]) == 1)'
    window = f'[{step}s]'
    continuous = (f'(min_over_time({UP}{window}) == 1)'
                  f' and (count_over_time({UP}{window}) >= {step // 5})'
                  f' and on(instance) (count_over_time({METRIC}{window}) >= {step // 5})'
                  f' and on(instance) (min_over_time({METRIC}{window}) >= 0)'
                  f' and on(instance) (max_over_time({METRIC}{window}) <= 1)')
    def index(series):
        result = {}
        for item in series:
            for ts, value in item['values']:
                result.setdefault(float(ts), []).append(float(value))
        return result
    async def fetch(expression):
        try:
            async with asyncio.timeout(timeout):
                return index(await query(expression, start, end, step))
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
            # Missing values stay blank; missing continuity prevents joining points.
            # Optional cache queries must not discard successful monitoring data.
            return {}
    values, continuity = await asyncio.gather(fetch(value), fetch(continuous))
    return values, continuity


def attach(points, result):
    values, continuity = result
    for point in points:
        node = point['nodes']['prefill'];ts = point['ts'];found = values.get(ts, [])
        node['cache_60s'] = from_value = cache(found[0] if len(found) == 1 else None)
        node['gap_before'] = [key for key in node.get('gap_before', []) if key != 'cache']
        if from_value['ratio'] is None or continuity.get(ts) != [1.0]:
            node['gap_before'].append('cache')
