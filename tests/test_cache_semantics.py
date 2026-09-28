import json
import sqlite3

import pytest

from monitoring.cache_metrics import (effective_counters, host_capacity, store_metrics, TierWindow, QueryWindow, capacity)
from monitoring.calculator import parse_prom
from monitoring.monitor_series import history_points, history_projection, downsample_points


def rows(values=(100, 20, 10, 70), ranks=((0, 0), (0, 1)), missing=None):
    result = []
    for dp, tp in ranks:
        labels = f'engine_type="prefill",model_name="m",dp_rank="{dp}",tp_rank="{tp}",pp_rank="0"'
        result.append(f'sglang:num_running_reqs{{{labels}}} 0')
        for mode, value in zip(('device_hit', 'host_hit', 'storage_hit', 'input'), values):
            if (tp, mode) != missing:
                result.append(f'sglang:prefill_effective_tokens_total{{{labels},mode="{mode}"}} {value}')
    return parse_prom('\n'.join(result))


def sample(ts, values=(100, 20, 10, 70), **kwargs):
    return {'ts': ts, 'cache_schema': 'prefill-effective-v1', 'cache_effective': effective_counters(rows(values, **kwargs))}


def test_dedup_and_real_dp_groups():
    raw = effective_counters(rows(ranks=((0, 0), (0, 1), (1, 0), (1, 1))))
    assert len(raw['groups']) == 2
    assert len(raw['topology']) == 4
    assert effective_counters(rows(missing=(0, 'host_hit'))) is None
    assert effective_counters(rows() + [rows()[1]]) is None
    assert effective_counters([]) is None


def test_tier_denominator_weighted_window():
    window = TierWindow()
    for i in range(13):
        result = window.add(sample(i * 5, (900 if i else 0, i * 10, i * 5, i * 20)))
    assert result['input_tokens'] == 1320
    assert result['ratio'] == pytest.approx(1080 / 1320)
    assert result['device'] == pytest.approx(900 / 1320)
    assert result['host'] == pytest.approx(120 / 1320)
    assert result['storage'] == pytest.approx(60 / 1320)
    assert result['ratio'] == pytest.approx(sum(result[k] for k in ('device', 'host', 'storage')))


@pytest.mark.parametrize('failure', ['reset', 'gap', 'missing', 'rank', 'topology'])
def test_window_invalidates(failure):
    w = TierWindow()
    for i in range(13):
        value = w.add(sample(i * 5, (i * 10, i * 10, i * 10, i * 10)))
    assert value['ratio'] == .75
    next_sample = {'reset': sample(65, (200, 0, 200, 200)), 'gap': sample(90),
                   'missing': sample(65, missing=(0, 'host_hit')), 'rank': sample(65, ranks=((0, 1),)),
                   'topology': sample(65, ranks=((0, 0), (0, 1), (0, 2)))}[failure]
    assert w.add(next_sample)['ratio'] is None


@pytest.mark.parametrize('active', [True, False])
def test_no_traffic_vs_zero_hit(active):
    w = TierWindow()
    for i in range(13):
        result = w.add(sample(i * 5, (0, 0, 0, i * 100 if active else 0)))
    assert result['ratio'] == (0 if active else None)
    assert result['host'] == (0 if active else None)


def test_capacity_pairing_missing_and_overfull():
    parsed = parse_prom('''sglang:hicache_host_used_tokens{engine_type="prefill",tp_rank="1"} 40
sglang:hicache_host_total_tokens{engine_type="prefill",tp_rank="0"} 100
sglang:hicache_host_used_tokens{engine_type="prefill",tp_rank="0"} 20
segment_allocated_bytes{segment="a"} 10
segment_total_capacity_bytes{segment="b"} 100
master_allocated_bytes 30
master_total_capacity_bytes 100''')
    host = host_capacity(parsed)
    assert host['representative']['ratio'] == .2
    assert host['ranks'][1]['ratio'] is None
    store = store_metrics(parsed)
    assert store['capacity']['ratio'] == .3
    assert all(x['ratio'] is None for x in store['segments'])
    assert all(capacity(a, b)['ratio'] is None for a, b in [(0, 0), (None, 5), (7, 5), (float('nan'), 5)])


def test_store_query_window():
    w = QueryWindow()
    for i in range(13):
        value = w.add({'ts': i * 5, 'query_counters': {'valid': i * 9, 'total': i * 10}})
    assert value['ratio'] == .9
    assert w.add({'ts': 65, 'query_counters': {'valid': 0, 'total': 200}})['ratio'] is None
    assert w.add(None)['ratio'] is None


def snap(ts, data, status='ok'):
    return {'ts': ts, 'nodes': {'prefill': {'metrics': {'status': status, 'observed_at': ts, 'data': data}}},
            'mooncake': {'status': status, 'observed_at': ts, 'data': {'ts': ts, 'capacity': capacity(50, 100), 'query_counters': {'valid': ts, 'total': ts * 2}}}}


def test_live_history_parity_and_missing_schema():
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE snapshots(ts REAL, data TEXT)')
    snapshots = []
    w = TierWindow()
    for i in range(13):
        data = sample(i * 5, (i * 100, i * 20, i * 10, i * 70))
        data['hicache'] = {'representative': capacity(50, 100)}
        value = w.add(data)
        snapshots.append(snap(i * 5, data))
    # A present but invalid new schema must never fall back to legacy accounting.
    broken = sample(65);broken.update(cache_effective=None,input_tokens=100,cache_sources={'device':50})
    snapshots.append(snap(65, broken))
    for s in snapshots:
        connection.execute('INSERT INTO snapshots VALUES(?,?)', (s['ts'], json.dumps(s)))
    restored = [json.loads(r[0]) for r in connection.execute('SELECT ' + history_projection() + ' FROM snapshots')]
    points = history_points(restored, 0)
    assert points[12]['nodes']['prefill']['cache_60s'] == value
    assert points[12]['mooncake']['query_60s']['ratio'] == .5
    assert points[12]['nodes']['prefill']['hicache']['representative']['ratio'] == .5
    assert points[13]['nodes']['prefill']['cache_60s']['ratio'] is None
    assert points[13]['nodes']['prefill']['cache_60s']['semantics'] == 'prefill-effective-v1'


def test_legacy_history_and_decimation_fault_boundaries():
    old = [snap(i * 5, {'ts': i * 5, 'input_tokens': i * 100, 'cache_sources': {'device': i * 50}}) for i in range(13)]
    points = history_points(old, 0)
    assert points[-1]['nodes']['prefill']['cache_60s']['ratio'] == .5
    assert points[-1]['nodes']['prefill']['cache_60s']['host'] is None
    long = [snap(i * 5, sample(i * 5, (i, i, i, i)), 'error' if i == 61 else 'ok') for i in range(1500)]
    stride, reduced = downsample_points(history_points(long, 0))
    assert stride == 3
    assert 'capacity' in next(p for p in reduced if p['ts'] == 315)['mooncake']['gap_before']
    assert 'cache' in next(p for p in reduced if p['ts'] == 315)['nodes']['prefill']['gap_before']
    # Do not bridge a semantic transition even when both endpoints have a value.
    p = points[-1]
    q = json.loads(json.dumps(p));q['ts'] += 5;q['nodes']['prefill']['cache_60s']['semantics'] = 'prefill-effective-v1'
    assert 'cache' in downsample_points([p,q])[1][1]['nodes']['prefill']['gap_before']
