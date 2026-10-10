"""Navigation consolidation preserves every chart and survives future publication."""
import copy
import json
from pathlib import Path

import pytest

from dashboard_columns import COLUMNS, apply, normalize_variable, origin, retired, sections
from project_split import build, read_resources, validate
from project_release import flattened, spec

ROOT = Path(__file__).parent


def resources():
    return read_resources(ROOT / 'projects')


def test_every_panel_query_renderer_and_datasource_survives_consolidation():
    before = resources()
    original = copy.deepcopy(before)
    after = apply(before)
    assert before == original
    validate(after)
    assert len(after['dashboards']) == 21
    assert sum(len(d['spec']['panels']) for d in after['dashboards']) == 249
    sources = flattened(before)
    accounted = set()
    for d in after['dashboards']:
        project, name = d['metadata']['project'], d['metadata']['name']
        for key, panel in d['spec']['panels'].items():
            source, oldkey = origin(name, key)
            identity = (project, source, oldkey)
            assert identity not in accounted
            accounted.add(identity)
            old = sources[('Dashboard', project, source)]['spec']['panels'][oldkey]
            if name == 'summary':
                assert panel['spec']['queries'] == old['spec']['queries']
                assert panel['spec']['plugin'] == old['spec']['plugin']
            else:
                assert panel == old
        if name in COLUMNS:
            refs = [item['content']['$ref'].rsplit('/', 1)[1] for row in d['spec']['layouts'] for item in row['spec']['items']]
            assert len(refs) == len(set(refs)) == len(d['spec']['panels'])
            for source, label in sections(project, name):
                original_rows = sources[('Dashboard', project, source)]['spec']['layouts']
                rows = [row for row in d['spec']['layouts'] if row['spec']['display']['title'].startswith(label)]
                assert len(rows) == len(original_rows)
                assert [row['spec'].get('display', {}).get('collapse') for row in rows] == [row['spec'].get('display', {}).get('collapse') for row in original_rows]
    assert len(accounted) == 249


def test_repeated_publication_and_live_snapshot_regeneration_stay_consolidated():
    after = apply(resources())
    assert apply(after) == after
    rebuilt = apply(build(after))
    assert {key: spec(d) for key, d in flattened(after).items()} == {key: spec(d) for key, d in flattened(rebuilt).items()}


def test_conflicting_variables_and_mixed_source_state_fail_closed():
    before = resources()
    d = next(d for d in before['dashboards'] if d['metadata']['name'] == 'accelerator-resources')
    d['spec']['variables'][0]['spec']['defaultValue'] = 'prefill'
    with pytest.raises(AssertionError, match='conflicting variable'):
        apply(before)
    mixed = apply(resources())
    mixed['dashboards'].append(next(d for d in resources()['dashboards'] if d['metadata']['name'] == 'gateway-requests'))
    with pytest.raises(AssertionError, match='mixed column/source state'):
        apply(mixed)


def test_role_filter_matches_both_cache_roles_and_hardware_nodes():
    import re
    for d in apply(resources())['dashboards']:
        if d['metadata']['name'] != 'model-monitoring':
            continue
        variable = next(v for v in d['spec']['variables'] if v['spec']['name'] == 'role')
        values = variable['spec']['plugin']['spec']['values']
        assert [v['label'] for v in values] == ['全部', 'Prefill', 'Decode']
        for option, role in zip(values[1:], ('prefill', 'decode')):
            assert re.fullmatch(option['value'], role)
            assert not re.fullmatch(option['value'], 'decode' if role == 'prefill' else 'prefill')
        assert normalize_variable(variable, d['metadata']['project']) == variable


def test_release_removes_old_entries_only_after_acceptance(tmp_path, monkeypatch):
    import project_release as release
    before = resources()
    server = flattened(before)
    (tmp_path / 'before.json').write_text(json.dumps(before))
    accepted = []

    def snapshot():
        return {category: [copy.deepcopy(d) for d in server.values() if d['kind'] == kind]
                for category, kind in (('projects', 'Project'), ('datasources', 'Datasource'), ('dashboards', 'Dashboard'))}

    # Preserve the original resource ordering for the initial concurrency check.
    (tmp_path / 'before.json').write_text(json.dumps(snapshot()))

    def http(url, method='GET', data=None):
        if method in ('POST', 'PUT'):
            server[release.key(data)] = copy.deepcopy(data)
            return data
        key = next(key for key, d in server.items() if release.endpoint(d['kind'], d) + '/' + key[-1] == url)
        if method == 'DELETE':
            assert accepted, 'Old entries removed before query acceptance'
            del server[key]
        else:
            return copy.deepcopy(server[key])

    def audit(*args, **kwargs):
        accepted.append(True)
        return {'passed': True}

    monkeypatch.setattr(release, 'snapshot', snapshot)
    monkeypatch.setattr(release, 'http', http)
    monkeypatch.setattr(release, 'audit', audit)
    release.apply(before, tmp_path)
    assert set(server) == set(flattened(apply(before)))
    journal = json.loads((tmp_path / 'journal.json').read_text())
    removed = [entry['before'] for entry in journal if entry['action'] == 'delete']
    assert len(removed) == 15
    assert all(d['metadata']['name'] in retired(d['metadata']['project']) for d in removed)
