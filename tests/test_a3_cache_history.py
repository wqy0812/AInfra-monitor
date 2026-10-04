"""External-cache gaps must remain independent of the local-cache series."""
import os
import time

import httpx
import pytest

from monitoring.api import Service


@pytest.mark.asyncio
@pytest.mark.parametrize('view', ['full', 'summary'])
async def test_external_gap_marker_and_latest_boundary(view):
    service = Service('a3-vllm')
    service.latest_point = {'ts': 125, 'nodes': {'prefill': {}, 'decode': {}}, 'mooncake': {}}

    async def query(expression, start, end, step):
        if 'monitoring_chart_' not in expression:
            return []
        return [{'metric': {'path': 'nodes.prefill.cache_60s.' + field},
                 'values': [[120, str(0 if field == 'external_ratio' and 'min_over_time' in expression else
                                     .5 if 'chart_value' in expression else 1)]]}
                for field in ('ratio', 'external_ratio')]

    service.query = query
    try:
        result = await service._history(1, 100, 125, 15, view)
        pre = result['points'][0]['nodes']['prefill']
        assert pre['cache_60s']['ratio'] == pre['cache_60s']['external_ratio'] == .5
        assert 'cache' not in pre['gap_before']
        assert 'external_cache' in pre['gap_before']
        assert 'external_cache' in result['points'][-1]['nodes']['prefill']['gap_before']
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('step', [15, 60])
@pytest.mark.parametrize('case', ['zero', 'invalid', 'missing'])
async def test_external_gap_with_real_vm(step, case, monkeypatch):
    url = os.environ.get('MONITORING_TEST_VM_URL')
    if not url:
        pytest.skip('Requires disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:')
    monkeypatch.setattr('monitoring.api.VM', url)
    end = int(time.time() // 60) * 60 - 600 - (['zero', 'invalid', 'missing'].index(case) * 2 + (step == 60)) * 300
    lines = []
    for ts in range(end - 120, end + 1, 5):
        for field in ('ratio', 'external_ratio'):
            if case == 'missing' and field == 'external_ratio' and ts == end - 5:
                continue
            tags = '{environment="a3-vllm",schema="v1",path="nodes.prefill.cache_60s.' + field + '"}'
            invalid = case == 'invalid' and field == 'external_ratio' and ts == end - 5
            for name, value in [('value', .5 if field == 'ratio' else 0), ('valid', int(not invalid))]:
                lines.append(f'monitoring_chart_{name}{tags} {value} {ts * 1000}')
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
        (await client.post('/api/v1/import/prometheus', content='\n'.join(lines)+'\n')).raise_for_status()
        (await client.get('/internal/force_flush')).raise_for_status()
    service = Service('a3-vllm')
    try:
        result = await service._history(1, end - step, end, step, 'summary')
        pre = result['points'][-1]['nodes']['prefill']
        assert pre['cache_60s']['ratio'] == .5
        assert pre['cache_60s']['external_ratio'] == 0
        assert 'cache' not in pre['gap_before']
        assert ('external_cache' in pre['gap_before']) == (case != 'zero')
    finally:
        await service.close()
