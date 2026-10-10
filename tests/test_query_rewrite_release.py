import copy
import json
import shutil
import sys
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'deploy/perses_acceleration'))
sys.path.insert(0, str(ROOT / 'perses'))
import query_release as release
import generator_transaction as transaction
import materialized_release as materialized
from query_slimming import prepare, replace_catalog, validate_revision_catalog
from query_slimming_fixtures import legacy_catalog, legacy_resources


def test_timing_gate_uses_all_finite_pairs_and_strict_median():
    pairs = [[{'target': 2.0, 'probe': 1.0}, {'target': 1.0, 'probe': 1.0}] for _ in range(41)]
    assert release.evaluate(pairs, target=True)['passed']
    pairs[0][1]['probe'] = float('nan')
    with pytest.raises(AssertionError): release.evaluate(pairs, target=True)
    for after, probe, passed in [(2.0, 1.0, False), (1.0, 1.06, False), (1.0, 1.05, True)]:
        rows = [[{'target': 2.0, 'probe': 1.0}, {'target': after, 'probe': probe}] for _ in range(41)]
        assert release.evaluate(rows, target=True)['passed'] == passed
    with pytest.raises(AssertionError): release.evaluate(rows[:-1], target=True)


def test_predecessor_batches_must_be_published(tmp_path, monkeypatch):
    monkeypatch.setattr(transaction, 'RUNTIME', tmp_path)
    release.save(tmp_path, 'acceleration_state.json', {'rewrites': []})
    release.require_predecessors('generation-results')
    with pytest.raises(AssertionError, match='prior batches'):
        release.require_predecessors('gateway-latency')
    entries = [list(t) + ['generation-results'] for t in release.batch_targets('generation-results')]
    release.save(tmp_path, 'acceleration_state.json', {'rewrites': entries})
    release.require_predecessors('gateway-latency')
    with pytest.raises(AssertionError): release.require_predecessors('a3-histograms')


def test_proof_rejects_duplicate_missing_and_mismatched_panels():
    changes = [{'project': 'p', 'dashboard': 'd', 'panel': 'k', 'kind': 'generation-results'}]
    checks = [dict(project='p', dashboard='d', panel='k', case=case, step=step, passed=True)
              for case in ('normal', 'zero', 'absent-result', 'gap-result') for step in (5, 15, 60)]
    release.validate_proof({'checks': checks}, changes, browser=False)
    for broken in (checks[:-1], checks + checks[:1], [dict(c, dashboard='other') for c in checks]):
        with pytest.raises(AssertionError): release.validate_proof({'checks': broken}, changes, browser=False)
    browser = dict(viewport={'width': 1920, 'height': 1080}, errors=[], requests=[{'status': 200}],
                   checks=[dict(project='p', dashboard='d', testCase=case, before={'k': []}, after={'k': []}, passed=True)
                           for case in ('normal', 'zero', 'absent-result', 'gap-result')])
    release.validate_proof(browser, changes, browser=True)
    browser['checks'][0]['after']['k'] = ['different colour']
    with pytest.raises(AssertionError): release.validate_proof(browser, changes, browser=True)


def test_generation_comparison_reads_both_candidate_queries(monkeypatch):
    resources = legacy_resources()
    _, changes = prepare(resources, 'generation-results')
    change = changes[0]
    calls = []
    def query(expression, *args):
        calls.append(expression)
        if 'label_del' in expression:
            return [{'metric': {'environment': 'env', 'perses_series': name, 'perses_order': '%02d' % i}, 'values': [[1, str(i)]]}
                    for i, name in enumerate(('客户端取消', '客户端断开', '未知结果'), 1)]
        return [{'metric': {'environment': 'env'}, 'values': [[1, '6']]}]
    monkeypatch.setattr(release, 'query', query)
    rows = release.rows_for(change['after'], change, 1, 1, 5)
    assert len(calls) == 2 and len(rows) == 4
    assert {r['metric']['perses_series'] for r in rows} == {'全部结束', '客户端取消', '客户端断开', '未知结果'}


@pytest.fixture(params=[False, True], ids=['source', 'columns'])
def revision(tmp_path, monkeypatch, request):
    resources = legacy_resources()
    if request.param:
        from dashboard_columns import apply
        resources = apply(resources)
    previous = legacy_catalog()
    candidate, after, changes = replace_catalog(previous, resources)
    root = tmp_path / 'batch'; root.mkdir()
    old = tmp_path / 'old.json'; old.write_text(json.dumps(previous))
    new = tmp_path / 'new.json'; new.write_text(json.dumps(candidate))
    monkeypatch.setattr(materialized, 'CATALOG', new)
    monkeypatch.setattr(materialized, 'snapshot', lambda: copy.deepcopy(resources))
    monkeypatch.setattr(materialized, 'fingerprint', lambda: ['frozen'])
    monkeypatch.setattr(materialized, 'api', lambda *a: pytest.fail('No datasource write when replacing an active group'))
    monkeypatch.setattr(transaction, 'RUNTIME', tmp_path / 'runtime')
    return root, old, previous, candidate, resources, after, changes


def test_revision_prepare_handles_already_switched_and_preserves_others(revision):
    root, old, previous, candidate, before, after, changes = revision
    materialized.prepare(root, 'a3', old)
    meta = release.load(root, 'batch-meta.json')
    assert meta['replace_revision']
    assert release.load(root, 'batch-candidate.json') == after
    assert len(release.load(root, 'batch-changes.json')) == 8
    assert sum(a != b for a, b in zip(previous['panels'], candidate['panels'])) == 8
    assert 'rewrites' not in release.load(root, 'batch-before.json')
    assert (root / 'batch-previous-catalog.json').read_bytes() == old.read_bytes()


def test_revision_prepare_does_not_rebase_prior_runtime_evidence(revision):
    root, old, *_ = revision
    release.save(root, 'query-runtime-before.json', {'query_slimming.py': 'previous-bytes'})
    original = (root / 'query-runtime-before.json').read_bytes()
    with pytest.raises(AssertionError, match='Runtime changed'):
        materialized.prepare(root, 'a3', old)
    assert (root / 'query-runtime-before.json').read_bytes() == original


def test_revision_apply_writes_actual_dashboard_and_installs_source_queries(revision, monkeypatch):
    from test_materialized_sequence import accepted
    from dashboard_columns import source_change
    root, old, _, _, before, after, changes = revision
    runtime = transaction.RUNTIME
    for document in legacy_resources()['dashboards']:
        target = runtime / 'projects' / document['metadata']['project'] / 'dashboards' / (document['metadata']['name'] + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document))
    for name in transaction.MODULES:
        shutil.copyfile(ROOT / 'perses' / name, runtime / name)
    release.save(runtime, 'acceleration_state.json', {'schema': 1, 'groups': ['a3'], 'merges': []})
    materialized.prepare(root, 'a3', old)
    accepted(root, 'a3')
    release.save(root, 'query-meta.json', {'tool_sha256': release.tool_fingerprint()})
    monkeypatch.setattr(release, 'validate_admission', lambda *a, **kw: copy.deepcopy((before, after, changes)))
    monkeypatch.setattr(materialized, 'readiness', lambda *a: {})
    server = copy.deepcopy(before)
    monkeypatch.setattr(materialized, 'snapshot', lambda: copy.deepcopy(server))
    writes = []
    def api(route, method='GET', data=None):
        document = next(d for d in server['dashboards'] if materialized.path(d) == route)
        if method == 'PUT':
            writes.append(route)
            document.clear(); document.update(copy.deepcopy(data))
        return copy.deepcopy(document)
    monkeypatch.setattr(materialized, 'api', api)
    materialized.apply(root, 'a3')
    assert server == after and len(writes) == 3
    for change in changes:
        c = source_change(change)
        relative = 'projects/{project}/dashboards/{dashboard}.json'.format(**c)
        assert release.load(runtime, relative)['spec']['panels'][c['panel']]['spec']['queries'] == c['after']['spec']['queries']
    assert not list((runtime / 'projects').glob('*/dashboards/model-monitoring.json'))


def test_catalog_cli_replaces_only_a3_and_rejects_step_change(revision, tmp_path):
    _, old, _, expected, before, *_ = revision
    snapshot = tmp_path / 'snapshot.json'; snapshot.write_text(json.dumps(before))
    out = tmp_path / 'candidate.json'
    args = [sys.executable, str(ROOT / 'perses/acceleration_catalog.py'), str(snapshot), str(out), '--previous-catalog', str(old)]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text()) == expected
    original = out.read_bytes()
    result = subprocess.run(args + ['--steps', '5'], capture_output=True, text=True)
    assert result.returncode != 0 and out.read_bytes() == original


@pytest.mark.parametrize('fault', ['unrelated', 'steps', 'same-revision', 'expression', 'digest', 'membership'])
def test_revision_rejects_unrelated_or_unproven_changes(revision, fault):
    _, _, previous, candidate, _, _, _ = revision
    changed = next(p for p in candidate['panels'] if p['group'] == 'a3')
    if fault == 'unrelated': candidate['panels'][0]['title'] = 'unrelated'
    elif fault == 'steps': candidate['steps'] = [5]
    elif fault == 'same-revision': candidate = previous
    elif fault == 'expression': changed['expression'] += ' * 2'
    elif fault == 'digest': changed['revision'] = 'wrong'
    else: candidate['panels'].pop()
    with pytest.raises(ValueError): validate_revision_catalog(previous, candidate, 'a3')


@pytest.fixture
def candidate_source(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    for relative in release.tool_fingerprint():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    monkeypatch.setattr(release, '__file__', str(source / 'deploy/perses_acceleration/query_release.py'))
    monkeypatch.setattr(transaction, '__file__', str(source / 'deploy/perses_acceleration/generator_transaction.py'))
    return source


@pytest.fixture
def rewrite_transaction(tmp_path, monkeypatch, candidate_source):
    runtime = tmp_path / 'runtime'; runtime.mkdir()
    evidence = tmp_path / 'evidence'; evidence.mkdir()
    monkeypatch.setattr(transaction, 'RUNTIME', runtime)
    resources = legacy_resources()
    _, changes = prepare(resources, 'generation-results')
    changes = changes[:1]
    c = changes[0]
    relative = 'projects/{project}/dashboards/{dashboard}.json'.format(**c)
    document = next(d for d in resources['dashboards'] if (d['metadata']['project'], d['metadata']['name']) == (c['project'], c['dashboard']))
    target = runtime / relative; target.parent.mkdir(parents=True)
    target.write_text(json.dumps(document))
    state = {'schema': 1, 'merges': [], 'groups': ['a3']}
    (runtime / 'acceleration_state.json').write_text(json.dumps(state))
    # A previous generator version is allowed only when its exact bytes were frozen.
    (runtime / 'query_acceleration.py').write_text('# existing version\n')
    release.save(evidence, 'query-runtime-before.json', release.runtime_snapshot(changes))
    release.save(evidence, 'query-meta.json', {'tool_sha256': release.tool_fingerprint()})
    entry = [[c['project'], c['dashboard'], c['panel'], c['kind']]]
    return runtime, evidence, changes, entry


def test_generator_rewrite_installs_against_frozen_bytes_and_keeps_group_state(rewrite_transaction):
    runtime, evidence, changes, entry = rewrite_transaction
    c = changes[0]
    relative = 'projects/{project}/dashboards/{dashboard}.json'.format(**c)
    transaction.plan(evidence, changes, rewrites=entry)
    transaction.install(evidence)
    actual = release.load(runtime, 'acceleration_state.json')
    assert actual == {'schema': 1, 'merges': [], 'groups': ['a3'], 'rewrites': entry}
    assert release.load(runtime, relative)['spec']['panels'][c['panel']] == c['after']
    assert (runtime / 'query_slimming.py').is_file()


@pytest.mark.parametrize('relative', transaction.source_paths())
@pytest.mark.parametrize('action', ['resume', 'apply'])
def test_rewrite_rejects_changed_install_source_before_network(tmp_path, monkeypatch, candidate_source, relative, action):
    before = legacy_resources()
    after, changes = prepare(before, 'generation-results')
    evidence = tmp_path / 'audit'; evidence.mkdir()
    meta = {'batch': 'generation-results', 'candidate_sha256': release.sha(after),
            'tool_sha256': release.tool_fingerprint()}
    for name, value in [('before', before), ('candidate', after), ('changes', changes),
                        ('meta', meta), ('performance', meta)]:
        release.save(evidence, 'query-' + name + '.json', value)
    source = candidate_source / relative
    source.write_bytes(source.read_bytes() + b'\n# Concurrent candidate edit\n')
    monkeypatch.setattr(release, 'snapshot', lambda: pytest.fail('Must reject changed source before network'))
    monkeypatch.setattr(release, 'require_predecessors', lambda *_: pytest.fail('Must reject changed source at admission'))
    with pytest.raises(AssertionError, match='Audit implementation changed'):
        if action == 'resume':
            release.audit(evidence, 'generation-results', resume=True)
        else:
            release.apply(evidence, 'generation-results')
    assert not (evidence / 'query-journal.json').exists()
    assert not (evidence / 'generator-journal.json').exists()


@pytest.mark.parametrize('relative', transaction.source_paths())
def test_plan_rechecks_source_bytes_after_admission(rewrite_transaction, candidate_source, relative):
    runtime, evidence, changes, entry = rewrite_transaction
    before = release.runtime_snapshot(changes)
    source = candidate_source / relative
    source.write_bytes(source.read_bytes() + b'\n# Edit between admission and planning\n')
    with pytest.raises(AssertionError, match='Candidate source changed since audit'):
        transaction.plan(evidence, changes, rewrites=entry)
    assert release.runtime_snapshot(changes) == before
    assert not (evidence / 'generator-journal.json').exists()


def test_plan_rejects_missing_source_digest(rewrite_transaction):
    _, evidence, changes, entry = rewrite_transaction
    meta = release.load(evidence, 'query-meta.json')
    del meta['tool_sha256']['perses/dashboard_reorg.py']
    release.save(evidence, 'query-meta.json', meta)
    with pytest.raises(AssertionError, match='Candidate source changed since audit: perses/dashboard_reorg.py'):
        transaction.plan(evidence, changes, rewrites=entry)
    assert not (evidence / 'generator-journal.json').exists()


def test_install_uses_audited_journal_bytes_after_source_changes(rewrite_transaction, candidate_source):
    runtime, evidence, changes, entry = rewrite_transaction
    source = candidate_source / 'perses/dashboard_reorg.py'
    audited = source.read_bytes()
    transaction.plan(evidence, changes, rewrites=entry)
    source.write_bytes(audited + b'\n# Edit after planning\n')
    transaction.install(evidence)
    assert (runtime / 'dashboard_reorg.py').read_bytes() == audited


def test_resume_rejects_changed_source_before_measurement(tmp_path, monkeypatch):
    resources = legacy_resources()
    candidate, changes = prepare(resources, 'generation-results')
    for name, value in [('before', resources), ('candidate', candidate), ('changes', changes), ('meta', {'batch': 'generation-results'})]:
        release.save(tmp_path, 'query-' + name + '.json', value)
    monkeypatch.setattr(release, 'prepare', lambda *_: ({}, []))
    monkeypatch.setattr(release, 'snapshot', lambda: pytest.fail('Must refuse changed candidate before network'))
    with pytest.raises(AssertionError, match='Candidate code changed'):
        release.audit(tmp_path, 'generation-results', resume=True)
