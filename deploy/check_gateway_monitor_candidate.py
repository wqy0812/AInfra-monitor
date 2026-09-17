"""Bounded API candidate checks against existing VM data; never writes VM data."""
import argparse
import asyncio
import json
import time

import httpx


async def api():
    from monitoring.api import Service
    from monitoring.gateway_live import FIELDS

    async def read_only(request):
        if request.method != 'GET' or request.url.path not in ('/api/v1/query', '/api/v1/query_range'):
            raise RuntimeError('Candidate checks permit only read-only VM queries')

    report = {}
    for environment in ('dcu-pd', 'a3-vllm'):
        service = Service(environment)
        await service.client.aclose()
        service.client = httpx.AsyncClient(trust_env=False, timeout=10,
            limits=httpx.Limits(max_connections=8), event_hooks={'request': [read_only]})
        try:
            for hours in (1, 6, 24, 168, 720):
                started = time.monotonic()
                async with asyncio.timeout(15):
                    data = await service.history(hours)
                assert data['environment'] == environment
                assert 0 < len(data['points']) <= 721
                assert set(data['gateway_status']) == set(FIELDS)
                assert set(data['gateway_status'].values()) == {'ok'}, data['gateway_status']
                counts = {key: sum(p['gateway'][key] is not None for p in data['points']) for key in FIELDS}
                assert all(counts.values()), (environment, hours, counts)
                report[environment + '/' + str(hours)] = {
                    'points': len(data['points']), 'gateway_valid_points': counts,
                    'seconds': round(time.monotonic() - started, 3)}
        finally:
            await service.client.aclose()
    result = {'passed': True, 'read_only_vm_queries': True, 'ranges': report}
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['api'])
    parser.parse_args()
    asyncio.run(api())
