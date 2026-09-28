import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'perses'))
from acceleration_publication import published
from acceleration_catalog import targets
from project_split import read_resources


def test_only_admitted_panels_and_datasources_change():
    before = read_resources(Path(__file__).parents[1] / 'perses/projects')
    expected = {item[:3] for item in targets() if item[3] == 'cpu'}
    # Checked-in resources track actual releases. Construct the original CPU
    # state explicitly so this transition test remains meaningful after rollout.
    projects = {identity[0] for identity in expected}
    before['datasources'] = [d for d in before['datasources'] if not (
        d['metadata']['project'] in projects and d['metadata']['name'] == 'perses-accelerated')]
    for document in before['dashboards']:
        for key, panel in document['spec']['panels'].items():
            if (document['metadata']['project'], document['metadata']['name'], key) in expected:
                for query in panel['spec']['queries']:
                    query['spec']['plugin']['spec'].pop('datasource', None)
    state = {'schema': 1, 'merges': [], 'groups': ['cpu']}
    after = published(before, state)
    changed = set()
    for old, new in zip(before['dashboards'], after['dashboards']):
        for key, p in old['spec']['panels'].items():
            actual = new['spec']['panels'][key]
            if actual != p:
                identity = (old['metadata']['project'], old['metadata']['name'], key)
                changed.add(identity)
                actual = copy.deepcopy(actual)
                for query in actual['spec']['queries']:
                    assert query['spec']['plugin']['spec'].pop('datasource')['name'] == 'perses-accelerated'
                assert actual == p
    assert changed == expected
    assert after['projects'] == before['projects']
    assert all(d in after['datasources'] for d in before['datasources'])
    assert len(after['datasources']) == len(before['datasources']) + 2
    assert published(after, state) == after


def test_generator_keeps_default_and_accelerated_datasources_separate(tmp_path, monkeypatch):
    import acceleration_publication
    from dashboard_reorg import write_resources
    before = read_resources(Path(__file__).parents[1] / 'perses/projects')
    state = tmp_path / 'manifest.json'
    state.write_text(json.dumps({'schema': 1, 'merges': [], 'groups': ['cpu', 'dcu', 'a3']}))
    monkeypatch.setattr(acceleration_publication, 'STATE', state)
    write_resources(before, tmp_path / 'generated')
    actual = read_resources(tmp_path / 'generated')
    assert len(actual['datasources']) == 6
    for project in ('a3-monitoring', 'dcu-monitoring', 'xpu-monitoring'):
        original = json.loads((tmp_path / 'generated' / project / 'datasource.json').read_text())
        accelerated = json.loads((tmp_path / 'generated' / project / 'perses-accelerated-datasource.json').read_text())
        assert original['metadata']['name'] == 'victoriametrics' and original['spec']['default']
        assert accelerated['metadata']['name'] == 'perses-accelerated' and not accelerated['spec']['default']
