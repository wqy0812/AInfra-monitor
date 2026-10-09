import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'perses'))
from query_acceleration import consolidate, optimize, prepare
from project_split import read_resources

ROOT = Path(__file__).parents[1] / 'perses' / 'projects'


def test_full_snapshot_consolidation_preserves_every_other_field():
    before = read_resources(ROOT)
    after, changes = prepare(before)
    assert sum(len(d['spec']['panels']) for d in after['dashboards']) == 249
    assert sum(len(p['spec']['queries']) for d in after['dashboards'] for p in d['spec']['panels'].values()) == 327
    assert 0 <= len(changes) <= 14
    for change in changes:
        old, new = copy.deepcopy(change['before']), copy.deepcopy(change['after'])
        old['spec'].pop('queries'); new['spec'].pop('queries')
        assert old == new
        assert len(change['after']['spec']['queries']) == 1
        assert len(change['after']['spec']['queries'][0]['spec']['plugin']['spec']['query'].encode()) < 16000
    assert prepare(after) == (after, [])


def percentile_panel():
    document = json.loads((ROOT / 'dcu-monitoring/dashboards/backend-performance.json').read_text())
    panel = document['spec']['panels']['core-ttft']
    if len(panel['spec']['queries']) == 1:
        q = panel['spec']['queries'][0]
        panel['spec']['queries'] = []
        for percentile in ('p50', 'p95', 'p99'):
            item = copy.deepcopy(q)
            item['spec']['plugin']['spec']['query'] = item['spec']['plugin']['spec']['query'].replace('(p50|p95|p99)', percentile)
            panel['spec']['queries'].append(item)
    return panel


@pytest.mark.parametrize('change', ['query', 'style', 'options'])
def test_refuse_unrelated_edits(change):
    p = percentile_panel()
    if change == 'query':
        p['spec']['queries'][1]['spec']['plugin']['spec']['query'] += ' * 2'
    elif change == 'style':
        p['spec']['plugin']['spec']['querySettings'] = [{'queryIndex': 0, 'colorValue': 'red'}]
    else:
        p['spec']['queries'][1]['spec']['plugin']['spec']['minStep'] = '1m'
    with pytest.raises(ValueError):
        consolidate(p, 'percentiles')


def test_percentile_union_does_not_recalculate_or_drop_guards():
    p = percentile_panel()
    original = p['spec']['queries'][0]['spec']['plugin']['spec']['query']
    after = consolidate(p, 'percentiles')
    expr = after['spec']['queries'][0]['spec']['plugin']['spec']['query']
    assert original.replace('.p50"', '.(p50|p95|p99)"') in expr
    assert expr.startswith('sort_by_label((label_replace(')
