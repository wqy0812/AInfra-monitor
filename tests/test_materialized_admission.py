import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import materialized_release as release


def test_admission_requires_12h_all_steps_and_no_lag_or_errors(monkeypatch):
    catalog = {'steps': [5, 3600], 'panels': [{'id': 'panel', 'revision': 'v1', 'group': 'cpu'}]}
    state = {'perses_acceleration': {'disabled_groups': [], 'state_error': None, 'jobs': [
        {'job': 'panel:v1:' + str(step), 'started_at': 100800, 'processed_at': 100800 + 43200,
         'lag_seconds': 0, 'error': None} for step in catalog['steps']]}}
    monkeypatch.setattr(release, 'health', lambda: state)
    assert len(release.readiness(catalog, 'cpu')) == 2
    state['perses_acceleration']['jobs'][0]['processed_at'] -= 5
    with pytest.raises(AssertionError, match='12h'):
        release.readiness(catalog, 'cpu')
    state['perses_acceleration']['jobs'][0]['processed_at'] += 5
    state['perses_acceleration']['jobs'][0]['lag_seconds'] = 305
    with pytest.raises(AssertionError, match='behind'):
        release.readiness(catalog, 'cpu')
    state['perses_acceleration']['jobs'][0]['lag_seconds'] = 0
    state['perses_acceleration']['jobs'][0]['error'] = 'read-back failed'
    with pytest.raises(AssertionError):
        release.readiness(catalog, 'cpu')


def test_verified_backfill_can_admit_without_waiting_12h(monkeypatch):
    catalog = {'steps': [60], 'panels': [{'id': 'panel', 'revision': 'v1', 'group': 'cpu'}]}
    job = {'job': 'panel:v1:60', 'started_at': 200000, 'coverage_start': 156780,
           'processed_at': 200040, 'lag_seconds': 0, 'error': None, 'backfill': {'error': None}}
    monkeypatch.setattr(release, 'health', lambda: {'perses_acceleration': {
        'disabled_groups': [], 'state_error': None, 'jobs': [job]}})
    assert release.readiness(catalog, 'cpu') == {'panel:v1:60': [156840, 200040]}
    job['backfill']['error'] = 'partial write'
    with pytest.raises(AssertionError, match='Backfill failure'):
        release.readiness(catalog, 'cpu')


def test_completed_empty_is_coverage_but_missing_or_duplicate_markers_are_not(monkeypatch):
    panel = {'id': 'panel', 'revision': 'v1', 'project': 'project'}
    rows = [{'values': [[100, '0'], [105, '2'], [110, '0']]}]
    monkeypatch.setattr(release, 'proxy_query', lambda *args: rows)
    assert release.coverage(panel, 5, 100, 110) == {100: 0, 105: 2, 110: 0}
    rows[0]['values'].pop()
    with pytest.raises(AssertionError, match='Coverage gap'):
        release.coverage(panel, 5, 100, 110)
    rows[0]['values'].append([105, '2'])
    with pytest.raises(AssertionError, match='Ambiguous'):
        release.coverage(panel, 5, 100, 110)
