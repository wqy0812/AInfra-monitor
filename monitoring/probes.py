"""Raw synthetic request observations. No interpolation or business-traffic SLOs."""
import asyncio
import json
import math
import re
import time

from fastapi import APIRouter, HTTPException, Request
import httpx

INTERVAL = 60
STALE_AFTER = 120
TARGETS = (('a3-vllm', 'A3'), ('xpu-pd', 'XPU'), ('dcu-pd', 'DCU'))
# Keep existing schedules stable when adding an entrance: history uses these offsets.
OFFSETS = {'a3-vllm': 0, 'xpu-pd': 20, 'dcu-pd': 40}
REASONS = ('success', 'http_error', 'timeout', 'connection_error', 'stream_error',
           'invalid_stream', 'empty_output', 'incomplete_stream', 'identity_mismatch')
FIELDS = ('result', 'started_at_seconds', 'duration_seconds', 'first_output_seconds', 'http_status')
PREFIX = 'model_probe_'
router = APIRouter()


def samples(environment, result, *, target=None):
    """All fields share one timestamp; a partially stored observation is unknown."""
    stamp = round(result['finished_at'] * 1000)
    values = (REASONS.index(result['reason']), result['started_at'], result['duration_seconds'],
              result['first_output_seconds'] if result['first_output_seconds'] is not None else -1,
              result['http_status'] or 0)
    labels = {'environment': environment} if environment else {}
    if target is not None:
        labels.update(probe_id=target['probe_id'], probe_kind=target['probe_kind'])
        for name in ('foundation_id', 'gateway_id'):
            if target.get(name): labels[name] = target[name]
        if result.get('actual_router_id'):
            labels['actual_router_id'] = result['actual_router_id']
    return '\n'.join(json.dumps({'metric': {'__name__': PREFIX + field, **labels},
                                 'values': [value], 'timestamps': [stamp]})
                     for field, value in zip(FIELDS, values)) + '\n'


def decode(raw, start, end, *, targets=None):
    managed = targets is not None
    if not managed:
        targets = [{'probe_id': env, 'environment': env, 'name': name, 'offset_seconds': OFFSETS[env]}
                   for env, name in TARGETS]
    by_id = {target['probe_id']: target for target in targets}
    legacy = {target['legacy_probe_environment']: target for target in targets
              if target.get('legacy_probe_environment')}
    observations = {identity: {} for identity in by_id}
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        labels = row['metric']
        identity = labels.get('probe_id') if managed else labels.get('environment')
        old_target = legacy.get(labels.get('environment')) if managed and not identity else None
        if old_target:
            identity = old_target['probe_id']
        field = labels.get('__name__', '').removeprefix(PREFIX)
        if identity not in observations or field not in FIELDS:
            continue
        target = by_id[identity]
        if managed and not old_target and labels.get('probe_kind') != target['probe_kind']:
            continue
        if len(row['values']) != len(row['timestamps']):
            raise ValueError('Invalid probe samples')
        for ts, value in zip(row['timestamps'], row['values']):
            ts, value = float(ts) / 1000, float(value)
            if not math.isfinite(ts) or not start <= ts <= end:
                continue
            if old_target and ts >= old_target['probe_migrated_at']:
                continue
            point = observations[identity].setdefault(ts, {})
            actual = labels.get('actual_router_id') or None
            if 'actual_router_id' in point and point['actual_router_id'] != actual:
                point['invalid'] = True
            point['actual_router_id'] = actual
            # Conflicting duplicate series must never produce a green result.
            if field in point and point[field] != value:
                point['invalid'] = True
            point[field] = value
    services = []
    for target in targets:
        records = []
        for ts, values in sorted(observations[target['probe_id']].items()):
            valid = (not values.get('invalid') and all(k in values and math.isfinite(values[k]) for k in FIELDS))
            if valid:
                code, started, duration, first, status = (values[k] for k in FIELDS)
                valid = (code.is_integer() and 0 <= code < len(REASONS) and 0 < started <= ts + .001
                         and 0 <= duration and (first == -1 or 0 <= first <= duration)
                         and status.is_integer() and (status == 0 or 100 <= status <= 599)
                         and (code != 0 or (first >= 0 and 200 <= status < 300)))
            record = {'finished_at': ts, 'started_at': started if valid else None,
                            'status': ('success' if code == 0 else 'failure') if valid else 'unknown',
                            'reason': REASONS[int(code)] if valid else 'incomplete_observation',
                            'duration_seconds': duration if valid else None,
                            'first_output_seconds': first if valid and first >= 0 else None,
                            'http_status': int(status) if valid and status else None}
            if managed:
                record['actual_router_id'] = values.get('actual_router_id')
            records.append(record)
        latest = records[-1] if records else None
        from .probe_catalog import public
        metadata = public(target) if managed else {k:target[k] for k in ('environment','name','offset_seconds')}
        services.append({**metadata, 'latest': latest,
                         'status': latest['status'] if latest and end-latest['finished_at'] <= STALE_AFTER else 'unknown',
                         'records': records})
    return {'source': 'victoriametrics', **({'probe_schema':'managed-v1'} if managed else {}), 'observed_at': end, 'start': start, 'end': end,
            'interval_seconds': INTERVAL, 'stale_after_seconds': STALE_AFTER, 'services': services}


async def history(client, vm, now=None):
    end = time.time() if now is None else now
    start = end - 86400
    from . import probe_catalog
    targets = probe_catalog.targets(now=end)
    family = '__name__=~"model_probe_(' + '|'.join(FIELDS) + ')"'
    if targets is None:
        selectors = ['{' + family + ',environment=~"' + '|'.join(env for env, _ in TARGETS) + '"}']
    else:
        if not targets:
            return decode('', start, end, targets=[])
        selectors = ['{' + family + ',probe_id=~' + json.dumps('|'.join(re.escape(t['probe_id']) for t in targets)) + '}']
        old = [t['legacy_probe_environment'] for t in targets if t.get('legacy_probe_environment')]
        if old:
            selectors.append('{' + family + ',probe_id="",environment=~' + json.dumps('|'.join(map(re.escape, old))) + '}')
    response = await client.get(vm + '/api/v1/export', params={
        'match[]': selectors if len(selectors) > 1 else selectors[0],
        'start': start, 'end': end}, timeout=6)
    response.raise_for_status()
    value = decode(response.text, start, end, targets=targets)
    if targets is not None:
        runtime = probe_catalog.read_json(probe_catalog.state_file('status'))
        for service in value['services']:
            service['pause_intervals'] = runtime.get('pause_intervals', {}).get(service['probe_id'], [])
    return value


@router.get('/api/monitoring/probes')
async def probe_history(request: Request):
    from .api import VM
    service = next(iter(request.app.state.services.values()), None)
    try:
        async with asyncio.timeout(8):
            if service:
                async with service.slots:
                    return await history(service.client, VM)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                return await history(client, VM)
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(503, '探测记录暂不可用')
