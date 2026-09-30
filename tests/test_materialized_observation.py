"""A maintenance-window batch completes when normal scheduling is ready."""
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
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
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
def test_maintenance_waits_only_until_normal_scheduling_recovers(tmp_path, monkeypatch, flag):
    environment(tmp_path, monkeypatch,
        scheduling=lambda elapsed: {flag: elapsed < 10},
        unhealthy=lambda elapsed: elapsed < 10)
    release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert report['passed']
    assert report['end'] - report['start'] == 10
    assert report['normal_start'] == report['start'] + 10
    assert report['normal_seconds'] == 0
    assert any(r['state'] == 'waiting_for_normal_scheduling' for r in report['records'])


@pytest.mark.parametrize('flag', ['backfill_priority_active', 'parallel_a3_backfill'])
def test_permanent_maintenance_times_out_without_passing(tmp_path, monkeypatch, flag):
    clock, rollbacks = environment(tmp_path, monkeypatch, scheduling=lambda elapsed: {flag: True})
    with pytest.raises(TimeoutError, match='observation'):
        release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert not report['passed'] and report['state'] == 'failed'
    assert clock[0] == 90 and not rollbacks


def test_healthy_batch_is_accepted_immediately(tmp_path, monkeypatch):
    clock, rollbacks = environment(tmp_path, monkeypatch)
    release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert report['passed'] and clock[0] == 0 and not rollbacks
    assert report['mode'] == 'maintenance-window'


def test_transient_startup_failure_retries_until_ready(tmp_path, monkeypatch):
    clock, rollbacks = environment(tmp_path, monkeypatch, unhealthy=lambda elapsed: elapsed == 0)
    release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert report['passed'] and clock[0] == 5 and not rollbacks
    assert report['normal_start'] == report['start'] + 5


@pytest.mark.parametrize('fault', ['model', 'unknown_scheduling'])
def test_sustained_failure_is_saved_without_rollback(tmp_path, monkeypatch, fault):
    _, rollbacks = environment(tmp_path, monkeypatch,
        scheduling=lambda elapsed: {'parallel_a3_backfill': None} if fault == 'unknown_scheduling' else {},
        unhealthy=lambda elapsed: fault == 'model')
    with pytest.raises(AssertionError):
        release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert not report['passed'] and report['state'] == 'failed'
    assert not rollbacks and report['error']
    assert report['automatic_rollback'] is False and report['recovery'] == 'fix_forward'


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
    assert clock[0] == 10 and not rollbacks
    assert not json.loads((tmp_path / 'batch-observation.json').read_text())['passed']


def test_healthy_acceptance_does_not_require_service_identity_snapshots(tmp_path, monkeypatch):
    clock, rollbacks = environment(tmp_path, monkeypatch)
    monkeypatch.setattr(release, 'fingerprint', lambda: pytest.fail('Continuity snapshot is not an upgrade prerequisite'))
    release.observe(tmp_path, 'cpu')
    report = json.loads((tmp_path / 'batch-observation.json').read_text())
    assert report['passed'] and report['normal_seconds'] == 0
    assert clock[0] == 0 and not rollbacks


def test_observation_report_write_failure_preserves_original_error(tmp_path, monkeypatch):
    _, rollbacks = environment(tmp_path, monkeypatch)
    failure = KeyboardInterrupt('observation interrupted')
    def health():
        raise failure
    original_save = release.save
    def save(root, name, value):
        if value.get('state') == 'failed':
            raise OSError('disk unavailable')
        original_save(root, name, value)
    monkeypatch.setattr(release, 'read_health', health)
    monkeypatch.setattr(release, 'save', save)
    with pytest.raises(KeyboardInterrupt) as caught:
        release.observe(tmp_path, 'cpu')
    assert caught.value is failure and not rollbacks
    assert any('disk unavailable' in note for note in failure.__notes__)
    assert not json.loads((tmp_path / 'batch-observation.json').read_text())['passed']
