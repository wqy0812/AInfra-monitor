"""Failed writes must preserve applied monitoring changes for forward repair."""
import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('module_name', ['project_release', 'merge_release'])
def test_dashboard_write_response_loss_keeps_candidate_and_journal(tmp_path, monkeypatch, module_name):
    monkeypatch.syspath_prepend(str(ROOT / 'perses'))
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/perses_acceleration'))
    release = importlib.import_module(module_name)
    old = {'kind': 'Dashboard', 'metadata': {'project': 'dcu-monitoring', 'name': 'host'},
           'spec': {'panels': {'target': {'spec': {'queries': []}}}}}
    new = copy.deepcopy(old); new['spec']['panels']['target']['spec']['display'] = {'name': 'new'}
    before = {'projects': [], 'datasources': [], 'dashboards': [old]}
    after = {**before, 'dashboards': [new]}
    current = copy.deepcopy(old)
    monkeypatch.setattr(release, 'snapshot', lambda: copy.deepcopy(before))
    monkeypatch.setattr(release, 'fingerprint', lambda: [])
    monkeypatch.setattr(release, 'rollback', lambda *a: pytest.fail('Rollback must never run'), raising=False)
    writes = []
    failure = OSError('PUT response lost')
    def api(route, method='GET', data=None):
        if method == 'PUT':
            writes.append(copy.deepcopy(data))
            current.clear(); current.update(copy.deepcopy(data))
            raise failure
        return copy.deepcopy(current)
    if module_name == 'project_release':
        # This unit test isolates uncertain PUT handling from resource projection.
        monkeypatch.setattr(release, 'columns', lambda resources: resources)
        monkeypatch.setattr(release, 'validate', lambda *a: None)
        reports = {'before.json': before}
        monkeypatch.setattr(release, 'http', api)
        monkeypatch.setattr(release, 'query', lambda *a: [1])
        action = lambda: release.apply(after, tmp_path)
        journal_name, publication_name = 'journal.json', 'publication.json'
    else:
        import generator_transaction
        monkeypatch.setattr(generator_transaction, 'plan', lambda *a: None)
        monkeypatch.setattr(release, 'api', api)
        proof = {'passed': True, 'candidate_sha256': release.sha(after)}
        reports = {'merge-before.json': before, 'merge-candidate.json': after,
                   'merge-services.json': [], 'merge-browser.json': proof,
                   'merge-synthetic.json': proof, 'non-target-performance.json': proof,
                   'merge-changes.json': [{'project': 'dcu-monitoring', 'dashboard': 'host', 'panel': 'target',
                                           'before': old['spec']['panels']['target'], 'after': new['spec']['panels']['target']}],
                   'merge-performance.json': {**proof, 'samples': 41, 'resources_unchanged': True,
                       'services_unchanged': True, 'benchmarks': [
                           {'project': 'dcu-monitoring', 'dashboard': 'host', 'passed': True} for _ in range(4)]}}
        action = lambda: release.apply(tmp_path)
        journal_name, publication_name = 'merge-journal.json', 'merge-publication.json'
    for name, value in reports.items():
        (tmp_path / name).write_text(json.dumps(value))
    with pytest.raises(OSError) as caught:
        action()
    assert caught.value is failure
    assert writes == [new] and current == new
    assert len(json.loads((tmp_path / journal_name).read_text())) == 1
    assert not (tmp_path / publication_name).exists()
