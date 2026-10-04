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

    async def raw(*args): return {}
    async def transport(request):
        if request.url.path == '/api/v1/query':
            entered.set()
            await release.wait()
        return httpx.Response(200, json={'data': {'result': []}})

    service.raw = raw
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


@pytest.mark.asyncio
@pytest.mark.parametrize('environment', ['dcu-pd', 'xpu-pd'])
async def test_real_vm_filtered_export_matches_original_replay(tmp_path, monkeypatch, environment):
    url = os.environ.get('MONITORING_TEST_VM_URL')
    if not url:
        pytest.skip('Requires disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:')
    monkeypatch.setattr(api, 'VM', url)
    monkeypatch.setattr(api, 'STATE', tmp_path)
    base = int(time.time() // 5) * 5 - 600
    lines = []
    for tick in range(100, 241, 5):
        def add(name, value, job, **labels):
            tags = {'environment': environment, 'job': job, 'instance': job, **labels}
            encoded = ','.join(k + '=' + json.dumps(v) for k, v in tags.items())
            lines.append(f'{name}{{{encoded}}} {value} {(base + tick) * 1000}')
        for role in ['prefill', 'decode']:
            job = 'sglang-' + role
            add('up', int(tick != 165), job)
            for name in ['num_requests_total', 'prompt_tokens_total', 'generation_tokens_total']:
                add('sglang:' + name, tick, job, model_name='fixture')
            add('sglang:num_running_reqs', 1, job, engine_type=role, tp_rank='0')
            add('sglang:realtime_tokens_total', tick * 20, job, engine_type=role, tp_rank='0', mode='decode')
            add('sglang:cache_hit_rate', .5, job, engine_type=role, tp_rank='0', pp_rank='0')
            for mode in ['input', 'device_hit', 'host_hit', 'storage_hit']:
                add('sglang:prefill_effective_tokens_total', tick, job, engine_type=role, tp_rank='0', mode=mode)
            for metric in ['time_to_first_token_seconds', 'inter_token_latency_seconds', 'e2e_request_latency_seconds']:
                for bound in ['1', '+Inf']:
                    add('sglang:' + metric + '_bucket', tick, job, le=bound, model_name='fixture')
                add('sglang:' + metric + '_count', tick, job, model_name='fixture')
            # These large diagnostic families are still independently queryable.
            for rank in range(16):
                add('sglang:per_stage_req_latency_seconds_bucket', tick, job, le='1', tp_rank=str(rank))
                add('sglang:queue_time_seconds_bucket', tick, job, le='1', tp_rank=str(rank))
        if environment == 'dcu-pd':
            add('up', 1, 'mooncake')
            for name, rate in [('total_get_nums_', 10), ('valid_get_nums_', 9), ('mem_cache_hit_nums_', 6), ('file_cache_hit_nums_', 3)]:
                add(name, tick * rate, 'mooncake')
            add('master_allocated_bytes', 20, 'mooncake')
            add('master_total_capacity_bytes', 40, 'mooncake')
            for role in ['prefill', 'decode']:
                add('up', 1, 'node-' + role)
                add('node_boot_time_seconds', 1, 'node-' + role)
                add('node_cpu_seconds_total', tick, 'node-' + role, cpu='0', mode='user')
                add('node_cpu_seconds_total', tick * 4, 'node-' + role, cpu='0', mode='idle')
    service = api.Service(environment)
    try:
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            (await client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n')).raise_for_status()
            (await client.get('/internal/force_flush')).raise_for_status()
            response = await client.get('/api/v1/export', params={'match[]': '{environment=' + json.dumps(environment) + '}', 'start': base+100, 'end': base+240})
            response.raise_for_status()
            original = decode_export(json.loads(line) for line in response.text.splitlines())
        filtered = await service.raw(base+100, base+240)
        assert sum(len(rows) for _, values in filtered.values() for rows in values.values()) < sum(len(rows) for _, values in original.values() for rows in values.values()) / 2
        assert service.replay(filtered, base+100, base+240) == service.replay(original, base+100, base+240)
        _, points = service.replay(filtered, base+100, base+240)
        by_tick = {p['ts']-base: p for p in points}
        assert by_tick[160]['nodes']['decode']['requests'] == 1
        assert by_tick[165]['nodes']['decode']['requests'] is None
        assert by_tick[175]['nodes']['decode']['requests'] == 1
    finally:
        await service.close()
