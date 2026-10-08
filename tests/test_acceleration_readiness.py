"""Startup checks consume generic publication evidence without mutating services."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/perses_acceleration'))
import api_readiness as readiness


@pytest.fixture
def startup(tmp_path, monkeypatch):
    publication = {'container': 'monitoring-api', 'container_id': 'new',
                   'image': 'locked-image', 'acceptance': 'passed'}
    (tmp_path / 'container-publication.json').write_text(json.dumps(publication))
    clock = [100.0]
    monkeypatch.setattr(readiness.time, 'time', lambda: clock[0])
    monkeypatch.setattr(readiness.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(readiness.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(readiness, 'inspect', lambda _: {'Id': 'new', 'Image': 'locked-image'})
    state = {'status': 'ok', 'environments': {
        env: {'error': None, 'processed_at': 99} for env in ('dcu-pd', 'a3-vllm', 'xpu-pd')},
        'perses_acceleration': {'enabled': True, 'state_error': None, 'disabled_groups': [],
            'backfill_priority_active': False, 'parallel_a3_backfill': False,
            'jobs': [{'job': 'panel:revision:5', 'error': None, 'lag_seconds': 5}]}}
    monkeypatch.setattr(readiness, 'read_health', lambda: copy.deepcopy(state))
    return tmp_path, state, clock


def test_generic_container_release_feeds_startup_acceptance(startup):
    root, state, clock = startup
    readiness.observe(root)
    assert readiness.accepted(root)
    result = json.loads((root / 'api-readiness.json').read_text())
    assert result['image'] == 'locked-image' and result['container_id'] == 'new'
    assert clock[0] == 100 and not (root / 'shadow-started.json').exists()


@pytest.mark.parametrize('fault', ['disabled', 'lag', 'scheduling', 'transport'])
def test_startup_failure_preserves_publication_and_has_no_state_writes(startup, monkeypatch, fault):
    root, state, clock = startup
    before = (root / 'container-publication.json').read_bytes()
    accelerator = state['perses_acceleration']
    if fault == 'disabled': accelerator['disabled_groups'] = ['cpu']
    if fault == 'lag': accelerator['jobs'][0]['lag_seconds'] = 601
    if fault == 'scheduling': accelerator['parallel_a3_backfill'] = True
    if fault == 'transport':
        def fail(): raise OSError('connection lost')
        monkeypatch.setattr(readiness, 'read_health', fail)
    with pytest.raises(RuntimeError, match='Sustained'):
        readiness.observe(root)
    assert (root / 'container-publication.json').read_bytes() == before
    assert readiness.accepted(root) is False
    assert clock[0] == 110
    report = json.loads((root / 'api-readiness-failure.json').read_text())
    assert report['recovery'] == 'fix_forward' and report['automatic_rollback'] is False
    assert {p.name for p in root.iterdir()} == {
        'container-publication.json', 'api-readiness.json', 'api-readiness-failure.json'}


def test_changed_container_stops_before_health_queries(startup, monkeypatch):
    root, _, _ = startup
    monkeypatch.setattr(readiness, 'inspect', lambda _: {'Id': 'someone-elses-container', 'Image': 'locked-image'})
    monkeypatch.setattr(readiness, 'read_health', lambda: pytest.fail('Stale acceptance must stop'))
    with pytest.raises(AssertionError, match='API changed'):
        readiness.observe(root)
    assert readiness.accepted(root) is False


def test_prior_evidence_is_read_only_and_does_not_override_new_result(tmp_path):
    (tmp_path / 'shadow-started.json').write_text('{"container_id":"old", "image":"image"}')
    (tmp_path / 'shadow-observation.json').write_text('{"passed":true}')
    assert readiness.publication(tmp_path)['container_id'] == 'old'
    assert readiness.accepted(tmp_path)
    (tmp_path / 'api-readiness.json').write_text('{"passed":false}')
    assert not readiness.accepted(tmp_path)
