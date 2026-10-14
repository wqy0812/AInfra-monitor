import asyncio
import json
from pathlib import Path

import httpx
import pytest

from monitoring import accelerator_history as accelerator, gateway_live
from monitoring.api import Service, history_expression
from monitoring.native_schema import NODES, empty_point

ENVIRONMENTS = [('dcu-pd', 'dcu-monitoring'), ('a3-vllm', 'a3-monitoring'), ('xpu-pd', 'xpu-monitoring')]


@pytest.mark.parametrize('environment,project', ENVIRONMENTS)
@pytest.mark.parametrize('step', [5, 15, 60, 3605])
def test_queries_match_summary(environment, project, step):
    panels = json.loads((Path(__file__).parents[1] / 'perses/projects' / project / 'dashboards/summary.json').read_text())['spec']['panels']
    for panel, actual in [('gateway-errors', gateway_live.expressions(environment, step)[gateway_live.ERRORS]),
                          ('accelerator-utilization', accelerator.expression(environment, step))]:
        expected = panels[panel]['spec']['queries'][0]['spec']['plugin']['spec']['query']
        assert actual == expected.replace('$__interval', f'{step}s')


def row(values, **labels):
    return {'metric': {'node': 'dcu1', 'role': 'Prefill', **labels}, 'values': values}


def test_accelerator_samples_validate_role_node_values_and_duplicates():
    rows = [row([[100, '0'], [105, '100'], [110, 'NaN'], [115, '101'], [120, '-1'], [125, 'inf'], [130, '20']]),
            row([[130, '25']]), row([[100, '999']], node='a3-1'),
            row([[100, '999']], environment='a3-vllm'), row([[100, '999']], role='Decode'),
            row([[100, '35']], node='dcu2', role='Decode')]
    values = accelerator.samples(rows, 'dcu-pd', 100, 135)
    assert values == {100: {'prefill': 0, 'decode': 35}, 105: {'prefill': 100}, 130: {}}


def test_accelerator_gaps_are_independent_of_other_node_and_metrics():
    points = [empty_point('dcu-pd', ts) for ts in (100, 105, 110, 115, 125)]
    values = {ts: {'decode': 0, **({'prefill': 50} if ts != 105 else {})} for ts in (100, 105, 110, 115, 125)}
    points[0]['nodes']['decode']['gap_before'] = ['cache']
    accelerator.attach(points, values, 5)
    assert [p['nodes']['prefill'][accelerator.FIELD] for p in points] == [50, None, 50, 50, 50]
    assert [accelerator.FIELD in p['nodes']['prefill'].get('gap_before', []) for p in points] == [True, True, True, False, True]
    assert [accelerator.FIELD in p['nodes']['decode'].get('gap_before', []) for p in points] == [True, False, False, False, True]
    assert 'cache' in points[0]['nodes']['decode']['gap_before']


@pytest.mark.parametrize('failure', ['timeout', 'http', 'malformed'])
@pytest.mark.asyncio
async def test_accelerator_query_failure_boundary(failure):
    async def query(*args):
        if failure == 'timeout':
            await asyncio.sleep(1)
        if failure == 'http':
            raise httpx.ConnectError('fixture offline')
        return [{'bad': 'response'}]
    assert await accelerator.history(query, 'dcu-pd', 100, 110, 5, timeout=.01) == ({}, 'unavailable')


@pytest.mark.parametrize('environment,project', ENVIRONMENTS)
@pytest.mark.parametrize('failed', [None, 'errors', 'accelerator'])
@pytest.mark.asyncio
async def test_history_views_merge_cache_and_isolate_queries(environment, project, failed):
    service = Service(environment)
    calls = []
    async def query(expr, start, end, step):
        calls.append(expr)
        if expr in (history_expression(environment), history_expression(environment, 'summary')):
            return [{'metric': {'__name__': 'monitoring_native_backend_decode_tokens_per_second' if environment != 'a3-vllm' else 'monitoring_native_backend_output_tokens_per_second',
                                'environment': environment, 'role': 'decode', 'node': NODES[environment]['decode']}, 'values': [[100, '12']]}]
        if expr == accelerator.expression(environment, step):
            if failed == 'accelerator':
                raise httpx.ConnectError('accelerator unavailable')
            return [row([[105, '25']], node=NODES[environment]['prefill']),
                    row([[110, '0']], node=NODES[environment]['decode'], role='Decode')]
        if expr == gateway_live.expressions(environment, step)[gateway_live.ERRORS]:
            if failed == 'errors':
                raise httpx.ConnectError('error counter unavailable')
            return [{'metric': {'environment': environment}, 'values': [[105, '0'], [110, '.25']]}]
        return []
    service.query = query
    service.latest_point = {'ts': 110, 'nodes': {'prefill': {accelerator.FIELD: 99}}}
    try:
        full = await service.history(1, 100, 110)
        summary = await service.history(1, 100, 110, view='summary')
        assert full == summary
        assert [p['ts'] for p in summary['points']] == [100, 105, 110]
        first, middle, last = summary['points']
        assert first['nodes']['decode']['decode_tokens'] == 12
        assert first['nodes']['prefill'][accelerator.FIELD] is None
        assert middle['nodes']['prefill'][accelerator.FIELD] == (None if failed == 'accelerator' else 25)
        assert last['nodes']['prefill'][accelerator.FIELD] is None
        assert last['nodes']['decode'][accelerator.FIELD] == (None if failed == 'accelerator' else 0)
        assert last['gateway'][gateway_live.ERRORS] == (None if failed == 'errors' else .25)
        assert summary['gateway_status'][gateway_live.ERRORS] == ('unavailable' if failed == 'errors' else 'ok')
        assert summary['accelerator_status'] == ('unavailable' if failed == 'accelerator' else 'ok')
        count = len(calls)
        assert await service.history(1, 100, 110, view='summary') is summary
        assert len(calls) == count == 14
    finally:
        await service.close()
