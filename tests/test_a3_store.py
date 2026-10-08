import copy
import json
import os
import time

import httpx
import pytest
from monitoring import a3, a3_store
from monitoring.api import Service, encode


def raw(end=170):
    result = []
    for name, value in [('up', 1), *zip(a3_store.METRICS, [32, 128, 0, 1024])]:
        result.append({'metric': {'__name__': name, 'environment': 'a3-vllm', 'job': 'mooncake-a3', 'instance': 'host:9003', 'node': 'a3-1'},
                       'timestamps': [t * 1000 for t in range(100, end + 1, 5)], 'values': [value] * ((end - 100) // 5 + 1)})
    return result


def test_decode_replay_capacity_without_engines():
    snaps, points = a3.replay(a3.decode_export(raw()), 100, 180)
    assert snaps[14]['mooncake']['status'] == 'ok'
    assert points[14]['mooncake']['ssd_capacity'] == dict(used=0, total=1024, ratio=0, reason=None)
    assert snaps[14]['nodes']['prefill']['metrics']['status'] == 'error'
    assert snaps[-1]['mooncake']['status'] == 'error'
    assert points[-1]['mooncake']['ssd_capacity']['used'] is None
    assert 'A3 未暴露' in points[14]['mooncake']['tier_query_60s']['reason']


@pytest.mark.parametrize('case', ['down', 'duplicate', 'source', 'negative', 'missing', 'zero', 'oversized'])
def test_invalid_or_independent_capacity(case):
    rows = raw()
    if case == 'down': rows[0]['values'][-1] = 0
    if case == 'duplicate': rows.append(copy.deepcopy(rows[3]))
    if case == 'source':
        extra = copy.deepcopy(rows)
        for r in extra: r['metric']['instance'] = 'another:9003'
        rows += extra
    if case == 'negative': rows[3]['values'][-1] = -1
    if case == 'missing': rows.pop(3)
    if case == 'zero': rows[4]['values'][-1] = 0
    if case == 'oversized': rows[3]['values'][-1] = 2048
    state = a3_store.observe(a3.decode_export(rows), 170)
    data = state['data']
    assert data['ssd_capacity']['ratio'] is None
    if case not in ('down', 'source'):
        assert data['capacity']['ratio'] == .25
    if case == 'zero':
        assert data['ssd_capacity']['used'] == data['ssd_capacity']['total'] == 0


@pytest.mark.parametrize('tier,offset', [('capacity', 1), ('ssd_capacity', 3)])
@pytest.mark.parametrize('used', [0, 32])
def test_zero_capacity_only_accepts_zero_usage(tier, offset, used):
    rows = raw()
    rows[offset]['values'][-1] = used
    rows[offset + 1]['values'][-1] = 0
    state = a3_store.observe(a3.decode_export(rows), 170)
    other = 'ssd_capacity' if tier == 'capacity' else 'capacity'
    assert state['data'][other]['ratio'] == (0 if tier == 'capacity' else .25)
    assert state['data'][tier]['ratio'] is None
    assert state['status'] == ('ok' if used == 0 else 'partial')
    assert state['data'][tier]['used'] == (0 if used == 0 else None)
    assert state['data'][tier]['total'] == (0 if used == 0 else None)
    assert (state['error'] is None) == (used == 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('step', [5, 15, 60])
@pytest.mark.parametrize('case', ['zero', 'gap', 'down'])
async def test_real_vm_capacity_history(step, case, monkeypatch):
    url = os.environ.get('MONITORING_TEST_VM_URL')
    if not url: pytest.skip('Requires disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:')
    monkeypatch.setattr('monitoring.api.VM', url)
    end = int(time.time() // 60) * 60 - 12000 - (['zero', 'gap', 'down'].index(case) * 3 + [5,15,60].index(step)) * 300
    rows = raw()
    for row in rows:
        row['timestamps'] = [(t // 1000 + end - 170) * 1000 for t in row['timestamps']]
        if case == 'gap':
            del row['timestamps'][-2]; del row['values'][-2]
        if case == 'down' and row['metric']['__name__'] == 'up': row['values'][-2] = 0
    # A full missed scrape is stale by the next tick: replay gap at end-5.
    if case == 'gap':
        for row in rows:
            del row['timestamps'][-2]; del row['values'][-2]
    _, points = a3.replay(a3.decode_export(rows), end - 70, end)
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
        (await client.post('/api/v1/import/prometheus', content=encode(points, 'a3-vllm'))).raise_for_status()
        (await client.get('/internal/force_flush')).raise_for_status()
    service = Service('a3-vllm')
    try:
        result = await service._history(1, end - step, end, step, 'summary')
        store = result['points'][-1]['mooncake']
        assert store['ssd_capacity'] == {'used': 0, 'total': 1024, 'ratio': 0}
        assert ('ssd_capacity' in store['gap_before']) == (case != 'zero' and step > 5)
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('tier,offset', [('capacity', 1), ('ssd_capacity', 3)])
@pytest.mark.parametrize('step', [5, 15, 60])
async def test_real_vm_rejects_nonzero_usage_with_zero_capacity(tier, offset, step, monkeypatch):
    url = os.environ.get('MONITORING_TEST_VM_URL')
    if not url: pytest.skip('Requires disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:')
    monkeypatch.setattr('monitoring.api.VM', url)
    slot = (offset == 3) * 3 + [5, 15, 60].index(step)
    end = int(time.time() // 60) * 60 - 16000 - slot * 300
    rows = raw()
    for row in rows:
        row['timestamps'] = [(tick // 1000 + end - 170) * 1000 for tick in row['timestamps']]
    rows[offset]['values'][-1] = 32
    rows[offset + 1]['values'][-1] = 0
    _, points = a3.replay(a3.decode_export(rows), end - 70, end)
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
        (await client.post('/api/v1/import/prometheus', content=encode(points, 'a3-vllm'))).raise_for_status()
        (await client.get('/internal/force_flush')).raise_for_status()
    service = Service('a3-vllm')
    try:
        result = await service._history(1, end - step, end, step, 'summary')
        store = result['points'][-1]['mooncake']
        assert store[tier] == {'used': None, 'total': None, 'ratio': None}
        assert tier in store['gap_before']
        other = 'ssd_capacity' if tier == 'capacity' else 'capacity'
        assert store[other]['ratio'] == (0 if tier == 'capacity' else .25)
    finally:
        await service.close()
