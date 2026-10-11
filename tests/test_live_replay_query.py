"""Keep diagnostic series out of live replay without changing its outputs."""
import asyncio
import json
import os
import time

import httpx
import pytest

from monitoring import api
from monitoring.query_client import QueryClient
from monitoring.replay import decode_export


@pytest.mark.asyncio
async def test_snapshot_is_published_only_after_infrastructure_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'STATE', tmp_path)
    service = api.Service()
    service.watermark = int(time.time() // 5) * 5 - 10
    previous = service.latest
    entered, release = asyncio.Event(), asyncio.Event()

    async def transport(request):
        if request.url.path == '/api/v1/query':
            entered.set()
            await release.wait()
        return httpx.Response(200, json={'status':'success','data': {'result': ([{'metric':{'__name__':'monitoring_native_backend_requests_per_second','environment':'dcu-pd','node':'dcu2','role':'decode'},'values':[[service.watermark,'1']]}] if request.url.path=='/api/v1/query_range' else [])}})

    service.client = QueryClient(transport=httpx.MockTransport(transport))
    task = asyncio.create_task(service.cycle())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert service.latest is previous
        assert service.latest_point is None
        release.set()
        await task
        assert service.latest is not previous
        assert 'infrastructure' in service.latest
        assert service.latest_point['ts'] == service.latest['ts']
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
