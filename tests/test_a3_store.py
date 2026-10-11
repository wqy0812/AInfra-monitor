import copy
import json
import os
import time

import httpx
import pytest
from monitoring import a3, a3_store


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
