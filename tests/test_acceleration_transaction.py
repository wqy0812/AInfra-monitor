import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import generator_transaction as transaction


def fixture(tmp_path, monkeypatch, guides=None):
    runtime = tmp_path / 'runtime'; runtime.mkdir()
    evidence = tmp_path / 'evidence'; evidence.mkdir()
    old = b'original generator\n'
    (runtime / 'dashboard_reorg.py').write_bytes(old)
    monkeypatch.setattr(transaction, 'RUNTIME', runtime)
    monkeypatch.setattr(transaction, 'BASE_HASH', hashlib.sha256(old).hexdigest())
    for name, content in (guides or {}).items():
        (runtime / name).write_text(content)
    document = {'metadata': {'project': 'project', 'name': 'dashboard'}, 'spec': {'panels': {
        'target': {'spec': {'queries': ['before']}}, 'untouched': {'spec': {'queries': ['other']}}}}}
    path = runtime / 'projects/project/dashboards/dashboard.json'; path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document))
    change = {'project': 'project', 'dashboard': 'dashboard', 'panel': 'target',
              'before': copy.deepcopy(document['spec']['panels']['target']), 'after': {'spec': {'queries': ['after']}}}
    transaction.plan(evidence, [change])
    return runtime, evidence, path


def test_generator_rollback_preserves_later_independent_batch(tmp_path, monkeypatch):
    runtime, evidence, path = fixture(tmp_path, monkeypatch)
    transaction.install(evidence)
    document = json.loads(path.read_text())
    assert document['spec']['panels']['target']['spec']['queries'] == ['after']
    document['spec']['panels']['untouched']['spec']['queries'] = ['later independent edit']
    path.write_text(json.dumps(document))
    state_path = runtime / 'acceleration_state.json'
    state = json.loads(state_path.read_text()); state['groups'] = ['cpu']; state_path.write_text(json.dumps(state))
    transaction.rollback(evidence)
    document = json.loads(path.read_text())
    assert document['spec']['panels']['target']['spec']['queries'] == ['before']
    assert document['spec']['panels']['untouched']['spec']['queries'] == ['later independent edit']
    assert json.loads(state_path.read_text()) == {'schema': 1, 'groups': ['cpu'], 'merges': []}
    assert (runtime / 'acceleration_publication.py').exists()


def test_generator_install_rejects_concurrent_edit(tmp_path, monkeypatch):
    runtime, evidence, path = fixture(tmp_path, monkeypatch)
    path.write_text('concurrent edit')
    with pytest.raises(AssertionError, match='Concurrent generator edit'):
        transaction.install(evidence)
    assert path.read_text() == 'concurrent edit'


def test_later_materialized_batch_keeps_previous_merge_and_rolls_back_own_binding(tmp_path, monkeypatch):
    runtime, evidence, path = fixture(tmp_path, monkeypatch)
    transaction.install(evidence)
    second = tmp_path / 'cpu'; second.mkdir()
    document = json.loads(path.read_text())
    change = {'project': 'project', 'dashboard': 'dashboard', 'panel': 'untouched',
              'before': copy.deepcopy(document['spec']['panels']['untouched']),
              'after': {'spec': {'queries': ['accelerated-source']}}}
    transaction.plan(second, [change], group='cpu')
    transaction.install(second)
    state_path = runtime / 'acceleration_state.json'
    assert json.loads(state_path.read_text())['groups'] == ['cpu']
    assert json.loads(path.read_text())['spec']['panels']['untouched']['spec']['queries'] == ['accelerated-source']
    transaction.rollback(second)
    state = json.loads(state_path.read_text())
    assert state['groups'] == [] and state['merges'] == [['project', 'dashboard', 'target']]
    document = json.loads(path.read_text())
    assert document['spec']['panels']['target']['spec']['queries'] == ['after']
    assert document['spec']['panels']['untouched']['spec']['queries'] == ['other']


@pytest.mark.parametrize('previous_note', [False, True])
def test_runtime_guides_survive_migrated_source_docs_and_repeated_publication(tmp_path, monkeypatch, previous_note):
    import re
    guide = '# Existing runtime guide\n\nKeep independent history.\n'
    if previous_note:
        guide += '\n## 查询加速与原有口径（2026-09-26）\n\nPrevious release note.\n'
    runtime, evidence, _ = fixture(tmp_path, monkeypatch, {
        'METRICS_GUIDE.md': guide, 'README.md': '# Runtime operations\n'})
    before = {name: (runtime / name).read_bytes() for name in ('METRICS_GUIDE.md', 'README.md')}
    transaction.install(evidence)
    document = runtime / 'perses-query-acceleration.md'
    assert document.is_file()
    assert 'monitoring_perses_complete' in document.read_text()
    for destination in re.findall(r'\]\(([^)]+)\)', document.read_text()):
        assert (runtime / destination).is_file(), destination
    first = {name: (runtime / name).read_bytes() for name in (*before, document.name)}
    for name in before:
        assert first[name].startswith(before[name].rstrip())
        assert first[name].count(b'](perses-query-acceleration.md)') == 1
    journal = json.loads((evidence / 'generator-journal.json').read_text())
    assert {e['path'] for e in journal['entries']} >= set(first)
    second = tmp_path / 'second'; second.mkdir()
    transaction.plan(second, [], group='cpu')
    transaction.install(second)
    assert {name: (runtime / name).read_bytes() for name in first} == first
    transaction.rollback(second)
    assert {name: (runtime / name).read_bytes() for name in first} == first


def test_runtime_document_edit_after_plan_blocks_install(tmp_path, monkeypatch):
    runtime, evidence, _ = fixture(tmp_path, monkeypatch, {'METRICS_GUIDE.md': '# Runtime guide\n'})
    guide = runtime / 'METRICS_GUIDE.md'
    guide.write_text('Concurrent operator note\n')
    with pytest.raises(AssertionError, match='Concurrent generator edit: METRICS_GUIDE.md'):
        transaction.install(evidence)
    assert guide.read_text() == 'Concurrent operator note\n'
