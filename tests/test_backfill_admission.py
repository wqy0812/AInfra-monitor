import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import admit_when_ready as admission


def test_waits_for_verified_coverage_and_stability_and_keeps_failed_groups_unpublished(tmp_path, monkeypatch):
    clock = [1000.0]; calls = []; ready = [False]
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'panels': [{'id': g, 'group': g} for g in ('cpu', 'dcu', 'a3')]}))
    (tmp_path / 'shadow-started.json').write_text(json.dumps({'container_id': 'candidate', 'image': 'new'}))
    (tmp_path / 'shadow-observation.json').write_text(json.dumps({'passed': False}))
    browser = tmp_path / 'browser.json'; browser.write_text('{}')
    for group in ('cpu', 'dcu', 'a3'): (tmp_path / ('batch-' + group)).mkdir()
    monkeypatch.setattr(admission.release, 'CATALOG', catalog)
    monkeypatch.setattr(admission.time, 'time', lambda: clock[0])
    def sleep(seconds):
        clock[0] += seconds
        if ready[0]: (tmp_path / 'shadow-observation.json').write_text(json.dumps({'passed': True}))
        ready[0] = True
    monkeypatch.setattr(admission.time, 'sleep', sleep)
    monkeypatch.setattr(admission, 'fingerprint', lambda: [{'name': '/monitoring-api', 'id': 'candidate', 'image': 'new'}])
    monkeypatch.setattr(admission.release, 'health', lambda: {'perses_acceleration': {
        'disabled_groups': [], 'state_error': None, 'jobs': [{'job': g + ':v1:5', 'error': None} for g in ('cpu', 'dcu', 'a3')]}})
    def readiness(*args): assert ready[0], 'Need 12h verified coverage'
    monkeypatch.setattr(admission.release, 'readiness', readiness)
    def prepare(root, group):
        assert ready[0] and json.loads((tmp_path / 'shadow-observation.json').read_text())['passed']
        calls.append(('prepare', group))
    def audit(root, group):
        calls.append(('audit', group)); (root / 'batch-performance.json').write_text(json.dumps({'passed': group != 'cpu'}))
    def impact(root, group):
        calls.append(('impact', group)); (root / 'non-target-performance.json').write_text(json.dumps({'passed': True}))
    monkeypatch.setattr(admission.release, 'prepare', prepare)
    monkeypatch.setattr(admission.release, 'audit', audit)
    monkeypatch.setattr(admission.release, 'impact', impact)
    monkeypatch.setattr(admission.release, 'apply', lambda *args: (_ for _ in ()).throw(AssertionError('Must not publish')))
    admission.main(tmp_path, tmp_path, browser, 1)
    result = json.loads((tmp_path / 'admission-progress.json').read_text())
    assert clock[0] == 1060 and result['published_panels'] == 0
    assert result['groups']['cpu']['state'] == 'admission_failed'
    assert result['groups']['dcu']['state'] == 'ready_for_publication'
    assert 'a3' not in result['groups']
    assert not any(g == 'a3' for _, g in calls), 'A later datasource must not invalidate the ready DCU snapshot'
    assert ('impact', 'cpu') not in calls


def test_cannot_reuse_observation_from_another_image(tmp_path, monkeypatch):
    catalog = tmp_path / 'catalog.json'; catalog.write_text('{"panels": []}')
    (tmp_path / 'shadow-started.json').write_text('{"image":"new","container_id":"candidate"}')
    prior = tmp_path / 'prior'; prior.mkdir()
    (prior / 'shadow-started.json').write_text('{"image":"old"}')
    monkeypatch.setattr(admission.release, 'CATALOG', catalog)
    with pytest.raises(AssertionError, match='another API image'):
        admission.main(tmp_path, tmp_path, tmp_path / 'browser.json', 1, prior)
    assert not (tmp_path / 'admission-progress.json').exists()
