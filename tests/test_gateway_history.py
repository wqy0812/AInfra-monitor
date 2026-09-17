import asyncio
import json
from pathlib import Path

import httpx
import pytest

from monitoring import gateway_live as live
from monitoring.api import Service


@pytest.mark.parametrize('environment,project', [('dcu-pd', 'dcu-monitoring'), ('a3-vllm', 'a3-monitoring')])
@pytest.mark.parametrize('step', [5, 15, 60, 3605])
def test_queries_match_current_project_dashboards(environment, project, step):
    path = Path(__file__).parents[1] / 'perses/projects' / project / 'dashboards/gateway-generation.json'
    panels = json.loads(path.read_text())['spec']['panels']
    queries = live.expressions(environment, step)
    for field, panel in [(live.IDLE, 'live-idle-max'), (live.OLDEST, 'live-oldest')]:
        expected = panels[panel]['spec']['queries'][0]['spec']['plugin']['spec']['query']
        assert queries[field] == expected.replace('$__interval', f'{step}s')


def row(values, **labels):
    return {'metric': {'environment': 'dcu-pd', **labels}, 'values': values}


def test_decode_retains_zero_and_rejects_wrong_environment_invalid_and_ambiguous_values():
    rows = [row([[100, '0'], [105, 'NaN'], [110, '-1'], [115, 'inf'], [120, '2'], [125, '9']]),
            row([[120, '3']]), row([[100, '9999']], environment='a3-vllm')]
    assert live.samples(rows, 'dcu-pd', live.IDLE, 100, 120) == {100: {live.IDLE: 0}}
    assert live.samples([row([[100, '8']])], 'dcu-pd', live.OLDEST, 100, 120) == {}
    assert live.samples([row([[100, '0']])], 'dcu-pd', live.OLDEST, 100, 120)[100]['stage_name'] == '无在途请求'


def test_gaps_and_stage_or_backend_changes_break_lines():
    points = [{'ts': ts} for ts in (100, 105, 110, 115, 120, 130)]
    gateway = {ts: {live.IDLE: 1, live.OLDEST: ts, 'backend': 'a', 'stage': 'streaming'}
               for ts in (100, 105, 110, 120, 130)}
    gateway[110]['stage'] = 'writing_client'
    gateway[120]['backend'] = 'b'
    live.attach(points, gateway, 5)
    assert points[1]['gateway']['gap_before'] == []
    assert points[2]['gateway']['gap_before'] == [live.OLDEST]
    assert points[3]['gateway'][live.IDLE] is None
    assert all(p['gateway']['gap_before'] == list(live.FIELDS) for p in points[3:])


@pytest.mark.asyncio
async def test_each_query_has_an_independent_timeout_and_error_boundary():
    async def query(expr, *args):
        if 'aigate_stream_idle_max_seconds' in expr:
            await asyncio.sleep(1)
        return [row([[100, '55']], backend='a', stage='streaming')]
    points, status = await live.history(query, 'dcu-pd', 100, 110, 5, timeout=.01)
    assert status == {live.IDLE: 'unavailable', live.OLDEST: 'ok'}
    assert points[100][live.OLDEST] == 55
    assert points[100]['stage_name'] == '读取后续流'


@pytest.mark.asyncio
@pytest.mark.parametrize('gateway_only', [False, True])
async def test_history_merges_gateway_timestamps_caches_and_preserves_backend_fields(gateway_only):
    service = Service()
    calls = []
    async def query(expr, start, end, step):
        calls.append((expr, start, end, step))
        if 'aigate_stream_idle_max_seconds' in expr:
            return [row([[105, '0'], [110, '3']])]
        if 'aigate_inflight_oldest_age_seconds' in expr:
            return [row([[105, '0']]), row([[110, '20']], backend='a', stage='streaming')]
        if gateway_only:
            return []
        return [{'metric': {'path': path}, 'values': [[100, '7' if 'chart_value' in expr else '1']]}
                for path in ('nodes.decode.requests', 'nodes.decode.percentiles.e2e.p95')]
    service.query = query
    try:
        result = await service.history(1, 100, 110)
        assert [p['ts'] for p in result['points']] == ([105, 110] if gateway_only else [100, 105, 110])
        assert result['points'][-1]['gateway'][live.OLDEST] == 20
        assert result['points'][-2]['gateway'][live.IDLE] == 0
        if not gateway_only:
            node = result['points'][0]['nodes']['decode']
            assert node['requests'] == node['percentiles']['e2e']['p95'] == 7
        assert await service.history(1, 100, 110) is result
        assert len(calls) == 5
        assert all(args[1:] == (100, 110, 5) for args in calls)
    finally:
        await service.client.aclose()


@pytest.mark.asyncio
async def test_gateway_failure_leaves_backend_history_available():
    service = Service()
    async def query(expr, *args):
        if 'aigate_' in expr:
            raise httpx.ConnectError('fixture offline')
        return [{'metric': {'path': 'nodes.decode.decode_tokens'}, 'values': [[100, '1']]}]
    service.query = query
    try:
        result = await service.history(1, 100, 110)
        assert result['points'][0]['nodes']['decode']['decode_tokens'] == 1
        assert result['points'][0]['gateway'][live.IDLE] is None
        assert set(result['gateway_status'].values()) == {'unavailable'}
    finally:
        await service.client.aclose()


@pytest.mark.asyncio
async def test_latest_backend_sample_is_retained_when_gateway_is_ahead():
    service = Service()
    service.latest_point = {'ts': 105, 'nodes': {'prefill': {}, 'decode': {'decode_tokens': 42}}, 'mooncake': {}}
    async def query(expr, *args):
        return [row([[110, '3']])] if 'aigate_stream_idle_max_seconds' in expr else []
    service.query = query
    try:
        result = await service.history(1, 100, 110)
        assert [p['ts'] for p in result['points']] == [105, 110]
        assert result['points'][0]['nodes']['decode']['decode_tokens'] == 42
        assert result['points'][1]['gateway'][live.IDLE] == 3
    finally:
        await service.client.aclose()
