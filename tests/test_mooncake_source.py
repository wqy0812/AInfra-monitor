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
