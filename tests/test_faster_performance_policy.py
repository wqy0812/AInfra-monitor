import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import materialized_release as release
import admit_when_ready as admission
from test_materialized_sequence import environment


@pytest.mark.parametrize('old,new,passed', [(1, .999, True), (2, 1.9, True), (.01, .009, True), (1, 1, False), (1, 1.01, False)])
def test_any_strict_median_gain_passes_without_absolute_ceiling(old, new, passed):
    pairs = [[old, new]] * 41
    result = release.evaluate_benchmark({'pairs': pairs, 'passed': not passed})
    assert result['passed'] is passed
    assert result['pairs'] == pairs and result['performance_policy'] == 'faster-median-v1'


@pytest.mark.parametrize('pairs', [[[1, .5]] * 40, [[1, float('nan')]] * 41, [[1, float('inf')]] * 41, [[0, .1]] * 41])
def test_missing_or_invalid_timings_cannot_pass(pairs):
    with pytest.raises(AssertionError, match='41 finite'):
        release.evaluate_benchmark({'pairs': pairs})


def interrupted(tmp_path, monkeypatch):
    resources, writes = environment(tmp_path, monkeypatch)
    catalog = json.loads(release.CATALOG.read_text())
    for panel in catalog['panels']: panel.update(revision='v1', variables={})
    release.CATALOG.write_text(json.dumps(catalog))
    root = tmp_path / 'batch-cpu'; release.prepare(root, 'cpu')
    meta = json.loads((root / 'batch-meta.json').read_text())
    checks = [{'panel': 'cpu', 'step': 60, 'filters': [], 'start': start, 'end': 43200,
               'passed': True, 'mode': 'whole_window', 'chunks': 1} for start in (0, -43200)]
    prior = {**meta, 'samples': 41, 'window_seconds': 43200, 'passed': False,
             'ranges': {'cpu:v1:60': [0, 43200]}, 'checks': checks,
             'benchmarks': [{'panel': 'cpu', 'step': 60, 'nocache': True, 'pairs': [[2, 1.9]] * 41,
                             'before_median': 2, 'after_median': 1.9, 'passed': False}]}
    (root / 'batch-performance.json').write_text(json.dumps(prior))
    monkeypatch.setattr(release, 'readiness', lambda *a: {'cpu:v1:60': [60, 43260]})
    monkeypatch.setattr(release, 'health', lambda: {})
    monkeypatch.setattr(release, 'coverage', lambda *a: dict.fromkeys(range(0, 43201, 60), 0))
    return root, prior, resources


def test_resume_regrades_existing_pairs_and_only_computes_missing_cases(tmp_path, monkeypatch):
    root, prior, _ = interrupted(tmp_path, monkeypatch)
    comparisons = []; queries = []; clock = [0]
    def compare(panel, expr, start, end, step):
        comparisons.append((start, end)); return {}, {'mode': 'whole_window', 'chunks': 1}
    def query(project, source, expr, start, end, step, nocache):
        queries.append((source, start, end, nocache))
        clock[0] += 2 if source == 'victoriametrics' else 1
    monkeypatch.setattr(release, 'compare_correctness_window', compare)
    monkeypatch.setattr(release, 'proxy_query', query)
    monkeypatch.setattr(release.time, 'monotonic', lambda: clock[0])
    release.audit(root, 'cpu', resume=True)
    result = json.loads((root / 'batch-performance.json').read_text())
    assert result['passed'] and result['reused_checks'] == 2 and result['reused_benchmarks'] == 1
    assert result['ranges'] == prior['ranges'], 'Moving live watermarks cannot shift stored comparison windows'
    assert result['benchmarks'][0]['pairs'] == prior['benchmarks'][0]['pairs']
    assert result['benchmarks'][0]['passed'] and len(result['benchmarks']) == 2
    assert comparisons == [(.123, 43200.123)] and len(result['checks']) == 3
    assert len(queries) == 82 and all(q[1:] == (0, 43200, False) for q in queries)


@pytest.mark.parametrize('bad', ['resources', 'catalog', 'duplicate', 'foreign_check'])
def test_resume_rejects_changed_resources_or_incompatible_evidence(tmp_path, monkeypatch, bad):
    root, prior, resources = interrupted(tmp_path, monkeypatch)
    if bad == 'resources': resources['dashboards'][0]['spec']['edited'] = True
    elif bad == 'catalog': prior['catalog_sha256'] = 'different'
    elif bad == 'duplicate': prior['benchmarks'].append(copy.deepcopy(prior['benchmarks'][0]))
    else: prior['checks'][0]['panel'] = 'unrelated'
    (root / 'batch-performance.json').write_text(json.dumps(prior))
    with pytest.raises(AssertionError): release.audit(root, 'cpu', resume=True)


def test_explicit_resume_keeps_original_snapshot_and_stops_before_next_batch(tmp_path, monkeypatch):
    root, _, _ = interrupted(tmp_path, monkeypatch)
    before = (root / 'batch-prepared.json').read_bytes(); calls = []
    (tmp_path / 'shadow-started.json').write_text('{"container_id":"api","image":"image"}')
    (tmp_path / 'shadow-observation.json').write_text('{"passed":true}')
    browser = tmp_path / 'browser.json'; browser.write_text('{}')
    monkeypatch.setattr(admission, 'fingerprint', lambda: [{'name': '/monitoring-api', 'id': 'api', 'image': 'image'}])
    monkeypatch.setattr(release, 'health', lambda: {'perses_acceleration': {'disabled_groups': [], 'state_error': None,
        'jobs': [{'job': 'cpu:v1:60', 'error': None}]}})
    def audit(root, group, resume=False):
        assert resume and group == 'cpu'; calls.append(group)
        (root / 'batch-performance.json').write_text('{"passed":true}')
    def impact(root, group): (root / 'non-target-performance.json').write_text('{"passed":true}')
    monkeypatch.setattr(release, 'audit', audit); monkeypatch.setattr(release, 'impact', impact)
    admission.main(tmp_path, tmp_path, browser, 1, resume_prepared='cpu')
    assert calls == ['cpu'] and (root / 'batch-prepared.json').read_bytes() == before
    assert not (tmp_path / 'batch-dcu/batch-before.json').exists()
    assert json.loads((root / 'batch-admission.json').read_text())['passed']
