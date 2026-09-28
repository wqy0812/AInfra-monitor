import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import continue_materialized as continuation


def fixture(tmp_path):
    for group in ('cpu', 'dcu', 'a3'): (tmp_path / ('batch-' + group)).mkdir()
    (tmp_path / 'shadow-started.json').write_text('{"container_id":"api","image":"image"}')
    browser = tmp_path / 'browser.json'; browser.write_text('{}')
    return browser


def accepted(root):
    (root / 'batch-before.json').write_text('{}')
    (root / 'batch-admission.json').write_text('{"passed":true}')


def observed(root):
    accepted(root)
    (root / 'batch-publication.json').write_text('{}')
    (root / 'batch-observation.json').write_text('{"passed":true}')


def test_failed_admission_is_not_a_publication_and_does_not_block_other_groups(tmp_path):
    fixture(tmp_path); observed(tmp_path / 'batch-cpu')
    root = tmp_path / 'batch-dcu'; (root / 'batch-before.json').write_text('{}')
    (root / 'batch-admission.json').write_text('{"passed":false}')
    assert continuation.next_action(tmp_path) == ('admit', 'a3', {'cpu': 'observed', 'dcu': 'admission_failed', 'a3': 'pending'})
    observed(tmp_path / 'batch-a3')
    action, group, states = continuation.next_action(tmp_path)
    assert action == 'finished' and group is None and states['dcu'] == 'admission_failed'


def test_wait_for_incomplete_observation_and_never_reprepare_snapshot(tmp_path):
    fixture(tmp_path); root = tmp_path / 'batch-cpu'; accepted(root)
    assert continuation.next_action(tmp_path)[0] == 'publish'
    (root / 'batch-publication.json').write_text('{}')
    assert continuation.next_action(tmp_path)[0] == 'incomplete_observation'


def test_proc_detection_ignores_shell_and_other_releases(tmp_path):
    for pid, args in [('10', ['python3', '-u', 'deploy/admit_when_ready.py', '--build', 'api-build-v6']),
                      ('11', ['sh', '-c', 'python3 deploy/admit_when_ready.py --build api-build-v6']),
                      ('12', ['python3', 'deploy/admit_when_ready.py', '--build', 'api-build-v5']),
                      ('13', ['python3', 'deploy/materialized_release.py', 'observe', '--evidence', 'api-build-v6/batch-cpu']),
                      ('14', ['sh', '-c', 'python3 deploy/admit_when_ready.py --build api-build-v6 > api-build-v6/cpu-release.log'])]:
        d = tmp_path / pid; d.mkdir(); (d / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in args))
    assert {w['pid'] for w in continuation.active_workers(Path('api-build-v6'), tmp_path)} == {10, 13, 14}


def test_controller_waits_for_owner_then_publishes_and_observes_before_next_prepare(tmp_path, monkeypatch):
    browser = fixture(tmp_path); clock = [0]; calls = []; busy = [True]
    monkeypatch.setattr(continuation.time, 'time', lambda: clock[0])
    monkeypatch.setattr(continuation, 'fingerprint', lambda: [{'name': '/monitoring-api', 'id': 'api', 'image': 'image'}])
    monkeypatch.setattr(continuation, 'active_workers', lambda *a: [{'pid': 123}] if busy[0] else [])
    def sleep(_):
        assert not calls; clock[0] += 10; busy[0] = False
        observed(tmp_path / 'batch-cpu'); accepted(tmp_path / 'batch-dcu')
    monkeypatch.setattr(continuation.time, 'sleep', sleep)
    monkeypatch.setattr(continuation.release, 'health', lambda: {})
    def apply(root, group):
        calls.append(('apply', group)); (root / 'batch-publication.json').write_text('{}')
    def observe(root, group):
        calls.append(('observe', group)); (root / 'batch-observation.json').write_text('{"passed":true}')
    def admit(*args):
        assert continuation.release.batch_terminal(tmp_path / 'batch-dcu') == 'observed'
        calls.append(('admit', 'a3')); accepted(tmp_path / 'batch-a3')
    monkeypatch.setattr(continuation.release, 'apply', apply)
    monkeypatch.setattr(continuation.release, 'observe', observe)
    monkeypatch.setattr(continuation.admission, 'main', admit)
    continuation.main(tmp_path, browser, 1)
    assert calls == [('apply', 'dcu'), ('observe', 'dcu'), ('admit', 'a3'), ('apply', 'a3'), ('observe', 'a3')]
    assert json.loads((tmp_path / 'continuation-progress.json').read_text())['passed']


def test_changed_api_stops_without_publication(tmp_path, monkeypatch):
    browser = fixture(tmp_path); accepted(tmp_path / 'batch-cpu')
    monkeypatch.setattr(continuation, 'fingerprint', lambda: [{'name': '/monitoring-api', 'id': 'new', 'image': 'new'}])
    with pytest.raises(AssertionError, match='API changed'): continuation.main(tmp_path, browser, 1)
    assert not (tmp_path / 'batch-cpu/batch-publication.json').exists()
    assert json.loads((tmp_path / 'continuation-progress.json').read_text())['state'] == 'stopped'
