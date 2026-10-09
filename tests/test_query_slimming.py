import copy
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'perses'))
from query_slimming import BATCHES, RESULTS, apply, batch_targets, generation, histogram, plugin, prepare, replace_catalog
from acceleration_publication import published
from query_slimming_fixtures import legacy_catalog, legacy_resources

ROOT = Path(__file__).parents[1]


@pytest.fixture
def resources():
    return legacy_resources()


def test_all_batches_are_scoped_lossless_and_idempotent(resources):
    original = copy.deepcopy(resources)
    targets = set().union(*(batch_targets(batch) for batch in BATCHES))
    count = lambda r: sum(len(re.findall(r'\{[^{}]*\}', plugin(q)['query']))
                         for d in r['dashboards'] for key, p in d['spec']['panels'].items()
                         if (d['metadata']['project'], d['metadata']['name'], key) in targets
                         for q in p['spec']['queries'])
    assert count(resources) == 2420
    entries = []
    for batch, expected in zip(BATCHES, [3, 6, 6, 8]):
        before = copy.deepcopy(resources)
        resources, changes = prepare(resources, batch)
        assert len(changes) == expected
        assert prepare(resources, batch) == (resources, [])
        for c in changes:
            old, new = copy.deepcopy(c['before']), copy.deepcopy(c['after'])
            old['spec'].pop('queries'); new['spec'].pop('queries')
            assert old == new
            entries.append([c['project'], c['dashboard'], c['panel'], c['kind']])
        # Replacing exactly the changed query lists reconstructs the full output.
        for c in changes:
            d = next(d for d in before['dashboards'] if (d['metadata']['project'], d['metadata']['name']) == (c['project'], c['dashboard']))
            d['spec']['panels'][c['panel']] = c['after']
        assert before == resources
    assert count(resources) == 972
    assert apply(original, entries)[0] == resources
    state = json.loads((ROOT / 'perses/acceleration_state.json').read_text())
    assert published(original, dict(state, rewrites=entries)) == resources
    assert published(resources, dict(state, rewrites=entries)) == resources


def test_legacy_fixture_restores_baseline_from_fully_published_inputs(resources):
    baseline = copy.deepcopy(resources)
    previous = legacy_catalog()
    catalog, _, _ = replace_catalog(previous, resources)
    for batch in BATCHES:
        resources, _ = prepare(resources, batch)
    published_resources, published_catalog = copy.deepcopy(resources), copy.deepcopy(catalog)
    assert legacy_resources(resources) == baseline
    assert legacy_catalog(catalog) == previous
    assert resources == published_resources and catalog == published_catalog
    for batch in BATCHES:
        assert prepare(resources, batch) == (resources, [])


def test_generation_veto_and_source_matching(resources):
    _, changes = prepare(resources, 'generation-results')
    for c in changes:
        queries = c['after']['spec']['queries']
        assert len(queries) == 2 and queries[0] == c['before']['spec']['queries'][0]
        e = plugin(queries[1])['query']
        assert 'unless on(environment,result)' in e and 'count by(environment,result)' in e
        assert 'on(job,instance,environment,result)' not in e
        assert e.startswith('sort_by_label(label_del(')
        assert len(re.findall(r'\{[^{}]*\}', e)) == 36


@pytest.mark.parametrize('edit', ['result', 'rendering', 'option', 'template'])
def test_generation_unknown_templates_rejected(resources, edit):
    _, changes = prepare(resources, 'generation-results')
    panel = changes[0]['before']
    if edit == 'result':
        plugin(panel['spec']['queries'][1])['query'] += ' * 2'
    elif edit == 'rendering':
        panel['spec']['plugin']['spec']['querySettings'].append({'queryIndex': 1, 'colorValue': 'red'})
    elif edit == 'option':
        plugin(panel['spec']['queries'][2])['minStep'] = '1m'
    else:
        plugin(panel['spec']['queries'][0])['query'] = 'vector(1)'
    with pytest.raises(ValueError):
        generation(panel)


def test_histogram_rejects_non_adjacent_mixed_and_modified_mapping(resources):
    _, changes = prepare(resources, 'gateway-latency')
    old = plugin(changes[0]['before']['spec']['queries'][0])['query']
    new = histogram(old)
    assert histogram(new) == new
    assert new.count('label_map(') == 2
    assert '> ignoring(le)' not in new
    for broken in (old.replace(' > ignoring(le) ', ' > bool ignoring(le) ', 1),
                   new.replace('"le", ', '"le", "bad", "boundary", ', 1),
                   old.replace(' or (rate(', ' and (rate(', 1)):
        with pytest.raises(ValueError):
            histogram(broken)


def test_publication_rejects_unknown_duplicate_and_missing_batch(resources):
    with pytest.raises(ValueError):
        apply(resources, [['a3-monitoring', 'summary', 'unknown', 'histogram-monotonic']])
    entry = ['a3-monitoring', 'gateway-requests', 'generation-0', 'generation-results']
    with pytest.raises(ValueError):
        apply(resources, [entry, entry])
    with pytest.raises(ValueError):
        prepare({'dashboards': []}, 'generation-results')
