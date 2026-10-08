"""Prepared full snapshots must stay valid until their own publication completes."""
import copy
import concurrent.futures
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import materialized_release as release
import admit_when_ready as admission


def environment(tmp_path, monkeypatch):
    projects = {'cpu': 'dcu-monitoring', 'dcu': 'dcu-monitoring', 'a3': 'a3-monitoring'}
    catalog = {'steps': [60], 'panels': []}
    resources = {'projects': [], 'dashboards': [], 'datasources': []}
    for project in sorted(set(projects.values())):
        resources['projects'].append({'kind': 'Project', 'metadata': {'name': project}, 'spec': {}})
        resources['datasources'].append({'kind': 'Datasource', 'metadata': {'name': 'victoriametrics', 'project': project},
            'spec': {'default': True, 'plugin': {'spec': {'proxy': {'spec': {'url': 'http://vm'}}}}}})
    for group, project in projects.items():
        expr = 'vector(1)'
        catalog['panels'].append({'id': group, 'group': group, 'project': project, 'dashboard': group, 'panel': 'target', 'expression': expr})
        resources['dashboards'].append({'kind': 'Dashboard', 'metadata': {'project': project, 'name': group},
            'spec': {'panels': {'target': {'spec': {'queries': [{'spec': {'plugin': {'spec': {
                'query': expr, 'datasource': {'name': 'victoriametrics'}}}}}]}},
                'other': {'spec': {'queries': []}}}}})
        (tmp_path / ('batch-' + group)).mkdir()
    catalog_path = tmp_path / 'catalog.json'; catalog_path.write_text(json.dumps(catalog))
    monkeypatch.setattr(release, 'CATALOG', catalog_path)
    monkeypatch.setattr(release, 'snapshot', lambda: copy.deepcopy(resources))
    monkeypatch.setattr(release, 'fingerprint', lambda: [])
    monkeypatch.setattr(release, 'readiness', lambda *a: {})
    writes = []
    def api(route, method='GET', data=None):
        kind = 'datasources' if '/datasources' in route else 'dashboards'
        path = release.source_path if kind == 'datasources' else release.path
        if method == 'POST':
            resources[kind].append(copy.deepcopy(data)); writes.append(route)
            return copy.deepcopy(data)
        current = next(d for d in resources[kind] if path(d) == route)
        if method == 'PUT':
            current.clear(); current.update(copy.deepcopy(data)); writes.append(route)
        return copy.deepcopy(current)
    monkeypatch.setattr(release, 'api', api)
    def published(before, state):
        after = copy.deepcopy(before); group = state['groups'][0]; project = projects[group]
        doc = next(d for d in after['dashboards'] if d['metadata']['name'] == group)
        doc['spec']['panels']['target']['spec']['queries'][0]['spec']['plugin']['spec']['datasource']['name'] = release.NAME
        if not any(d['metadata'] == {'name': release.NAME, 'project': project} for d in after['datasources']):
            after['datasources'].append({'kind': 'Datasource', 'metadata': {'name': release.NAME, 'project': project},
                'spec': {'default': False, 'plugin': {'spec': {'proxy': {'spec': {'url': 'http://127.0.0.1:18430/internal/perses'}}}}}})
        return after
    monkeypatch.setattr(release, 'published', published)
    monkeypatch.setattr(release, 'generator_plan', lambda *a: None)
    monkeypatch.setattr(release, 'generator_install', lambda *a: None)
    return resources, writes


def accepted(root, group):
    meta = json.loads((root / 'batch-meta.json').read_text())
    report = dict(meta, passed=True, samples=41, window_seconds=43200,
                  performance_policy=release.PERFORMANCE_POLICY,
                  benchmarks=[release.evaluate_benchmark({'pairs': [[1, .5]] * 41})])
    for name in ('batch-performance.json', 'non-target-performance.json', 'materialized-browser.json'):
        (root / name).write_text(json.dumps(report))
    (root / 'batch-admission.json').write_text(json.dumps({'group': group, 'passed': True}))


def test_each_batch_uses_snapshot_after_previous_publication_and_sources(tmp_path, monkeypatch):
    resources, writes = environment(tmp_path, monkeypatch)
    for index, group in enumerate(('cpu', 'dcu', 'a3')):
        root = tmp_path / ('batch-' + group)
        release.prepare(root, group); accepted(root, group)
        snapshot = release.normalized(resources); saved = copy.deepcopy(writes)
        if index < 2:
            next_group = ('dcu', 'a3')[index]
            with pytest.raises(AssertionError, match='Finish prepared batch'):
                release.prepare(tmp_path / ('batch-' + next_group), next_group)
            assert release.normalized(resources) == snapshot and writes == saved
        release.apply(root, group)
        assert not release.batch_terminal(root), 'Publication alone cannot unlock another batch'
        (root / 'batch-observation.json').write_text('{"passed":true}')
        assert release.batch_terminal(root) == 'observed'
    for doc in resources['dashboards']:
        assert doc['spec']['panels']['target']['spec']['queries'][0]['spec']['plugin']['spec']['datasource']['name'] == release.NAME
    assert sum(d['metadata']['name'] == release.NAME for d in resources['datasources']) == 2


@pytest.mark.parametrize('edit', ['new_source', 'source_edit', 'target_panel', 'other_panel'])
def test_real_resource_edit_still_blocks_apply_without_writes(tmp_path, monkeypatch, edit):
    resources, writes = environment(tmp_path, monkeypatch)
    root = tmp_path / 'batch-cpu'; release.prepare(root, 'cpu'); accepted(root, 'cpu')
    if edit == 'new_source':
        new = copy.deepcopy(resources['datasources'][0]); new['metadata']['name'] = 'new-source'
        resources['datasources'].append(new)
    elif edit == 'source_edit': resources['datasources'][0]['spec']['default'] = False
    else:
        key = 'target' if edit == 'target_panel' else 'other'
        resources['dashboards'][0]['spec']['panels'][key]['display'] = {'name': 'user edit'}
    before = copy.deepcopy(resources); calls = len(writes)
    with pytest.raises(AssertionError, match='Concurrent resource edit'): release.apply(root, 'cpu')
    assert resources == before and len(writes) == calls


def test_resume_stops_at_pending_batch_and_only_advances_after_observation(tmp_path, monkeypatch):
    resources, writes = environment(tmp_path, monkeypatch)
    (tmp_path / 'shadow-started.json').write_text('{"container_id":"api","image":"image"}')
    (tmp_path / 'shadow-observation.json').write_text('{"passed":true}')
    browser = tmp_path / 'browser.json'; browser.write_text('{}')
    monkeypatch.setattr(admission, 'fingerprint', lambda: [{'name': '/monitoring-api', 'id': 'api', 'image': 'image'}])
    monkeypatch.setattr(release, 'health', lambda: {'perses_acceleration': {'disabled_groups': [], 'state_error': None,
        'jobs': [{'job': g + ':v1:60', 'error': None} for g in ('cpu', 'dcu', 'a3')]}})
    monkeypatch.setattr(release, 'audit', accepted)
    monkeypatch.setattr(release, 'impact', lambda *a: None)
    admission.main(tmp_path, tmp_path, browser, 1)
    assert (tmp_path / 'batch-cpu/batch-before.json').exists()
    assert not (tmp_path / 'batch-dcu/batch-before.json').exists()
    admission.main(tmp_path, tmp_path, browser, 1)
    assert not (tmp_path / 'batch-dcu/batch-before.json').exists()
    root = tmp_path / 'batch-cpu'; release.apply(root, 'cpu')
    admission.main(tmp_path, tmp_path, browser, 1)
    assert not (tmp_path / 'batch-dcu/batch-before.json').exists()
    (root / 'batch-observation.json').write_text('{"passed":true}')
    admission.main(tmp_path, tmp_path, browser, 1)
    assert (tmp_path / 'batch-dcu/batch-before.json').exists()
    assert not (tmp_path / 'batch-a3/batch-before.json').exists()


def test_failed_admission_does_not_block_next_independent_batch(tmp_path, monkeypatch):
    environment(tmp_path, monkeypatch)
    cpu = tmp_path / 'batch-cpu'; release.prepare(cpu, 'cpu')
    (cpu / 'batch-admission.json').write_text('{"passed":false}')
    assert release.batch_terminal(cpu) == 'admission_failed'
    release.prepare(tmp_path / 'batch-dcu', 'dcu')


def test_simultaneous_prepares_cannot_both_capture_an_unsettled_snapshot(tmp_path, monkeypatch):
    resources, writes = environment(tmp_path, monkeypatch)
    entered, finish = threading.Event(), threading.Event()
    original = release._prepare
    def delayed(root, group):
        if group == 'cpu':
            entered.set()
            assert finish.wait(2)
        original(root, group)
    monkeypatch.setattr(release, '_prepare', delayed)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(release.prepare, tmp_path / 'batch-cpu', 'cpu')
        assert entered.wait(2)
        second = pool.submit(release.prepare, tmp_path / 'batch-a3', 'a3')
        finish.set(); first.result()
        with pytest.raises(AssertionError, match='Finish prepared batch'): second.result()
    assert not (tmp_path / 'batch-a3/batch-before.json').exists()
    assert not any(d['metadata']['project'] == 'a3-monitoring' and d['metadata']['name'] == release.NAME for d in resources['datasources'])


def test_old_failed_admission_cannot_unlock_incomplete_publication(tmp_path):
    (tmp_path / 'batch-admission.json').write_text('{"passed":false}')
    (tmp_path / 'batch-publication.json').write_text('{}')
    assert release.batch_terminal(tmp_path) is None
    (tmp_path / 'batch-observation.json').write_text('{"passed":false}')
    assert release.batch_terminal(tmp_path) is None


@pytest.mark.parametrize('fault', ['put_response', 'generator_install'])
@pytest.mark.parametrize('report_writable', [True, False])
def test_partial_publication_keeps_changes_and_blocks_next_batch(tmp_path, monkeypatch, fault, report_writable):
    import merge_release
    import continue_materialized
    resources, writes = environment(tmp_path, monkeypatch)
    root = tmp_path / 'batch-cpu'
    release.prepare(root, 'cpu'); accepted(root, 'cpu')
    # A prior failed admission must not unlock a later, partially applied release.
    (root / 'batch-admission.json').write_text('{"passed":false}')
    failure = OSError('injected ' + fault)
    original_api = release.api
    def api(route, method='GET', data=None):
        result = original_api(route, method, data)
        if method == 'PUT' and fault == 'put_response':
            raise failure
        return result
    monkeypatch.setattr(release, 'api', api)
    runtime = tmp_path / 'generator-state.json'
    def install(*args):
        runtime.write_text('{"groups":["cpu"]}')
        raise failure
    monkeypatch.setattr(release, 'generator_install', install)
    monkeypatch.setattr(release, 'rollback', lambda *a: pytest.fail('Rollback must never run'), raising=False)
    import admin
    monkeypatch.setattr(admin, 'update', lambda *a, **k: pytest.fail('Do not disable acceleration'))
    original_save = merge_release.save
    def save(root, name, data):
        if name == 'batch-apply-failure.json' and not report_writable:
            raise OSError('disk unavailable')
        original_save(root, name, data)
    import release_support
    monkeypatch.setattr(release_support, 'save', save)
    with pytest.raises(OSError) as caught:
        release.apply(root, 'cpu')
    assert caught.value is failure
    doc = next(d for d in resources['dashboards'] if d['metadata']['name'] == 'cpu')
    assert doc['spec']['panels']['target']['spec']['queries'][0]['spec']['plugin']['spec']['datasource']['name'] == release.NAME
    if fault == 'generator_install':
        assert json.loads(runtime.read_text())['groups'] == ['cpu']
    assert len(json.loads((root / 'batch-journal.json').read_text())) == 1
    assert not (root / 'batch-publication.json').exists()
    assert not (root / 'batch-rollback.json').exists()
    if report_writable:
        assert json.loads((root / 'batch-apply-failure.json').read_text())['automatic_rollback'] is False
    assert release.batch_terminal(root) is None
    assert continue_materialized.next_action(tmp_path)[0] == 'incomplete_publication'
    previous_writes = list(writes)
    with pytest.raises(AssertionError, match='Finish prepared batch'):
        release.prepare(tmp_path / 'batch-dcu', 'dcu')
    assert writes == previous_writes
