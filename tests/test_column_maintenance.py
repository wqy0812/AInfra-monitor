"""Maintenance keeps public writes and logical catalog/runtime identities distinct."""
import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path[:0] = [str(ROOT / 'perses'), str(ROOT / 'deploy/perses_acceleration')]
from dashboard_columns import apply as columns, panel_index, section_panels, source_change
from project_split import read_resources
from acceleration_catalog import build as catalog
from acceleration_publication import published
from query_slimming import BATCHES, prepare, replace_catalog
from query_slimming_fixtures import legacy_catalog, legacy_resources
import generator_transaction as transaction
import query_release as release


def test_catalog_identity_revision_filters_and_probes_survive_columns():
    source = read_resources(ROOT / 'perses/projects')
    public = columns(source)
    assert catalog(source) == catalog(public)
    from local_browser import browser_targets
    bindings = browser_targets(public)
    assert len(bindings) == 18
    assert {b['id'] for b in bindings} == {p['id'] for p in catalog(source)['panels']}
    assert sum(b['dashboard'] == 'model-monitoring' for b in bindings) == 2
    for binding in bindings:
        document = next(d for d in public['dashboards'] if
                        (d['metadata']['project'], d['metadata']['name']) == (binding['project'], binding['dashboard']))
        assert binding['panel'] in document['spec']['panels']
    for project in ('a3-monitoring', 'dcu-monitoring', 'xpu-monitoring'):
        for section in ('backend-performance', 'accelerator-resources', 'monitoring-health'):
            original = [d['spec']['panels'][k] for d, k in section_panels(source, project, section)]
            actual = [d['spec']['panels'][k] for d, k in section_panels(public, project, section)]
            assert actual == original
    mixed = copy.deepcopy(public)
    mixed['dashboards'].append(next(d for d in source['dashboards'] if d['metadata']['name'] == 'backend-performance'))
    with pytest.raises(ValueError, match='Ambiguous'):
        panel_index(mixed)


@pytest.mark.parametrize('batch', BATCHES)
def test_rewrite_candidate_changes_only_public_target_queries(batch):
    source = legacy_resources()
    expected, source_changes = prepare(source, batch)
    public = columns(source)
    candidate, changes = prepare(public, batch)
    assert candidate == columns(expected)
    keyed = lambda rows: {tuple(source_change(c)[k] for k in ('project', 'dashboard', 'panel')):
                          source_change(c) for c in rows}
    assert keyed(changes) == keyed(source_changes)
    assert len(changes) == len(source_changes)
    assert prepare(candidate, batch) == (candidate, [])
    for change in changes:
        doc = next(d for d in candidate['dashboards'] if
                   (d['metadata']['project'], d['metadata']['name']) == (change['project'], change['dashboard']))
        assert doc['spec']['panels'][change['panel']] == change['after']


def test_revision_catalog_keeps_existing_job_ids_and_revisions_outside_a3():
    source = legacy_resources()
    previous = legacy_catalog()
    expected, source_candidate, _ = replace_catalog(previous, source)
    actual, candidate, changes = replace_catalog(previous, columns(source))
    assert actual == expected and candidate == columns(source_candidate)
    assert len(changes) == 8
    assert sum(c['dashboard'] == 'model-monitoring' for c in changes) == 2


def test_merge_and_datasource_candidates_keep_public_resources():
    from query_acceleration import prepare as merge
    from test_query_acceleration import percentile_panel
    source = legacy_resources()
    doc = next(d for d in source['dashboards'] if
               (d['metadata']['project'], d['metadata']['name']) == ('dcu-monitoring', 'backend-performance'))
    doc['spec']['panels']['core-ttft'] = percentile_panel()
    expected, original = merge(source)
    actual, changes = merge(columns(source))
    assert original and len(changes) == len(original)
    assert actual == columns(expected)
    state = json.loads((ROOT / 'perses/acceleration_state.json').read_text())
    assert published(columns(source), state) == columns(published(source, state))


@pytest.mark.parametrize('fault', [None, 'concurrent', 'lost_response'])
def test_public_write_source_install_and_failure_evidence(tmp_path, monkeypatch, fault):
    source = legacy_resources()
    before = columns(source)
    after, changes = prepare(before, 'generation-results')
    runtime = tmp_path / 'runtime'
    for document in source['dashboards']:
        target = runtime / 'projects' / document['metadata']['project'] / 'dashboards' / (document['metadata']['name'] + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document))
    for name in transaction.MODULES:
        shutil.copyfile(ROOT / 'perses' / name, runtime / name)
    state = {'schema': 1, 'groups': ['a3'], 'merges': []}
    (runtime / 'acceleration_state.json').write_text(json.dumps(state))
    evidence = tmp_path / 'evidence'; evidence.mkdir()
    monkeypatch.setattr(transaction, 'RUNTIME', runtime)
    release.save(evidence, 'query-meta.json', {'tool_sha256': release.tool_fingerprint()})
    release.save(evidence, 'query-runtime-before.json', release.runtime_snapshot(changes))
    monkeypatch.setattr(release, 'validate_admission', lambda *a: copy.deepcopy((before, after, changes)))
    server = copy.deepcopy(before)
    monkeypatch.setattr(release, 'snapshot', lambda: copy.deepcopy(server))
    writes = []

    def api(route, method='GET', data=None):
        doc = next(d for d in server['dashboards'] if release.path(d) == route)
        if method == 'GET' and fault == 'concurrent':
            # An unrelated panel sharing the merged dashboard must not be overwritten.
            doc['spec']['panels']['gateway-generation--live-stages']['spec']['display']['name'] += ' edit'
        if method == 'PUT':
            assert route.endswith('/gateway-monitoring')
            writes.append(route)
            doc.clear(); doc.update(copy.deepcopy(data))
            if fault == 'lost_response':
                raise OSError('PUT response lost')
        return copy.deepcopy(doc)

    monkeypatch.setattr(release, 'api', api)
    if fault:
        with pytest.raises(AssertionError if fault == 'concurrent' else OSError):
            release.apply(evidence, 'generation-results')
        assert len(writes) == (0 if fault == 'concurrent' else 1)
        assert not (evidence / 'query-publication.json').exists()
        journal = release.load(evidence, 'query-journal.json')
        assert len(journal) == len(writes)
        assert release.load(runtime, 'acceleration_state.json') == state
        return
    release.apply(evidence, 'generation-results')
    assert len(writes) == 3 and server == after
    installed = release.load(runtime, 'acceleration_state.json')
    assert installed['rewrites'] == sorted([list(source_change(c)[k] for k in
        ('project', 'dashboard', 'panel')) + [c['kind']] for c in changes])
    for change in changes:
        logical = source_change(change)
        path = 'projects/{project}/dashboards/{dashboard}.json'.format(**logical)
        assert release.load(runtime, path)['spec']['panels'][logical['panel']] == change['after']
    assert not list((runtime / 'projects').glob('*/dashboards/gateway-monitoring.json'))


def test_full_project_uncertain_write_reaches_fault_journal(tmp_path, monkeypatch):
    import project_release
    before = columns(read_resources(ROOT / 'perses/projects'))
    candidate = copy.deepcopy(before)
    target = next(d for d in candidate['dashboards'] if d['metadata']['name'] == 'model-monitoring')
    target['spec']['display']['description'] += ' reviewed'
    server = project_release.flattened(before)
    release.save(tmp_path, 'before.json', before)
    monkeypatch.setattr(project_release, 'snapshot', lambda: copy.deepcopy(before))

    def http(url, method='GET', data=None):
        identity = next(k for k, d in server.items() if project_release.endpoint(d['kind'], d) + '/' + k[-1] == url)
        if method == 'PUT':
            server[identity] = copy.deepcopy(data)
            raise OSError('PUT response lost')
        return copy.deepcopy(server[identity])

    monkeypatch.setattr(project_release, 'http', http)
    with pytest.raises(OSError, match='PUT response lost'):
        project_release.apply(candidate, tmp_path)
    assert len(release.load(tmp_path, 'journal.json')) == 1
    assert server[project_release.key(target)]['spec'] == target['spec']
    assert not (tmp_path / 'publication.json').exists()
