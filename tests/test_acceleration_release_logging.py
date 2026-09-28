"""A lost status-output pipe must not roll back a healthy, accepted release."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import release_api as release


def setup_observation(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'time', SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600)))
    (tmp_path / 'shadow-started.json').write_text(json.dumps({'at': 1000, 'container_id': 'candidate'}))
    (tmp_path / 'prepared.json').write_text(json.dumps({'protected': {'vm': 'unchanged'}}))
    monkeypatch.setattr(release, 'inspect', lambda name: {'Id': 'candidate'})
    monkeypatch.setattr(release, 'protected', lambda: {'vm': 'unchanged'})
    monkeypatch.setattr(release, 'health', lambda: {'status': 'ok',
        'environments': {'env': {'error': None, 'processed_at': clock[0] - 5}},
        'perses_acceleration': {'state_error': None}})
    rollback = []
    monkeypatch.setattr(release, 'rollback', lambda: rollback.append(True))
    return rollback


def test_closed_output_after_success_never_rolls_back(tmp_path, monkeypatch):
    rollback = setup_observation(tmp_path, monkeypatch)
    def broken(*args, **kwargs):
        raise BrokenPipeError('status stream closed')
    monkeypatch.setattr(release, 'print', broken, raising=False)
    release.observe()
    assert json.loads((tmp_path / 'shadow-observation.json').read_text())['passed']
    assert not rollback


def test_real_health_failure_is_recorded_and_still_rolls_back(tmp_path, monkeypatch):
    rollback = setup_observation(tmp_path, monkeypatch)
    monkeypatch.setattr(release, 'health', lambda: {'status': 'error',
        'environments': {'env': {'error': 'source stopped', 'processed_at': 0}}})
    with pytest.raises(RuntimeError, match='Sustained health'):
        release.observe()
    assert rollback == [True]
    failure = json.loads((tmp_path / 'observation-failure.json').read_text())
    assert failure['type'] == 'RuntimeError'


def test_deferred_observation_counts_from_actual_start(tmp_path, monkeypatch):
    setup_observation(tmp_path, monkeypatch)
    (tmp_path / 'shadow-started.json').write_text(json.dumps({'at': 100, 'container_id': 'candidate'}))
    release.observe()
    report = json.loads((tmp_path / 'shadow-observation.json').read_text())
    assert report['release_started_at'] == 100
    assert report['started_at'] == 1000
    assert report['ended_at'] - report['started_at'] >= 1800


@pytest.mark.parametrize('acceleration', [
    {'backfill_priority_active': True},
    {'parallel_a3_backfill': True},
    {'disabled_groups': ['a3']},
    {'jobs': [{'job': 'a3:v1:5', 'lag_seconds': 0, 'error': 'write error'}]},
    {'jobs': [{'job': 'a3:v1:5', 'lag_seconds': 900, 'error': None}]},
])
def test_maintenance_or_worker_fault_cannot_pass_normal_observation(tmp_path, monkeypatch, acceleration):
    rollback = setup_observation(tmp_path, monkeypatch)
    healthy = release.health
    def faulty():
        value = healthy(); value['perses_acceleration'].update(acceleration)
        return value
    monkeypatch.setattr(release, 'health', faulty)
    with pytest.raises(RuntimeError, match='Sustained health'): release.observe()
    assert rollback == [True]
