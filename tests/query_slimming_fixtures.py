"""Explicit legacy query inputs, independent of the current release state."""
import copy
import json
from functools import lru_cache
from pathlib import Path

from project_split import read_resources

ROOT = Path(__file__).parents[1]


@lru_cache(maxsize=1)
def _legacy():
    value = json.loads((Path(__file__).parent / 'fixtures/query_slimming_legacy.json').read_text())
    assert value['schema'] == 1
    identities = [(p['project'], p['dashboard'], p['panel']) for p in value['panels']]
    assert len(identities) == len(set(identities)) == 23
    assert len({p['id'] for p in value['a3_catalog_entries']}) == 8
    return value


def legacy_panel(project, dashboard, panel):
    return copy.deepcopy(next(p['before'] for p in _legacy()['panels']
                             if (p['project'], p['dashboard'], p['panel']) == (project, dashboard, panel)))


def legacy_resources(current=None):
    resources = copy.deepcopy(current) if current is not None else read_resources(ROOT / 'perses/projects')
    documents = {(d['metadata']['project'], d['metadata']['name']): d for d in resources['dashboards']}
    for entry in _legacy()['panels']:
        panels = documents[entry['project'], entry['dashboard']]['spec']['panels']
        assert entry['panel'] in panels, 'Legacy target no longer exists'
        panels[entry['panel']] = copy.deepcopy(entry['before'])
    return resources


def legacy_catalog(current=None):
    catalog = copy.deepcopy(current) if current is not None else json.loads(
        (ROOT / 'monitoring/perses_acceleration_catalog.json').read_text())
    entries = {p['id']: p for p in _legacy()['a3_catalog_entries']}
    assert entries.keys() <= {p['id'] for p in catalog['panels']}, 'Legacy catalog target no longer exists'
    catalog['panels'] = [copy.deepcopy(entries.get(p['id'], p)) for p in catalog['panels']]
    return catalog
