"""Isolated candidate regression: synthetic queries only, no VM writes."""
import asyncio
import httpx
from monitoring.api import Service
from monitoring import xpu_cache


async def main():
    service = Service('xpu-pd')
    async def query(expr, *args):
        if 'sglang:cache_hit_rate' in expr:
            raise httpx.ReadTimeout('synthetic cache timeout')
        if 'monitoring_chart_' in expr:
            return [{'metric': {'path': 'nodes.decode.decode_tokens'},
                     'values': [[100, '42' if 'chart_value' in expr else '1']]}]
        return []
    service.query = query
    try:
        result = await service.history(1, 100, 105)
        node = result['points'][0]['nodes']
        assert node['decode']['decode_tokens'] == 42
        assert node['prefill']['cache_60s']['ratio'] is None
        assert 'cache' in node['prefill']['gap_before']
        assert all(v == 'ok' for v in result['gateway_status'].values())
    finally:
        await service.client.aclose()
    finished = []
    async def pending(expr, *args):
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(expr)
    async with asyncio.timeout(1):
        assert await xpu_cache.history(pending, 100, 105, 5, timeout=.01) == ({}, {})
    assert len(finished) == 2
    print('Candidate timeout isolation, deadlines and cleanup passed')


asyncio.run(main())
