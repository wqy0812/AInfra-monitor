"""Publication acceptance must exercise each panel's actual datasource."""
import copy
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def release(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'perses'))
    return importlib.import_module('project_release')


def resources():
    def datasource(project, name, default=False):
        return {'kind': 'Datasource', 'metadata': {'project': project, 'name': name},
                'spec': {'default': default, 'plugin': {'kind': 'PrometheusDatasource', 'spec': {}}}}
    def query(reference=None):
        spec = {'query': 'up{environment="a3-vllm"}'}
        if reference is not None:
            spec['datasource'] = {'kind': 'PrometheusDatasource', 'name': reference}
        return {'spec': {'plugin': {'spec': spec}}}
    return {'projects': [], 'datasources': [datasource('a3-monitoring', 'raw-custom', True),
            datasource('a3-monitoring', 'perses-accelerated'), datasource('dcu-monitoring', 'other-default', True)],
            'dashboards': [{'kind': 'Dashboard', 'metadata': {'project': 'a3-monitoring', 'name': 'hosts'},
                'spec': {'panels': {'cpu': {'spec': {'queries': [query('perses-accelerated'), query()]}}}}}]}


def test_audit_checks_each_explicit_and_project_default_datasource(release, tmp_path, monkeypatch):
    calls = []
    def evaluate(base, expression, start, end, step):
        calls.append((base, expression, step))
        return [{'metric': {'environment': 'a3-vllm'}, 'values': [[end, '1']]}]
    monkeypatch.setattr(release, 'query', evaluate)
    report = release.audit(resources(), tmp_path, published=True)
    assert report['passed'] and report['queries'] == 4
    proxy = release.BASE + '/proxy/projects/a3-monitoring/datasources/'
    assert sorted((base, step) for base, _, step in calls if base != release.VM) == sorted(
        (proxy + name, step) for name in ('perses-accelerated', 'raw-custom') for step in (15, 60))
    assert {check['datasource'] for check in report['checks']} == {'perses-accelerated', 'raw-custom'}


def test_accelerated_datasource_failure_fails_audit_and_is_saved(release, tmp_path, monkeypatch):
    def evaluate(base, expression, start, end, step):
        if base.endswith('/datasources/perses-accelerated'):
            raise OSError('accelerated datasource unavailable')
        return [{'metric': {}, 'values': [[end, '1']]}]
    monkeypatch.setattr(release, 'query', evaluate)
    report = release.audit(resources(), tmp_path, published=True)
    assert not report['passed']
    failures = [check for check in report['checks'] if 'error' in check]
    assert len(failures) == 2
    assert all(check['datasource'] == 'perses-accelerated' and 'unavailable' in check['error'] for check in failures)
    assert json.loads((tmp_path/'audit-after.json').read_text()) == report


@pytest.mark.parametrize('fault', ['missing', 'ambiguous'])
def test_unresolved_default_fails_audit_instead_of_checking_another_source(release, tmp_path, monkeypatch, fault):
    candidate = resources()
    default = candidate['datasources'][0]
    if fault == 'missing':
        default['spec']['default'] = False
    else:
        extra = copy.deepcopy(default)
        extra['metadata']['name'] = 'second-default'
        candidate['datasources'].append(extra)
    calls = []
    monkeypatch.setattr(release, 'query', lambda base, *args: calls.append(base) or [])
    report = release.audit(candidate, tmp_path, published=True)
    assert not report['passed']
    failures = [check for check in report['checks'] if 'error' in check]
    assert len(failures) == 2 and all('default Prometheus datasource' in check['error'] for check in failures)
    assert all(base == release.VM or base.endswith('/perses-accelerated') for base in calls)
