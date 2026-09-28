import importlib.util
from pathlib import Path
import pytest

SPEC = importlib.util.spec_from_file_location('time_navigation_release', Path(__file__).resolve().parents[1] / 'deploy/time-navigation-20260925/release_api.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_wrong_live_source_blocks_before_building(tmp_path, monkeypatch):
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'read', lambda _: {'before_image': 'old', 'before_sha256': 'expected'})
    monkeypatch.setattr(release, 'find', lambda _: None)
    monkeypatch.setattr(release, 'inspect', lambda _: {'Image': 'old', 'State': {'Running': True}})
    monkeypatch.setattr(release, 'source_hash', lambda _: 'unexpected')
    monkeypatch.setattr(release.subprocess, 'run', lambda *a, **k: pytest.fail('Build must not start'))
    with pytest.raises(AssertionError, match='Live source changed'):
        release.prepare()
    assert not list(tmp_path.iterdir())


def test_uncertain_create_preserves_state_without_restoring(monkeypatch):
    old = {'Id': 'old-id', 'Config': {'Env': ['X=preserve'], 'Cmd': ['uvicorn']}, 'HostConfig': {'NetworkMode': 'host'}}
    records = {
        'container-before.json': old,
        'prepared.json': {'image': 'candidate-image', 'protected': {'vm': ['id', 'started']}},
        'manifest.json': {'before_sha256': 'source'},
        'candidate-api.json': {'passed': True, 'image': 'candidate-image'},
    }
    calls = []
    monkeypatch.setattr(release, 'read', records.__getitem__)
    monkeypatch.setattr(release, 'inspect', lambda _: old)
    monkeypatch.setattr(release, 'source_hash', lambda _: 'source')
    monkeypatch.setattr(release, 'find', lambda _: None)
    monkeypatch.setattr(release, 'protected', lambda: {'vm': ['id', 'started']})
    monkeypatch.setattr(release, 'get', lambda _: {'status': 'ok'})
    monkeypatch.setattr(release, 'save', lambda name, value: records.update({name: value}))
    monkeypatch.setattr(release, 'cmd', lambda *args: calls.append(args))

    def uncertain(name, config):
        assert name == release.NAME
        assert config['Env'] == old['Config']['Env']
        assert config['HostConfig'] == old['HostConfig']
        assert config['Labels']['monitoring.transaction'] == release.BACKUP
        raise OSError('lost response after Docker created container')

    restored = []
    monkeypatch.setattr(release, 'create', uncertain)
    monkeypatch.setattr(release, 'restore', lambda *args: restored.append(args))
    with pytest.raises(OSError, match='lost response'):
        release.switch()
    assert not restored
    assert calls == [('docker', 'stop', 'old-id'), ('docker', 'rename', 'old-id', release.BACKUP)]
    assert 'rollback.json' not in records
    assert 'Labels' not in old['Config']


def test_summary_comparison_keeps_zeros_and_gap_markers():
    full = {'points': [{'nodes': {'prefill': {'value': 0, 'resources': {'card': 1}, 'gap_before': ['value', 'resources.card']}},
                        'mooncake': {'value': None, 'resources': {}, 'gap_before': ['value']}}]}
    reduced = release.summary(full)
    assert reduced['points'][0]['nodes']['prefill'] == {'value': 0, 'gap_before': ['value']}
    assert reduced['points'][0]['mooncake'] == {'value': None, 'gap_before': ['value']}
    assert 'resources' in full['points'][0]['nodes']['prefill']
