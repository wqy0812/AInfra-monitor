"""Persist only admitted acceleration changes across dashboard regeneration."""
import copy
import json
from pathlib import Path

from acceleration_catalog import targets
from query_acceleration import consolidate, target

NAME = 'perses-accelerated'
STATE = Path(__file__).with_name('acceleration_state.json')


def published(resources, state=None):
    if state is None:
        state = json.loads(STATE.read_text()) if STATE.exists() else {'schema': 1, 'merges': [], 'groups': []}
    if state['schema'] != 1 or set(state['groups']) - {'cpu', 'dcu', 'a3'}:
        raise ValueError('Invalid publication manifest')
    result = copy.deepcopy(resources)
    selected = {tuple(item) for item in state['merges']}
    if any(len(item) != 3 or not target(*item) for item in selected):
        raise ValueError('Unknown consolidation target')
    accelerated = {item[:3] for item in targets() if item[3] in state['groups']}
    projects = set()
    for d in result['dashboards']:
        project, name = d['metadata']['project'], d['metadata']['name']
        for key, panel in d['spec']['panels'].items():
            identity = (project, name, key)
            if identity in selected:
                kind = 'percentiles' if name == 'backend-performance' else 'stages' if key == 'live-stages' else 'operations'
                panel = consolidate(panel, kind)
                d['spec']['panels'][key] = panel
            if identity in accelerated:
                projects.add(project)
                for query in panel['spec']['queries']:
                    query['spec']['plugin']['spec']['datasource'] = {'kind': 'PrometheusDatasource', 'name': NAME}
    for project in sorted(projects):
        existing = next((d for d in result['datasources'] if d['metadata'].get('project') == project and d['metadata']['name'] == NAME), None)
        if existing:
            continue
        original = next(d for d in result['datasources'] if d['metadata'].get('project') == project and d['metadata']['name'] == 'victoriametrics')
        datasource = copy.deepcopy(original)
        datasource['metadata'] = {'project': project, 'name': NAME}
        datasource['spec']['display']['name'] = 'Perses 查询加速'
        datasource['spec']['default'] = False
        datasource['spec']['plugin']['spec']['proxy']['spec']['url'] = 'http://127.0.0.1:18430/internal/perses'
        result['datasources'].append(datasource)
    return result
