"""Chart writer ownership, compatible labels, and native cutover bounds."""
import json

import pytest
from monitoring import chart_recording as charts


def test_rules_are_raw_only_compatible_and_bounded():
    rules = [r for g in charts.recording_rules(100)['groups'] for r in g['rules']]
    assert len(rules) == charts.RULE_COUNT == 6
    assert len({g['name'] for g in charts.recording_rules(100)['groups']}) == 3
    for r in rules:
        assert 'monitoring_chart_' not in r['expr']
        assert 'monitoring_perses_' not in r['expr']
        assert 'vector(time()) >= 100' in r['expr']
        assert not r.get('labels')
    for start in (-5, 1, 1.5, True):
        with pytest.raises(ValueError): charts.recording_rules(start)


def test_persistent_cutover_is_validated(tmp_path):
    assert charts.cutover(tmp_path) is None
    path = tmp_path / charts.CUTOVER_FILE
    path.write_text(json.dumps({'schema': 1, 'start': 100}))
    assert charts.cutover(tmp_path) == 100
    for stamp in [False, 101, -5, '100']:
        path.write_text(json.dumps({'schema': 1, 'start': stamp}))
        with pytest.raises(ValueError): charts.cutover(tmp_path)
