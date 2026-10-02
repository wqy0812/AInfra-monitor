"""Mooncake counter windows must stay within one scrape source identity."""
import json
import os
import time

import httpx
import pytest

from monitoring import api
from monitoring.calculator import parse_prom
from monitoring.replay import decode_export, replay


def observations(changed_label=None, shift=1):
    exported = []
    rates = {'total_get_nums_': 10, 'valid_get_nums_': 9,
             'mem_cache_hit_nums_': 6, 'file_cache_hit_nums_': 3}
    bases = {'total_get_nums_': 2_000_000, 'valid_get_nums_': 1_000_000,
             'mem_cache_hit_nums_': 600_000, 'file_cache_hit_nums_': 400_000}
    for tick in range(100, 241, 5):
        changed = changed_label is not None and tick >= 165
        source = {'job': 'mooncake', 'environment': 'dcu-pd',
                  'instance': 'old:9003', 'node': 'old-node', 'service': 'old-service'}
        if changed:
            source[changed_label] = 'new-source'
        values = {'up': 1, 'master_allocated_bytes': 20, 'master_total_capacity_bytes': 40}
        values.update({name: tick * rate + bases[name] * (1 + shift if changed else 1)
                       for name, rate in rates.items()})
        # Changing the metric/segment inventory within one source is not a restart.
        if tick >= 165:
            values['segment_allocated_bytes'] = 10
        for name, value in values.items():
            metric = {'__name__': name, **source}
            if name == 'segment_allocated_bytes':
                metric['segment'] = 'new-segment'
            exported.append({'metric': metric, 'timestamps': [tick * 1000], 'values': [value]})
    return exported


def assert_ratios(store, expected):
    actual = (store['query_60s']['ratio'], store['tier_query_60s']['memory'],
              store['tier_query_60s']['ssd'])
    if expected is None:
        assert actual == (None, None, None)
    else:
        assert actual == pytest.approx(expected)
    assert store['capacity']['ratio'] == .5


@pytest.mark.parametrize('changed_label', ['instance', 'node', 'service'])
@pytest.mark.parametrize('shift', [-1, 1], ids=['smaller-counters', 'larger-counters'])
def test_source_change_restarts_both_windows_and_recovers(changed_label, shift):
    snapshots, points = replay(decode_export(observations(changed_label, shift)), 100, 240)
    stores = {p['ts']: p['mooncake'] for p in points}
    assert_ratios(stores[160], (.9, .6, .3))
    for tick in range(165, 220, 5):
        assert_ratios(stores[tick], None)
    for tick in range(220, 241, 5):
        assert_ratios(stores[tick], (.9, .6, .3))
    for snapshot in snapshots:
        for field in ('query_60s', 'tier_query_60s'):
            assert snapshot['mooncake']['data'][field] == stores[snapshot['ts']][field]


def test_stable_source_keeps_windows_when_metric_inventory_changes():
    _, points = replay(decode_export(observations()), 100, 240)
    for point in points:
        if point['ts'] >= 160:
            assert_ratios(point['mooncake'], (.9, .6, .3))


def test_source_change_materializes_invalid_ratios_until_window_recovers():
    _, points = replay(decode_export(observations('instance')), 100, 240)
    paths = ('mooncake.query_60s.ratio', 'mooncake.tier_query_60s.memory', 'mooncake.tier_query_60s.ssd')
    for point in points:
        if point['ts'] < 160:
            continue
        flags = {row['labels']['path']: row['value'] for row in parse_prom(api.encode([point]))
                 if row['name'] == 'monitoring_chart_valid'}
        assert all(flags[path] == int(not 165 <= point['ts'] < 220) for path in paths)
        assert flags['mooncake.capacity.ratio'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('shift', [-1, 1], ids=['smaller-counters', 'larger-counters'])
async def test_real_vm_source_change_preserves_history_gaps(tmp_path, monkeypatch, shift):
    url = os.environ.get('MONITORING_TEST_VM_URL')
    if not url:
        pytest.skip('Set MONITORING_TEST_VM_URL to a disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:'), 'Never inject fixtures into a remote service'
    monkeypatch.setattr(api, 'VM', url)
    monkeypatch.setattr(api, 'STATE', tmp_path)
    base = int(time.time() // 5) * 5 - (3600 if shift < 0 else 1800)
    lines = []
    for row in observations('instance', shift):
        labels = dict(row['metric'])
        name = labels.pop('__name__')
        tags = ','.join(k + '=' + json.dumps(v) for k, v in labels.items())
        lines.append(f"{name}{{{tags}}} {row['values'][0]} {row['timestamps'][0] + base * 1000}")
    service = api.Service()
    try:
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            (await client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n')).raise_for_status()
            (await client.get('/internal/force_flush')).raise_for_status()
            groups = await service.raw(base + 100, base + 240)
            _, points = replay(groups, base + 100, base + 240)
            (await client.post('/api/v1/import/prometheus', content=api.encode(points))).raise_for_status()
            (await client.get('/internal/force_flush')).raise_for_status()
        history = await service.history(1, base + 100, base + 240)
        stores = {p['ts'] - base: p['mooncake'] for p in history['points']}
        assert_ratios(stores[160], (.9, .6, .3))
        for tick in range(165, 220, 5):
            assert_ratios(stores[tick], None)
            assert {'query_60s.ratio', 'memory_query', 'ssd_query'} <= set(stores[tick]['gap_before'])
        assert_ratios(stores[220], (.9, .6, .3))
        # A recovered value must still break a coarser line across the hidden gap.
        rows = await service.query(api.history_expression('dcu-pd', 'gaps', 60), base + 220, base + 220, 60)
        flags = {row['metric']['path']: float(row['values'][0][1]) for row in rows}
        assert all(flags[path] == 0 for path in ('mooncake.query_60s.ratio',
                   'mooncake.tier_query_60s.memory', 'mooncake.tier_query_60s.ssd'))
    finally:
        await service.close()
