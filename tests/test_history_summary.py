"""The page summary must preserve aggregates without fetching resource series."""
import copy
import json

import pytest
from fastapi import HTTPException

from monitoring.api import Service, history_expression, summary_point


@pytest.mark.parametrize('environment', ['dcu-pd', 'a3-vllm', 'xpu-pd'])
@pytest.mark.asyncio
async def test_summary_preserves_values_gaps_latest_and_full_contract(environment):
    service = Service(environment)
    resources = {'gpu_memory': {f'card{i}': i for i in range(64)}}
    service.latest_point = {
        'ts': 110, 'nodes': {'prefill': {'resources': resources},
                           'decode': {'decode_tokens': 42, 'resources': resources}},
        'mooncake': {'resources': resources},
    }
    original_latest = copy.deepcopy(service.latest_point)
    calls = []

    async def query(expr, start, end, step):
        calls.append(expr)
        if 'monitoring_chart_' not in expr:
            return []
        paths = {'nodes.decode.decode_tokens': 0,
                 'nodes.decode.percentiles.ttft.p95': 0.25,
                 'nodes.prefill.cache_60s.ratio': 0.5,
                 'mooncake.tier_query_60s.memory': 0.8}
        if 'path!~".*\\\\.resources\\\\..*"' not in expr:
            paths.update({f'nodes.decode.resources.gpu_memory.card{i}': i for i in range(64)})
        return [{'metric': {'path': path},
                 'values': [[100, str(value if 'chart_value' in expr else 1)],
                            [105, str(value if 'chart_value' in expr else 0)]]}
                for path, value in paths.items()]

    service.query = query
    try:
        full = await service.history(1, 100, 110)
        calls.clear()
        summary = await service.history(1, 100, 110, view='summary')
        assert summary == {**full, 'points': [summary_point(copy.deepcopy(p)) for p in full['points']]}
        assert service.latest_point == original_latest
        assert 'resources' not in json.dumps(summary)
        assert len(json.dumps(summary)) < len(json.dumps(full))
        assert summary['points'][0]['nodes']['decode']['decode_tokens'] == 0
        assert summary['points'][1]['nodes']['decode']['decode_tokens'] is None
        assert 'decode_tokens' in summary['points'][1]['nodes']['decode']['gap_before']
        backend_queries = [q for q in calls if 'monitoring_chart_' in q]
        assert len(backend_queries) == 3
        assert all('path!~".*\\\\.resources\\\\..*"' in q for q in backend_queries)
        count = len(calls)
        assert await service.history(1, 100, 110, view='summary') is summary
        assert len(calls) == count
        assert (await service.history(1, 100, 110))['points'][0]['nodes']['decode']['resources']
    finally:
        await service.client.aclose()


def test_unknown_view_is_rejected():
    with pytest.raises(HTTPException) as error:
        history_expression('dcu-pd', 'value', 5, 'typo')
    assert error.value.status_code == 400
