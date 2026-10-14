"""Raw synthetic request observations. No interpolation or business-traffic SLOs."""
import asyncio
import json
import math
import time

from fastapi import APIRouter, HTTPException, Request
import httpx

INTERVAL = 60
STALE_AFTER = 120
TARGETS = (('a3-vllm', 'A3'), ('xpu-pd', 'XPU'), ('dcu-pd', 'DCU'))
OFFSETS = {environment: index * (INTERVAL // len(TARGETS)) for index, (environment, _) in enumerate(TARGETS)}
REASONS = ('success', 'http_error', 'timeout', 'connection_error', 'stream_error',
           'invalid_stream', 'empty_output', 'incomplete_stream')
FIELDS = ('result', 'started_at_seconds', 'duration_seconds', 'first_output_seconds', 'http_status')
PREFIX = 'model_probe_'
router = APIRouter()


def samples(environment, result):
    """All fields share one timestamp; a partially stored observation is unknown."""
    stamp = round(result['finished_at'] * 1000)
    values = (REASONS.index(result['reason']), result['started_at'], result['duration_seconds'],
              result['first_output_seconds'] if result['first_output_seconds'] is not None else -1,
              result['http_status'] or 0)
    return '\n'.join(json.dumps({'metric': {'__name__': PREFIX + field, 'environment': environment},
                                 'values': [value], 'timestamps': [stamp]})
                     for field, value in zip(FIELDS, values)) + '\n'


def decode(raw, start, end):
    observations = {environment: {} for environment, _ in TARGETS}
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        labels = row['metric']
        environment = labels.get('environment')
        field = labels.get('__name__', '').removeprefix(PREFIX)
        if environment not in observations or field not in FIELDS:
            continue
        if len(row['values']) != len(row['timestamps']):
            raise ValueError('Invalid probe samples')
        for ts, value in zip(row['timestamps'], row['values']):
            ts, value = float(ts) / 1000, float(value)
            if not math.isfinite(ts) or not start <= ts <= end:
                continue
            point = observations[environment].setdefault(ts, {})
            # Conflicting duplicate series must never produce a green result.
            if field in point and point[field] != value:
                point['invalid'] = True
            point[field] = value
    services = []
    for environment, name in TARGETS:
        records = []
        for ts, values in sorted(observations[environment].items()):
            valid = (not values.get('invalid') and all(k in values and math.isfinite(values[k]) for k in FIELDS))
            if valid:
                code, started, duration, first, status = (values[k] for k in FIELDS)
                valid = (code.is_integer() and 0 <= code < len(REASONS) and 0 < started <= ts + .001
                         and 0 <= duration and (first == -1 or 0 <= first <= duration)
                         and status.is_integer() and (status == 0 or 100 <= status <= 599)
                         and (code != 0 or (first >= 0 and 200 <= status < 300)))
            records.append({'finished_at': ts, 'started_at': started if valid else None,
                            'status': ('success' if code == 0 else 'failure') if valid else 'unknown',
                            'reason': REASONS[int(code)] if valid else 'incomplete_observation',
                            'duration_seconds': duration if valid else None,
                            'first_output_seconds': first if valid and first >= 0 else None,
                            'http_status': int(status) if valid and status else None})
        latest = records[-1] if records else None
        services.append({'environment': environment, 'name': name, 'latest': latest,
                         'offset_seconds': OFFSETS[environment],
                         'status': latest['status'] if latest and end-latest['finished_at'] <= STALE_AFTER else 'unknown',
                         'records': records})
    return {'source': 'victoriametrics', 'observed_at': end, 'start': start, 'end': end,
            'interval_seconds': INTERVAL, 'stale_after_seconds': STALE_AFTER, 'services': services}


async def history(client, vm, now=None):
    end = time.time() if now is None else now
    start = end - 86400
    response = await client.get(vm + '/api/v1/export', params={
        'match[]': '{__name__=~"model_probe_(' + '|'.join(FIELDS) + ')",environment=~"a3-vllm|xpu-pd|dcu-pd"}',
        'start': start, 'end': end}, timeout=6)
    response.raise_for_status()
    return decode(response.text, start, end)


@router.get('/api/monitoring/probes')
async def probe_history(request: Request):
    from .api import VM
    service = request.app.state.services['dcu-pd']
    try:
        async with asyncio.timeout(8), service.slots:
            return await history(service.client, VM)
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(503, '探测记录暂不可用')
