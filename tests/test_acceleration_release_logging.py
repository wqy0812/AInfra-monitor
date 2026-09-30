"""Release failures preserve live state and evidence for forward repair."""
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
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
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


def test_real_health_failure_is_recorded_without_rollback(tmp_path, monkeypatch):
    rollback = setup_observation(tmp_path, monkeypatch)
    monkeypatch.setattr(release, 'health', lambda: {'status': 'error',
        'environments': {'env': {'error': 'source stopped', 'processed_at': 0}}})
    with pytest.raises(RuntimeError, match='Sustained health'):
        release.observe()
    assert not rollback
    failure = json.loads((tmp_path / 'observation-failure.json').read_text())
    assert failure['type'] == 'RuntimeError'
    assert failure['automatic_rollback'] is False and failure['recovery'] == 'fix_forward'


def test_acceptance_records_actual_start_without_a_soak_delay(tmp_path, monkeypatch):
    setup_observation(tmp_path, monkeypatch)
    (tmp_path / 'shadow-started.json').write_text(json.dumps({'at': 100, 'container_id': 'candidate'}))
    release.observe()
    report = json.loads((tmp_path / 'shadow-observation.json').read_text())
    assert report['release_started_at'] == 100
    assert report['started_at'] == 1000
    assert report['ended_at'] == report['started_at']
    assert report['mode'] == 'maintenance-window'


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
    assert not rollback


@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
@pytest.mark.parametrize('report_writable', [True, False])
def test_uncertain_switch_never_restores_or_disables_groups(tmp_path, monkeypatch, error_type, report_writable):
    old = {'Id': 'old', 'Config': {'Env': [], 'Cmd': ['uvicorn']}, 'HostConfig': {}}
    records = {'container-before.json': old, 'prepared.json': {'image': 'candidate', 'protected': []},
               'manifest.json': {'before_sha256': 'hash'}, 'candidate-api.json': {'image': 'candidate'}}
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'read', records.__getitem__)
    monkeypatch.setattr(release, 'inspect', lambda _: old)
    monkeypatch.setattr(release, 'source_hash', lambda _: 'hash')
    monkeypatch.setattr(release, 'find', lambda _: None)
    monkeypatch.setattr(release, 'protected', lambda: [])
    monkeypatch.setattr(release, 'health', lambda: {'status': 'ok'})
    commands = []
    monkeypatch.setattr(release, 'cmd', lambda *args: commands.append(args))
    monkeypatch.setattr(release, 'rollback', lambda: pytest.fail('Rollback must never run'))
    failure = error_type('create response lost')
    def create(*args):
        raise failure
    monkeypatch.setattr(release, 'create', create)
    save = release.save
    def save_report(name, value):
        if name == 'switch-failure.json' and not report_writable:
            raise OSError('disk unavailable')
        save(name, value)
    monkeypatch.setattr(release, 'save', save_report)
    with pytest.raises(error_type) as caught:
        release.switch()
    assert caught.value is failure
    assert commands == [('docker', 'stop', 'old'), ('docker', 'rename', 'old', release.BACKUP)]
    assert (tmp_path / 'transaction.json').exists()
    assert not (tmp_path / 'shadow-started.json').exists()
    if report_writable:
        assert json.loads((tmp_path / 'switch-failure.json').read_text())['automatic_rollback'] is False
    else:
        assert any('disk unavailable' in note for note in failure.__notes__)
