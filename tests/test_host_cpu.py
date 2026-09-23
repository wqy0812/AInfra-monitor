import copy
import json
from pathlib import Path

import httpx
import pytest

from monitoring import host_cpu
from monitoring.api import Service, encode, history_expression


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


def test_schema_isolated_from_old_a3_and_all_dcu_cpu():
    point = {'ts': 100, 'nodes': {'prefill': {'cpu': 0}, 'decode': {'cpu': None}}}
    a3 = encode([point], 'a3-vllm')
    dcu = encode([point], 'dcu-pd')
    assert 'schema="host-cpu-v1",node="a3-1"} 0 100000' in a3
    assert 'monitoring_chart_valid{path="nodes.prefill.cpu",environment="a3-vllm",schema="host-cpu-v1",node="a3-1"} 1' in a3
    assert 'monitoring_chart_valid{path="nodes.decode.cpu",environment="a3-vllm",schema="host-cpu-v1",node="a3-2"} 0' in a3
    assert 'host-cpu-v1' not in dcu and 'schema="v1"' in dcu
    for kind in ('value', 'valid', 'gaps'):
        expression = history_expression('a3-vllm', kind, 60)
        assert 'schema="host-cpu-v1"' in expression
        assert ',path!~' + json.dumps(host_cpu.PATH_REGEX) in expression
        assert 'host-cpu-v1' not in history_expression('dcu-pd', kind, 60)


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


@pytest.mark.asyncio
async def test_cycle_materializes_once_and_retains_watermark_on_write_failure(tmp_path, monkeypatch):
    import monitoring.api as api
    monkeypatch.setattr(api, 'STATE', tmp_path)
    monkeypatch.setattr(api.time, 'time', lambda: 113)
    service = Service('a3-vllm')
    await service.client.aclose()
    service.watermark = 100
    posts, calls = [], []
    failing = False
    async def raw(start, end):
        return {}
    async def collect(client, vm, start, end):
        calls.append((start, end))
        return {(105, 'prefill', 'cpu'): 0, (110, 'prefill', 'cpu'): 40, (110, 'prefill', 'cpu_iowait'): 10}, 'ok'
    def handler(request):
        if request.method == 'POST':
            posts.append(request.content.decode())
            return httpx.Response(503 if failing else 204)
        return httpx.Response(200, json={'data': {'result': []}})
    service.raw = raw
    monkeypatch.setattr(host_cpu, 'collect', collect)
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await service.cycle()
        assert calls == [(105, 110)] and service.watermark == 110
        assert service.latest_point['nodes']['prefill']['cpu'] == 40
        assert service.latest['nodes']['prefill']['host_cpu']['percent'] == 40
        assert service.latest['nodes']['prefill']['host_cpu']['iowait_percent'] == 10
        assert service.latest['nodes']['decode']['host_cpu']['status'] == 'unavailable'
        assert 'schema="host-cpu-v1",node="a3-1"} 0 105000' in posts[0]
        assert 'schema="host-cpu-v1",node="a3-1"} 40 110000' in posts[0]
        assert service.watermark_file.read_text() == '{"ts": 110}'
        failing = True
        monkeypatch.setattr(api.time, 'time', lambda: 118)
        with pytest.raises(httpx.HTTPStatusError):
            await service.cycle()
        assert service.watermark == 110
        assert service.watermark_file.read_text() == '{"ts": 110}'
    finally:
        await service.client.aclose()


@pytest.mark.asyncio
async def test_history_reads_aggregates_without_raw_cpu_query():
    service = Service('a3-vllm')
    calls = []
    async def query(expression, *args):
        calls.append(expression)
        if 'monitoring_chart_' not in expression:
            return []
        value = '42' if 'chart_value' in expression else '1'
        return [{'metric': {'path': 'nodes.prefill.' + field}, 'values': [[100, value]]} for field in host_cpu.FIELDS]
    service.query = query
    try:
        result = await service.history(1, 100, 105)
        assert result['points'][0]['nodes']['prefill']['cpu'] == 42
        assert result['points'][0]['nodes']['prefill']['cpu_iowait'] == 42
        assert 'cpu' not in result['points'][0]['nodes']['prefill']['gap_before']
        assert 'cpu_iowait' not in result['points'][0]['nodes']['prefill']['gap_before']
        assert all('node_cpu_seconds_total' not in q for q in calls)
        assert await service.history(1, 100, 105) is result
        assert len(calls) == 7
    finally:
        await service.client.aclose()
