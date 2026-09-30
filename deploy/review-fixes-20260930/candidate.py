"""Read-only candidate acceptance against test4's actual VictoriaMetrics."""
import asyncio
import copy
import json
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, '/monitoring')

import httpx

from monitoring import api
from monitoring.access import client_allowlist


async def main():
    original = httpx.AsyncClient.request
    reads = []

    async def read_only(self, method, url, **kwargs):
        if method.upper() != 'GET':
            raise RuntimeError('Candidate must not write to production VM')
        reads.append(str(url))
        return await original(self, method, url, **kwargs)

    httpx.AsyncClient.request = read_only
    try:
        client_allowlist('*')
    except ValueError:
        pass
    else:
        raise AssertionError('Wildcard was accepted')
    end = int((time.time()-120)//5)*5
    reports = {}
    for environment in api.ENVIRONMENTS:
        service = api.Service(environment)
        try:
            full = await service.history(1, end-3600, end)
            summary = await service.history(1, end-3600, end, view='summary')
            assert full['points'] and summary == {
                **full, 'points': [api.summary_point(copy.deepcopy(p)) for p in full['points']]}
            reports[environment] = {'points': len(full['points']), 'summary_matches': True}
        finally:
            await service.close()

    service = api.Service('xpu-pd')
    query = service.query
    started = time.monotonic()
    async def unavailable_optional(expression, *args):
        if 'aigate' in expression or 'sglang:cache_hit_rate' in expression:
            await asyncio.Event().wait()
        return await query(expression, *args)
    service.query = unavailable_optional
    try:
        value = await service.history(1, end-3600, end)
        elapsed = time.monotonic()-started
        assert value['points'] and elapsed < 7.8
        assert set(value['gateway_status'].values()) == {'unavailable'}
        assert any(p['nodes']['decode']['output_tokens'] is not None for p in value['points'])
        reports['optional_outage'] = {'seconds': elapsed, 'successful_backend_preserved': True}
    finally:
        await service.close()

    # Reproduce the actual XPU outage using a fresh processing watermark.
    services = {}
    for environment in api.ENVIRONMENTS:
        services[environment] = SimpleNamespace(error=None, watermark=time.time()-5,
            started=time.time(), host_cpu_status='ok', retention_gap=None,
            latest={'nodes': {r: {'metrics': {'status': 'ok'},
                'telemetry': {'status': 'ok', 'data': {'gpus': [{}]*8}}} for r in ('prefill', 'decode')},
                'mooncake': {'status': 'ok'}})
    services['xpu-pd'].latest['nodes']['prefill']['metrics']['status'] = 'error'
    state = SimpleNamespace(service=services['dcu-pd'], services=services)
    assert (await api.health(SimpleNamespace(app=SimpleNamespace(state=state))))['status'] == 'degraded'
    transport = httpx.ASGITransport(app=api.app, client=('203.0.113.123', 12345))
    async with httpx.AsyncClient(transport=transport, base_url='http://candidate') as client:
        assert (await client.get('/health', headers={'X-Forwarded-For': '127.0.0.1'})).status_code == 403
    print(json.dumps({'passed': True, 'production_writes': 0, 'reads': len(reads), 'results': reports}))


asyncio.run(main())
