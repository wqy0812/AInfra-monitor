"""CPU recording protocol: independent torn-write and ownership regressions."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from monitoring.perses_acceleration import AccelerationService, metric_identity
from monitoring.perses_recording import (
    RecordingService, KIND, PUBLICATION, GENERATION, completed, recording_rules, record_name,
)

CATALOG = json.loads((Path(__file__).parents[1] / 'monitoring/perses_acceleration_catalog.json').read_text())
PANEL = CATALOG['panels'][0]
NAME = record_name(PANEL, 5)
PUB = 'a' * 32


def row(kind, value, *, publication=PUB, timestamp=100, node=None, generation=1):
    labels = {'__name__': NAME, KIND: kind, PUBLICATION: publication, GENERATION: str(generation)}
    if node is not None:
        labels['node'] = node
    return {'metric': labels, 'values': [[timestamp, str(value)]]}


def test_same_evaluation_requires_manifest_and_every_value():
    manifest = row('complete', 2)
    a, b = row('value', 3, node='a'), row('value', 7, node='b')
    assert completed([manifest], [100], NAME) == {}
    assert completed([a, b], [100], NAME) == {}
    assert completed([manifest, a], [100], NAME) == {}
    assert completed([manifest, a, b], [100], NAME) == {
        100: {metric_identity({'node': 'a'}): '3', metric_identity({'node': 'b'}): '7'}}
    assert completed([row('complete', 0)], [100], NAME) == {100: {}}
    assert completed([], [100], NAME) == {}


def test_overlapping_processes_do_not_combine_even_if_counts_match():
    rows = [row('complete', 2), row('value', 3, node='a'),
            row('value', 7, node='b', publication='b' * 32)]
    assert completed(rows, [100], NAME) == {}
    # Even a full publication plus a partial second one is ambiguous.
    rows.append(row('value', 7, node='b'))
    assert completed(rows, [100], NAME) == {}


@pytest.mark.parametrize('bad', ['-1', '1.5', 'nan', 'inf'])
def test_bad_completion_is_a_miss(bad):
    assert completed([row('complete', bad)], [100], NAME) == {}


def test_duplicate_series_and_markers_are_not_counted_twice():
    a = row('value', 1, node='a')
    assert completed([row('complete', 2), a, a], [100], NAME) == {}
    assert completed([row('complete', 0)] * 2, [100], NAME) == {}
    assert completed([row('complete', 0), a], [100], NAME) == {}


@pytest.mark.parametrize('change', ['kind', 'publication', 'metric', 'timestamp', 'manifest_label'])
def test_malformed_protocol_raises_for_request_level_fallback(change):
    r = row('complete', 0)
    if change == 'kind': r['metric'][KIND] = 'unknown'
    if change == 'publication': r['metric'][PUBLICATION] = 'reused-human-name'
    if change == 'metric': r['metric']['__name__'] = 'wrong_revision'
    if change == 'timestamp': r['values'][0][0] = 99
    if change == 'manifest_label': r['metric']['node'] = 'unexpected'
    with pytest.raises(ValueError):
        completed([r], [100], NAME)


def test_native_has_no_legacy_writer_or_state_reads(tmp_path):
    (tmp_path / 'perses-acceleration-watermarks.json').write_text('not valid json')
    candidate = RecordingService('http://vm', tmp_path, CATALOG, client=object())
    assert not hasattr(candidate, 'materialize')
    assert not hasattr(candidate, 'run_worker')
    assert not hasattr(candidate, 'watermarks')
    assert candidate.status()['rule_count'] == 126


def test_newest_generation_wins_only_when_complete():
    old = [row('complete', 1), row('value', 3, node='a')]
    partial = [row('complete', 1, generation=2, publication='b'*32)]
    assert completed(old + partial, [100], NAME) == {}
    latest = partial + [row('value', 7, node='a', generation=2, publication='b'*32)]
    assert completed(old + latest, [100], NAME) == {100: {metric_identity({'node':'a'}): '7'}}
    assert completed(old, [100], NAME, minimum_generation=2) == {}
    assert completed(old + [row('complete', 0, generation=2)], [100], NAME) == {100: {}}


def test_rule_identity_tracks_expression_and_only_cpu_is_selected():
    changed = dict(PANEL, expression=PANEL['expression'] + ' + 0')
    assert record_name(changed, 5) != NAME
    config = recording_rules(CATALOG, PUB, 1)
    assert len(config['groups']) == 7
    assert all(len(g['rules']) == 18 for g in config['groups'])
    assert len({r['record'] for g in config['groups'] for r in g['rules']}) == 126
