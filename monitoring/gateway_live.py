"""Read-only gateway history; query semantics match Perses gateway_live.py.

Keep this small runtime module independent of the dashboard tooling. Tests compare
both expressions to the checked-in project dashboards to prevent semantic drift.
"""
import asyncio
import json
import math

import httpx

STAGES = {
    'routing_discovery': '路由与模型发现',
    'preparing_upstream': '构造下游请求', 'waiting_upstream_headers': '等待下游响应头',
    'waiting_first_output': '等待首个有效输出', 'streaming': '读取后续流',
    'reading_response': '读取完整响应', 'writing_client': '向客户端写入',
    'finishing_stream': '流结束收尾',
}
IDLE = 'stream_idle_max_seconds'
OLDEST = 'oldest_age_seconds'
BACKEND_WAIT = 'backend_wait_max_seconds'
WRITE_ACTIVE = 'write_active_max_seconds'
FIELDS = (IDLE, OLDEST, BACKEND_WAIT, WRITE_ACTIVE)


def selector(metric, environment):
    scope = 'streaming' if metric.startswith('aigate_stream_') else 'all'
    extra = ',request_scope=' + json.dumps(scope) if metric.startswith('aigate_') else ''
    return metric + '{job="aigate",environment=' + json.dumps(environment) + extra + '}'


def source_gate(environment):
    up = selector('up', environment)
    boot = selector('aigate_profile_start_time_seconds', environment)
    return (f'({up} == 1) and (time() - timestamp({up}) < 15) '
            f'and (min_over_time({up}[$__interval]) == 1) and (count_over_time({up}[$__interval]) >= ($__interval / 5)) '
            f'and on(job,instance,environment) ((time() - timestamp({boot}) < 15) '
            f'and (time() - {boot} >= $__interval) and (changes({boot}[$__interval]) == 0) '
            f'and (count_over_time({boot}[$__interval]) >= ($__interval / 5)))')


def complete_gauge(metric, environment):
    s = selector(metric, environment)
    groups = selector('aigate_live_backend_groups', environment)
    width = len(STAGES) if metric == 'aigate_inflight_oldest_stage' else 1
    inventory = (f'(count by(job,instance,environment) ({s} and (time() - timestamp({s}) < 15)) '
                 f'== on(job,instance,environment) ({groups} * {width})) '
                 f'and on(job,instance,environment) (time() - timestamp({groups}) < 15) '
                 f'and on(job,instance,environment) (count_over_time({groups}[$__interval]) >= ($__interval / 5))')
    good = (f'(count_over_time({s}[$__interval]) >= ($__interval / 5)) '
            f'and (time() - timestamp({s}) < 15) '
            f'and on(job,instance,environment) ({inventory}) '
            f'and on(job,instance,environment) ({source_gate(environment)})')
    bad = f'(count_over_time({s}[$__interval]) unless ({good}))'
    return f'({s} and ({good})) unless on(environment) (count by(environment) ({bad}))'


def oldest(environment):
    age = complete_gauge('aigate_inflight_oldest_age_seconds', environment)
    stages = complete_gauge('aigate_inflight_oldest_stage', environment)
    stage_samples = selector('aigate_inflight_oldest_stage', environment)
    joined = (f'(topk by(environment) (1, ({age}) > 0)) '
              f'* on(job,instance,environment,backend) group_left(stage) '
              f'((({stages}) == 1) and (min_over_time({stage_samples}[$__interval]) == 1))')
    for stage, label in STAGES.items():
        joined = f'label_replace({joined}, "stage_name", {json.dumps(label, ensure_ascii=False)}, "stage", "{stage}")'
    idle = f'label_replace((max by(environment) ({age})) == 0, "stage_name", "无在途请求", "environment", ".*")'
    return f'({joined}) or ({idle})'


def expressions(environment, step):
    if environment not in ('dcu-pd', 'a3-vllm', 'xpu-pd') or not isinstance(step, int) or step < 5 or step % 5:
        raise ValueError('Invalid gateway history environment or step')
    queries = {
        IDLE: f'max by(environment) ({complete_gauge("aigate_stream_idle_max_seconds", environment)})',
        OLDEST: oldest(environment),
        BACKEND_WAIT: f'max by(environment) ({complete_gauge("aigate_stream_backend_wait_max_seconds", environment)})',
        WRITE_ACTIVE: f'max by(environment) ({complete_gauge("aigate_stream_write_active_max_seconds", environment)})',
    }
    return {key: query.replace('$__interval', f'{step}s') for key, query in queries.items()}


def samples(rows, environment, field, start, end):
    """Retain measured zero; reject non-finite, ambiguous or unlabelled samples."""
    result, seen = {}, set()
    for row in rows:
        labels = row['metric']
        if labels.get('environment') != environment:
            continue
        for raw_ts, raw_value in row['values']:
            ts, value = float(raw_ts), float(raw_value)
            if not math.isfinite(ts) or not start <= ts <= end:
                continue
            if ts in seen:
                result.pop(ts, None)
                continue
            seen.add(ts)
            if not math.isfinite(value) or value < 0:
                continue
            data = {field: value}
            if field == OLDEST:
                backend, stage = labels.get('backend'), labels.get('stage')
                if value > 0 and (not backend or stage not in STAGES):
                    continue
                data.update(backend=backend if value > 0 else None,
                            stage=stage if value > 0 else None,
                            stage_name=STAGES.get(stage) if value > 0 else '无在途请求')
            result[ts] = data
    return result


async def history(query, environment, start, end, step, timeout=4):
    async def fetch(field, expression):
        try:
            # Finish before the enclosing history deadline; a gateway outage
            # must not discard the successful backend/cache response.
            async with asyncio.timeout(timeout):
                rows = await query(expression, start, end, step)
                return field, samples(rows, environment, field, start, end), 'ok'
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
            return field, {}, 'unavailable'

    results = await asyncio.gather(*(fetch(field, expr) for field, expr in expressions(environment, step).items()))
    points, status = {}, {}
    for field, values, state in results:
        status[field] = state
        for ts, data in values.items():
            points.setdefault(ts, {}).update(data)
    return points, status


def attach(points, gateway, step):
    """Annotate identity changes and missing display intervals, without padding."""
    previous = None
    for point in points:
        ts = point['ts']
        data = {**dict.fromkeys(FIELDS), 'backend': None, 'stage': None, 'stage_name': None,
                **gateway.get(ts, {}), 'gap_before': []}
        for field in FIELDS:
            contiguous = previous and 0 < ts - previous['ts'] <= step + .001
            if (not contiguous or data[field] is None or previous['gateway'][field] is None
                    or (field == OLDEST and any(data[k] != previous['gateway'][k] for k in ('backend', 'stage')))):
                data['gap_before'].append(field)
        point['gateway'] = data
        previous = point
