"""Failed writes must preserve applied monitoring changes for forward repair."""
import copy
import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_write_response_loss_keeps_candidate_and_journal(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'perses'))
    release = importlib.import_module('project_release')
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
    # This unit test isolates uncertain PUT handling from resource projection.
    monkeypatch.setattr(release, 'columns', lambda resources: resources)
    monkeypatch.setattr(release, 'validate', lambda *a: None)
    monkeypatch.setattr(release, 'http', api)
    monkeypatch.setattr(release, 'query', lambda *a: [1])
    (tmp_path / 'before.json').write_text(json.dumps(before))
    with pytest.raises(OSError) as caught:
        release.apply(after, tmp_path)
    assert caught.value is failure
    assert writes == [new] and current == new
    assert len(json.loads((tmp_path / 'journal.json').read_text())) == 1
    assert not (tmp_path / 'publication.json').exists()
