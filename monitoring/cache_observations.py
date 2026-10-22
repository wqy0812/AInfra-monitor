"""Report observations: existing native trends plus disjoint counter buckets.

Only bounded VM rollup summaries leave VM, not raw scrape histories. Each
counter's baseline, reset and freshness are checked before tier aggregation.
"""
import asyncio
import json
import math
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import cache_sources, native_schema, source_registry

router = APIRouter()
MAX_SPAN = 6 * 3600
ROLLUPS = ('first_over_time', 'last_over_time', 'min_over_time', 'max_over_time', 'count_over_time',
           'tfirst_over_time', 'tlast_over_time')


class ResolveInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    backend_url: str = Field(max_length=2048)
    proxy_url: str = Field(max_length=2048)
    at: float


class ObservationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    snapshot: dict
    start: float
    end: float
    offset_seconds: int = Field(default=0, ge=0, le=604800)
    bucket_seconds: int = Field(default=30, ge=30, le=30)


def finite(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def indexed(rows, increments=False):
    result = {}
    for row in rows:
        labels = dict(row['metric'])
        rollup = 'increase' if increments else labels.pop('rollup')
        identity = tuple(sorted(labels.items()))
        for ts, value in row['values']:
            result.setdefault(float(ts), {}).setdefault(identity, {})[rollup] = finite(value)
    return result


def selectors(environment):
    original=environment
    environment=source_registry.adapter_environment(environment)
    if environment == 'a3-vllm':
        scope = 'environment="a3-vllm",job="vllm-a3",node="a3-1"'
        names = 'vllm:prompt_tokens(_by_source|_cached)?_total'
    else:
        scope = 'environment=' + json.dumps(environment) + ',job="sglang-prefill"'
        names = 'sglang:cache_hit_rate' if environment == 'xpu-pd' else 'sglang:prefill_effective_tokens_total'
    extra = ',tp_rank="0",pp_rank="0"' if environment == 'xpu-pd' else ',tp_rank=~"0|",pp_rank=~"0|",moe_ep_rank=~"0|"' if environment == 'dcu-pd' else ''
    return tuple(source_registry.rewrite_query(value, original) for value in
                 ('{__name__=~"' + names + '",' + scope + extra + '}', 'up{' + scope + '}'))


def usable(stats, right, width=30):
    return (stats.get('count_over_time', 0) is not None and stats.get('count_over_time', 0) >= width / 5
            and stats.get('tlast_over_time') is not None and 0 <= right - stats['tlast_over_time'] <= 10
            and stats.get('tfirst_over_time') is not None and 0 <= stats['tfirst_over_time'] - (right - width) <= 10)


def delta(stats, baseline, right):
    if not usable(stats, right) or baseline.get('tlast_over_time') is None or not 0 <= right - 30 - baseline['tlast_over_time'] <= 10:
        return None
    first, last, minimum, increase = (stats.get(k) for k in ('first_over_time', 'last_over_time', 'min_over_time', 'increase'))
    base = baseline.get('last_over_time')
    if any(v is None or v < 0 for v in (first, last, minimum, increase, base)):
        return None
    if minimum < base or not math.isclose(increase, last-first, rel_tol=1e-9, abs_tol=1e-6):
        return None
    return last - base


def bucket_counts(environment, current, baseline, health, right):
    environment=source_registry.adapter_environment(environment)
    healthy = {dict(key).get('instance', '') for key, stats in health.items()
               if usable(stats, right) and stats.get('min_over_time') == 1}
    groups = {}
    for key, stats in current.items():
        labels = dict(key)
        instance = labels.get('instance', '')
        if instance not in healthy:
            continue
        value = delta(stats, baseline.get(key, {}), right)
        identity = tuple(sorted((k, v) for k, v in labels.items() if k not in ('__name__', 'mode', 'source')))
        if environment == 'dcu-pd':
            field = labels.get('mode')
        else:
            field = labels.get('source') if labels['__name__'] == 'vllm:prompt_tokens_by_source_total' else labels['__name__']
        group = groups.setdefault(identity, {})
        # Duplicate label partitions must not silently inflate token counts.
        group[field] = None if field in group else value
    totals = dict(input_tokens=0, hit_tokens=0, device=0, host=0, storage=0)
    available = set()
    accepted = 0
    for identity, group in groups.items():
        if environment == 'dcu-pd':
            if set(group) != {'device_hit', 'host_hit', 'storage_hit', 'input'} or any(v is None for v in group.values()):
                continue
            device, host, storage = (group[k] for k in ('device_hit', 'host_hit', 'storage_hit'))
            inputs = device + host + storage + group['input']
        else:
            required = {'local_compute', 'local_cache_hit', 'external_kv_transfer', 'vllm:prompt_tokens_total', 'vllm:prompt_tokens_cached_total'}
            if set(group) != required or any(v is None for v in group.values()):
                continue
            device, host, storage = group['local_cache_hit'], 0, group['external_kv_transfer']
            inputs = group['vllm:prompt_tokens_total']
            if not math.isclose(inputs, device+storage+group['local_compute']) or not math.isclose(device+storage, group['vllm:prompt_tokens_cached_total']):
                continue
        available.add(dict(identity).get('instance', ''))
        accepted += 1
        for k, v in dict(input_tokens=inputs, hit_tokens=device+host+storage, device=device, host=host, storage=storage).items():
            totals[k] += v
    return totals if available else None, len(available), accepted != len(groups) or set(current) != set(baseline)


async def observe(service, snapshot, start, end, offset):
    # VM evaluates query_range timestamps at millisecond precision; replay
    # clocks retain micro/nanoseconds. Normalize both sides before indexing.
    start, end = round(start, 3), round(end, 3)
    environment = snapshot['source']['environment']
    baseline_environment=source_registry.adapter_environment(environment)
    metric, up = selectors(environment)
    def stats(selector, width):
        return 'aggr_over_time((' + ','.join(json.dumps(r) for r in ROLLUPS) + '),' + selector + '[' + str(width) + 's]) keep_metric_names'
    async def query(expression, left=start+30, right=end):
        # VM aligns ranges with >=50 points to epoch multiples of step when
        # response caching is enabled. Reports require their actual start's
        # fractional offset, including for counter baselines and trend centers.
        return await service.query(expression, left, right, 30, nocache=True)
    # Counter baselines use actual samples at/before the left boundary. Range
    # summaries are strictly (left,right], avoiding overlap across buckets.
    calculation = 'avg_over_time' if baseline_environment == 'xpu-pd' else 'increase_prometheus'
    tasks = [query(stats(metric, 30)), query(stats(metric, 15), start, end-30),
             query(stats(up, 30)), query(calculation+'('+metric+'[30s]) keep_metric_names')]
    from .api import history_expression
    tasks.append(query(history_expression(environment, 'summary'), start+15, end-15))
    pending = [asyncio.create_task(task) for task in tasks]
    try:
        raw, base, up_rows, extra, trend_rows = await asyncio.gather(*pending)
    finally:
        for task in pending:
            if not task.done():task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    current, baseline, health = indexed(raw), indexed(base), indexed(up_rows)
    if baseline_environment != 'xpu-pd':
        for ts, rows in indexed(extra, increments=True).items():
            for key, values in rows.items():current.setdefault(ts, {}).setdefault(key, {}).update(values)
    averages = {float(ts): finite(v) for row in extra for ts, v in row['values']} if baseline_environment == 'xpu-pd' and len(extra) == 1 else {}
    trends = {p['ts']: p['nodes']['prefill'] for p in native_schema.points(trend_rows, environment, start, end, 30, summary=True)}
    points = []
    for index in range(round((end-start)/30)):
        right = start+(index+1)*30
        node = trends.get(right-15, {})
        cache = node.get('cache_60s', {})
        cache = cache.get('effective', {}) if baseline_environment == 'a3-vllm' else cache
        point = {'offset_seconds': offset+index*30, 'width_seconds': 30,
                 'trend': {k: cache.get(k) for k in ('ratio', 'device', 'host', 'storage')},
                 'expected_instances': node.get('coverage', {}).get('expected'),
                 'counts': None, 'available_instances': 0, 'valid_seconds': 0}
        if baseline_environment == 'xpu-pd':
            rows = list(current.get(right, {}).values())
            ups = list(health.get(right, {}).values())
            valid = len(rows) == 1 and usable(rows[0], right) and rows[0].get('min_over_time', -1) >= 0 and rows[0].get('max_over_time', 2) <= 1
            valid = valid and len(ups) == 1 and usable(ups[0], right) and ups[0].get('min_over_time') == 1
            value = averages.get(right) if valid else None
            if value is not None and 0 <= value <= 1:
                point.update(ratio_seconds=value*30, valid_seconds=30, available_instances=1)
        else:
            counts, available, partial = bucket_counts(environment, current.get(right, {}), baseline.get(right-30, {}), health.get(right, {}), right)
            point.update(counts=counts, available_instances=available, valid_seconds=30 if counts else 0, partial=partial)
        if not point['valid_seconds']:
            point['reason'] = 'missing_or_inconsistent_cache_observation'
        points.append(point)
    valid = sum(p['valid_seconds'] for p in points)
    partial = any(p.get('partial') or p['expected_instances'] is None or p['available_instances'] != p['expected_instances'] for p in points)
    return {'schema': cache_sources.SCHEMA, 'status': 'complete' if valid == end-start and not partial else 'partial' if valid else 'unavailable',
            'reason': None if valid else 'no_valid_cache_observation', 'points': points,
            'source': snapshot['source'], 'computed_at': time.time(), 'boundary_approximation_seconds': 10,
            'trend_window_seconds': None if baseline_environment == 'xpu-pd' else 60}


@router.post('/api/monitoring/cache-sources/resolve')
async def resolve_source(body: ResolveInput):
    try:return cache_sources.resolve(body.model_dump())
    except (ValueError, KeyError, OSError):raise HTTPException(400, 'invalid_cache_source_request')


@router.post('/api/monitoring/cache-observations')
async def observations(body: ObservationInput, request: Request):
    now = time.time()
    if not all(math.isfinite(v) for v in (body.start, body.end)) or not now-720*3600 <= body.start < body.end <= now-15:
        raise HTTPException(400, 'cache_observation_window_unavailable')
    if body.end-body.start > MAX_SPAN or abs((body.end-body.start)/30-round((body.end-body.start)/30)) > 1e-5:
        raise HTTPException(400, 'invalid_cache_observation_window')
    try:
        definition = cache_sources.validate_snapshot(body.snapshot, body.start, body.end)
        service = request.app.state.services[definition['environment']]
        async with asyncio.timeout(20), service.slots, service.client.scope():
            return await observe(service, body.snapshot, body.start, body.end, body.offset_seconds)
    except (KeyError, ValueError) as exc:raise HTTPException(409, str(exc))
    except (TimeoutError, httpx.HTTPError):raise HTTPException(503, 'cache_observations_unavailable')
