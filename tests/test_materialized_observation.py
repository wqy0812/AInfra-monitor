"""Only a continuous normal-scheduling window may finish batch observation."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import materialized_release as release


def environment(tmp_path, monkeypatch, scheduling=lambda elapsed: {}, unhealthy=lambda elapsed: False):
    clock = [0.0]
    monkeypatch.setattr(release, 'time', SimpleNamespace(
        time=lambda: 100000 + clock[0], monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + 60)))
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'steps': [60], 'panels': [{'id': 'cpu', 'revision': 'v1', 'group': 'cpu'}]}))
    monkeypatch.setattr(release, 'CATALOG', catalog)
    monkeypatch.setattr(release, 'fingerprint', lambda: [])
    rollbacks = []
    monkeypatch.setattr(release, 'rollback', lambda root, group: rollbacks.append(group))
    def read_health():
        return {'status': 'degraded' if unhealthy(clock[0]) else 'ok',
            'environments': {'env': {'error': None, 'processed_at': 99995 + clock[0]}},
            'perses_acceleration': {'disabled_groups': [], 'state_error': None,
                'backfill_priority_active': False, 'parallel_a3_backfill': False,
                'jobs': [{'job': 'cpu:v1:60', 'started_at': 0, 'processed_at': 100800,
                          'lag_seconds': 0, 'error': None}], **scheduling(clock[0])}}
    # The observer must inspect maintenance flags before enforcing model health.
    monkeypatch.setattr(release, 'read_health', read_health)
    return clock, rollbacks


@pytest.mark.parametrize('flag', ['backfill_priority_active', 'parallel_a3_backfill'])
def test_maintenance_restarts_the_full_normal_observation(tmp_path, monkeypatch, flag):
    environment(tmp_path, monkeypatch,
        scheduling=lambda elapsed: {flag: 600 <= elapsed < 900},
        unhealthy=lambda elapsed: 600 <= elapsed < 900)
    release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert report['passed']
    assert report['end'] - report['start'] == 2700
    assert report['normal_start'] == report['start'] + 900
    assert report['normal_seconds'] >= 1800
    assert any(r['state'] == 'waiting_for_normal_scheduling' for r in report['records'])


@pytest.mark.parametrize('flag', ['backfill_priority_active', 'parallel_a3_backfill'])
def test_permanent_maintenance_times_out_without_passing(tmp_path, monkeypatch, flag):
    clock, rollbacks = environment(tmp_path, monkeypatch, scheduling=lambda elapsed: {flag: True})
    with pytest.raises(TimeoutError, match='observation'):
        release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert not report['passed'] and report['state'] == 'failed'
    assert clock[0] == 3600 and rollbacks == ['cpu']


def test_normal_observation_completes_at_30_minutes(tmp_path, monkeypatch):
    clock, rollbacks = environment(tmp_path, monkeypatch)
    release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert report['passed'] and clock[0] == 1800 and not rollbacks


def test_transient_health_failure_restarts_normal_window(tmp_path, monkeypatch):
    clock, rollbacks = environment(tmp_path, monkeypatch, unhealthy=lambda elapsed: elapsed == 600)
    release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert report['passed'] and clock[0] == 2460 and not rollbacks
    assert report['normal_start'] == report['start'] + 660


@pytest.mark.parametrize('fault', ['model', 'unknown_scheduling', 'container'])
def test_sustained_failure_is_saved_and_rolled_back(tmp_path, monkeypatch, fault):
    _, rollbacks = environment(tmp_path, monkeypatch,
        scheduling=lambda elapsed: {'parallel_a3_backfill': None} if fault == 'unknown_scheduling' else {},
        unhealthy=lambda elapsed: fault == 'model')
    if fault == 'container':
        identities = iter([[], ['replacement'], ['replacement'], ['replacement']])
        monkeypatch.setattr(release, 'fingerprint', lambda: next(identities))
    with pytest.raises(AssertionError):
        release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert not report['passed'] and report['state'] == 'failed'
    assert rollbacks == ['cpu'] and report['error']


@pytest.mark.parametrize('fault', ['state_error', 'disabled', 'worker', 'backfill'])
def test_maintenance_does_not_hide_worker_failures(tmp_path, monkeypatch, fault):
    clock, rollbacks = environment(tmp_path, monkeypatch,
        scheduling=lambda elapsed: {'backfill_priority_active': True})
    original = release.read_health
    def faulty():
        result = original(); accelerator = result['perses_acceleration']
        if fault == 'state_error': accelerator['state_error'] = 'Invalid state'
        elif fault == 'disabled': accelerator['disabled_groups'] = ['cpu']
        elif fault == 'worker': accelerator['jobs'][0]['error'] = 'Write failure'
        else: accelerator['jobs'][0]['backfill'] = {'error': 'Read-back failure'}
        return result
    monkeypatch.setattr(release, 'read_health', faulty)
    with pytest.raises(AssertionError):
        release.observe(tmp_path, 'cpu')
    assert clock[0] == 120 and rollbacks == ['cpu']
    assert not json.loads((tmp_path / 'batch-observation.json').read_text())['passed']


def test_wall_clock_jump_cannot_finish_observation_early(tmp_path, monkeypatch):
    clock, rollbacks = environment(tmp_path, monkeypatch)
    monkeypatch.setattr(release.time, 'time', lambda: 100000 + clock[0] + (3600 if clock[0] >= 600 else 0))
    original = release.read_health
    def synchronized_clock():
        result = original()
        result['environments']['env']['processed_at'] = release.time.time() - 5
        return result
    monkeypatch.setattr(release, 'read_health', synchronized_clock)
    release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert report['passed'] and report['normal_seconds'] == 1800
    assert clock[0] == 1800 and not rollbacks
