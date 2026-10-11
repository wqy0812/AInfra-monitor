import copy
import json
from pathlib import Path

import httpx
import pytest

from monitoring import host_cpu


def row(values, **labels):
    return {'metric': {'job': 'node-a3', 'node': 'a3-1', 'instance': '122.209.21.24:9100', 'host_cpu_field': 'cpu', **labels}, 'values': values}


def test_calculation_matches_legacy_panel_exactly():
    old = json.loads((Path(__file__).parent / 'fixtures/host-cpu-queries.json').read_text())
    assert host_cpu.expression() == old['cpu']
    assert host_cpu.iowait_expression() == old['cpu_iowait']


def test_samples_preserve_zero_and_reject_wrong_hosts_bad_values_and_duplicates():
    rows = [row([[100, '0'], [105, 'NaN'], [110, '-1'], [115, '101'], [120, '42'], [125, '8'], [127, '9']]),
            row([[100, '9']], environment='dcu-pd'), row([[100, '9']], instance='other:9100'),
            row([[120, '42']]), row([[120, '42']])]
    rows.append({'metric': {'environment': 'a3-vllm', 'node': 'a3-1', 'host_cpu_field': 'cpu_iowait'}, 'values': [[100, '12']]})
    assert host_cpu.samples(rows, 100, 125) == {(100, 'prefill', 'cpu'): 0, (125, 'prefill', 'cpu'): 8, (100, 'prefill', 'cpu_iowait'): 12}




@pytest.mark.asyncio
async def test_collect_uses_only_new_points_and_disables_recent_result_offset():
    calls = []
    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'matrix', 'result': [row([[105, '40']])]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await host_cpu.collect(client, 'http://vm', 105, 105) == ({(105, 'prefill', 'cpu'): 40}, 'ok')
        assert await host_cpu.collect(client, 'http://vm', 110, 105) == ({}, 'ok')
        with pytest.raises(ValueError):
            await host_cpu.collect(client, 'http://vm', 100, 400)
    assert len(calls) == 1
    assert '"host_cpu_field", "cpu_iowait"' in calls[0]['query']
    assert {k: calls[0][k] for k in ('start', 'end', 'step', 'latency_offset', 'nocache')} == {
        'start': '105', 'end': '105', 'step': '5', 'latency_offset': '1ms', 'nocache': '1'}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['http', 'timeout', 'malformed'])
async def test_cpu_failure_is_explicit_and_preserves_other_metrics(failure):
    async def handler(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('CPU timed out')
        return httpx.Response(503 if failure == 'http' else 200, json={'status': 'error'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        values, status = await host_cpu.collect(client, 'http://vm', 100, 105)
    assert values == {} and status == 'unavailable'
    points = [{'ts': 105, 'nodes': {'prefill': {'requests': 42}, 'decode': {'requests': 7}}}]
    snapshots = copy.deepcopy(points)
    host_cpu.attach(snapshots, points, values, status)
    assert points[0]['nodes']['prefill'] == {'requests': 42, 'cpu': None, 'cpu_iowait': None}
    assert snapshots[0]['nodes']['prefill']['host_cpu']['query_status'] == 'unavailable'
