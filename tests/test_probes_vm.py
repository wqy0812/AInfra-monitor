"""Exercise production raw import/export against the disposable local VM."""
import os
import time

import httpx
import pytest

from monitoring import probes, probe_worker


@pytest.mark.asyncio
async def test_probe_import_export_preserves_failure_gaps_and_missing_fields():
    vm = os.environ.get('MONITORING_TEST_VM_URL')
    if not vm:
        pytest.skip('Use scripts/test_local_vm.sh for the real VM test')
    assert vm.startswith('http://127.0.0.1:')
    now = int(time.time())
    def result(ts, reason):
        return {'reason': reason, 'started_at': ts-1, 'finished_at': ts, 'duration_seconds': 1,
                'first_output_seconds': .1, 'http_status': 200}
    minute_results = [result(now - 660 + n * 60, 'timeout' if n == 4 else 'success') for n in range(10)]
    raw = ''.join(probes.samples('dcu-pd', value) for value in minute_results) + (probes.samples('a3-vllm', result(now-1200, 'success'))
           + probes.samples('a3-vllm', result(now-2, 'incomplete_stream'))
           + probes.samples('dcu-pd', result(now-700, 'success'))
           + probes.samples('xpu-pd', result(now-1, 'success')).splitlines()[0]+'\n')
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(vm+'/api/v1/import', content=raw)
        response.raise_for_status()
        (await client.get(vm+'/internal/force_flush')).raise_for_status()
        data = await probes.history(client, vm, now)
    a3, xpu, dcu = data['services']
    assert [service['offset_seconds'] for service in data['services']] == [0, 20, 40]
    assert a3['status'] == 'failure'
    assert [record['finished_at'] for record in a3['records']] == [now-1200, now-2]
    assert a3['latest']['reason'] == 'incomplete_stream'
    assert a3['latest']['first_output_seconds'] == .1
    assert xpu['status'] == 'unknown' and xpu['latest']['reason'] == 'incomplete_observation'
    assert data['interval_seconds'] == 60 and data['stale_after_seconds'] == 120
    assert dcu['status'] == 'success'
    assert [record['finished_at'] for record in dcu['records'][1:]] == [value['finished_at'] for value in minute_results]
    assert dcu['records'][5]['reason'] == 'timeout'
    assert (await read_after_expiry(vm, now + 1))['services'][2]['status'] == 'unknown'
    # Exercise the actual worker cycle through VM, with only model inference replaced.
    def model(request):
        assert request.method == 'POST' and request.url.path == '/v1/chat/completions'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=(
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n'))
    async with httpx.AsyncClient(trust_env=False, mounts={'http://model.test': httpx.MockTransport(model)}) as client:
        result = await probe_worker.cycle(client, vm, 'a3-vllm', 'http://model.test/v1/chat/completions')
        assert result['reason'] == 'success'
        (await client.get(vm+'/internal/force_flush')).raise_for_status()
        data = await probes.history(client, vm)
    assert data['services'][0]['status'] == 'success'
    assert len(data['services'][0]['records']) == 3


async def read_after_expiry(vm, now):
    async with httpx.AsyncClient(trust_env=False) as client:
        return await probes.history(client, vm, now)
