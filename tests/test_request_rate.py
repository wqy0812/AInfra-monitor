import json
import pytest
from monitoring.calculator import Calculator, request_counters
from monitoring.replay import decode_export, replay


def rows(streaming=100, nonstreaming=10):
    return [{'name': 'sglang:num_requests_total',
             'labels': {'model_name': 'm', 'engine_type': 'decode', 'is_streaming': flag},
             'value': value}
            for flag, value in [('true', streaming), ('false', nonstreaming)]
            if value is not None]


def test_streaming_partitions_actual_interval_and_idle():
    calc = Calculator()
    first = calc.metrics('decode', rows(), 100)
    assert first['requests'] == 110
    assert first['rates']['requests'] is None
    assert calc.metrics('decode', rows(104, 11), 104)['rates']['requests'] == 1.25
    assert calc.metrics('decode', rows(104, 11), 109)['rates']['requests'] == 0


@pytest.mark.parametrize('after,seconds', [
    (rows(1, 120), 5),  # a reset must not be hidden by the other partition growing
    (rows(None, 120), 5),
    ([], 5),
    (rows(104, 11), 20),
    (rows(104, 11), 0),
    (rows(104, 11), -5),
])
def test_reset_missing_partition_and_gap(after, seconds):
    calc = Calculator()
    calc.metrics('decode', rows(), 100)
    assert calc.metrics('decode', after, 100 + seconds)['rates']['requests'] is None


def test_new_nonstreaming_partition_requires_a_fresh_baseline():
    calc = Calculator()
    calc.metrics('decode', rows(100, None), 100)
    assert calc.metrics('decode', rows(102, 1), 105)['rates']['requests'] is None
    assert calc.metrics('decode', rows(103, 2), 110)['rates']['requests'] == .4


def test_legacy_single_counter_and_identity_change():
    old = rows(100, None)
    del old[0]['labels']['is_streaming']
    after = json.loads(json.dumps(old));after[0]['value'] = 110
    calc = Calculator();calc.metrics('decode', old, 100)
    assert calc.metrics('decode', after, 105)['rates']['requests'] == 2
    after[0]['labels']['model_name'] = 'replacement'
    assert calc.metrics('decode', after, 110)['rates']['requests'] is None


@pytest.mark.parametrize('kind', ['rank', 'model', 'extra_label', 'duplicate', 'mixed', 'unknown', 'negative', 'nan'])
def test_ambiguous_or_invalid_counters_stay_missing(kind):
    data = rows()
    data[1]['labels']['is_streaming'] = 'true'
    if kind == 'rank':
        for i, row in enumerate(data):row['labels']['tp_rank'] = str(i)
    elif kind == 'model':data[1]['labels']['model_name'] = 'other'
    elif kind == 'extra_label':data[1]['labels']['worker'] = 'other'
    elif kind == 'duplicate':data.append(data[0])
    elif kind == 'mixed':
        for r in data:del r['labels']['is_streaming']
    elif kind == 'unknown':
        for r in data:r['labels']['is_streaming'] = 'unknown'
    elif kind == 'negative':data[1]['value'] = -1
    elif kind == 'nan':data[1]['value'] = float('nan')
    assert request_counters(data) is None


def test_raw_vm_replay_produces_kpi_and_chart_with_same_rate():
    exports = []
    for before, after in zip(rows(), rows(104, 11)):
        exports.append({'metric': dict(before['labels'], __name__=before['name'],
                                      job='sglang-decode', instance='endpoint'),
                        'timestamps': [100000, 105000], 'values': [before['value'], after['value']]})
    snaps, points = replay(decode_export(exports), 100, 105)
    assert snaps[-1]['nodes']['decode']['metrics']['data']['rates']['requests'] == 1
    assert points[-1]['nodes']['decode']['requests'] == 1
