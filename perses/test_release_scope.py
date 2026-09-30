"""Query acceptance follows affected panels, including indirect dependencies."""
import copy
import json
from pathlib import Path

import pytest

from project_release import affected_panels
import project_release
from project_split import read_resources


@pytest.mark.parametrize('change', ['none', 'title', 'panel', 'variables', 'datasource'])
def test_audit_scope_includes_dependencies_without_unrelated_panels(change):
    before = read_resources(Path(__file__).parent / 'projects')
    candidate = copy.deepcopy(before)
    dashboard = candidate['dashboards'][0]
    project = dashboard['metadata']['project']
    pid = next(iter(dashboard['spec']['panels']))
    if change == 'title':
        dashboard['spec']['panels'][pid]['spec']['display']['name'] += ' updated'
    elif change == 'panel':
        dashboard['spec']['panels'][pid]['spec']['queries'][0]['spec']['plugin']['spec']['query'] += ' + 0'
    elif change == 'variables':
        dashboard['spec']['variables'][0]['spec']['display']['name'] += ' updated'
    elif change == 'datasource':
        datasource = next(d for d in candidate['datasources'] if d['metadata']['project'] == project)
        datasource['spec']['plugin']['spec']['proxy']['spec']['url'] += '/changed'
    selected = {(d['metadata']['project'], d['metadata']['name'], p) for d, p, _ in affected_panels(candidate, before)}
    if change in ('none', 'title'):
        expected = set()
    elif change == 'panel':
        expected = {(project, dashboard['metadata']['name'], pid)}
    else:
        dashboards = [dashboard] if change == 'variables' else [d for d in candidate['dashboards'] if d['metadata']['project'] == project]
        expected = {(project, d['metadata']['name'], p) for d in dashboards for p in d['spec']['panels']}
    assert selected == expected


def test_published_query_audit_uses_affected_panel_datasource(tmp_path, monkeypatch):
    before = read_resources(Path(__file__).parent / 'projects')
    candidate = copy.deepcopy(before)
    dashboard = candidate['dashboards'][0]
    panel = next(iter(dashboard['spec']['panels'].values()))
    panel['spec']['queries'] = panel['spec']['queries'][:1]
    query = panel['spec']['queries'][0]['spec']['plugin']['spec']
    query['query'] += ' + 0'
    query['datasource'] = {'name': 'perses-accelerated'}
    (tmp_path / 'before.json').write_text(json.dumps(before))
    requests = []
    def evaluate(base, expression, *args):
        requests.append((base, expression))
        return [{'metric': {}, 'values': [[100, '1']]}]
    monkeypatch.setattr(project_release, 'query', evaluate)
    result = project_release.audit(candidate, tmp_path, published=True)
    assert result['passed'] and result['queries'] == 2
    assert len(requests) == 4 and all(expression == query['query'] for _, expression in requests)
    assert {base for base, _ in requests} == {project_release.VM,
        project_release.BASE + '/proxy/projects/' + dashboard['metadata']['project'] + '/datasources/perses-accelerated'}
