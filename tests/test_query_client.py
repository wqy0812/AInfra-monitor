import asyncio

import httpx
import pytest

from monitoring import api
from monitoring.query_client import QueryClient


@pytest.mark.asyncio
async def test_failed_pool_is_discarded_and_next_batch_succeeds(monkeypatch):
    original = httpx.AsyncClient
    clients = []

    def factory(**kwargs):
        failed = not clients
        async def handler(request):
            if failed:
                raise httpx.PoolTimeout('poisoned pool')
            return httpx.Response(200, json={'status': 'success', 'data': {'result': []}})
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    service = api.Service('a3-vllm')
    try:
        with pytest.raises(httpx.PoolTimeout):
            await service.history(1, 100, 200)
        assert len(clients) == 1 and clients[0].is_closed
        assert not service.cache and not service.history_tasks
        assert (await service.history(1, 100, 200))['environment'] == 'a3-vllm'
        assert len(clients) == 2 and clients[1].is_closed
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancellation_closes_pool_and_does_not_block_other_batches():
    entered = asyncio.Event()
    pools = []

    async def handler(request):
        if request.url.path == '/slow':
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200)

    client = QueryClient(transport=httpx.MockTransport(handler))
    async def batch(path):
        async with client.scope():
            pools.append(client.current.get())
            return await client.get('http://vm'+path)

    slow = asyncio.create_task(batch('/slow'))
    await entered.wait()
    assert (await batch('/fast')).status_code == 200
    assert pools[0] is not pools[1] and pools[1].is_closed
    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow
    assert pools[0].is_closed
    assert (await batch('/next')).status_code == 200
    assert all(pool.is_closed for pool in pools)
    await client.aclose()
