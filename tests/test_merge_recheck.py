import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import merge_recheck as runner


def environment(tmp_path, monkeypatch, passed=True):
    root = tmp_path / 'merge-recheck'; root.mkdir()
    cpu = tmp_path / 'batch-cpu'; cpu.mkdir()
    (cpu / 'batch-observation.json').write_text('{"passed":true}')
    resources = {'dashboards': [], 'datasources': [], 'projects': []}
    for name in ('merge-before.json', 'merge-candidate.json'):
        (root / name).write_text(json.dumps(resources))
    (root / 'merge-services.json').write_text('[]')
    for name in ('merge-browser.json', 'merge-synthetic.json', 'browser-cohorts.json'):
        (root / name).write_text(json.dumps({'passed': True, 'candidate_sha256': runner.release.sha(resources)}))
    monkeypatch.setattr(runner.release, 'snapshot', lambda: resources)
    monkeypatch.setattr(runner.release, 'fingerprint', lambda: [])
    calls = []
    def audit(evidence, browser, windows, target_timing):
        assert windows == (1, 12) and target_timing
        calls.append('audit')
        (root / 'viewport-performance.json').write_text(json.dumps({'benchmarks': [
            {'project': 'p', 'dashboard': 'd', 'passed': passed} for _ in range(4)]}))
    def apply(evidence, admitted_only=False, viewport=False):
        assert admitted_only and viewport and evidence == root
        calls.append('apply')
    monkeypatch.setattr(runner.viewport_audit, 'main', audit)
    monkeypatch.setattr(runner.release, 'apply', apply)
    return root, calls


def test_recheck_benchmarks_now_and_publishes_only_after_cpu_observation(tmp_path, monkeypatch):
    root, calls = environment(tmp_path, monkeypatch)
    (tmp_path / 'batch-cpu/batch-observation.json').write_text('{"passed":false}')
    def sleep(_):
        assert calls == ['audit']
        (tmp_path / 'batch-cpu/batch-observation.json').write_text('{"passed":true}')
    monkeypatch.setattr(runner.time, 'sleep', sleep)
    runner.main(root, tmp_path, 1)
    assert calls == ['audit', 'apply']
    assert json.loads((root / 'recheck-progress.json').read_text())['published']


def test_failed_performance_does_not_publish(tmp_path, monkeypatch):
    root, calls = environment(tmp_path, monkeypatch, passed=False)
    runner.main(root, tmp_path, 1)
    assert calls == ['audit']
    assert json.loads((root / 'recheck-progress.json').read_text())['state'] == 'no_admitted_dashboards'


@pytest.mark.parametrize('failure', ['browser', 'resources', 'cpu_rollback'])
def test_unverified_or_stale_evidence_cannot_publish(tmp_path, monkeypatch, failure):
    root, calls = environment(tmp_path, monkeypatch)
    if failure == 'browser': (root / 'merge-browser.json').write_text('{"passed":false}')
    elif failure == 'resources': monkeypatch.setattr(runner.release, 'snapshot', lambda: {'dashboards': [{'kind': 'Dashboard', 'metadata': {'name': 'edit'}, 'spec': {}}]})
    else: (tmp_path / 'batch-cpu/batch-rollback.json').write_text('{}')
    with pytest.raises(AssertionError): runner.main(root, tmp_path, 1)
    assert 'apply' not in calls
    assert json.loads((root / 'recheck-progress.json').read_text())['state'] == 'stopped'
