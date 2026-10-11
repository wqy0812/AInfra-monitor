"""Chart writer ownership, compatible labels, and native cutover bounds."""
import json

import pytest
from monitoring import chart_recording as charts
from monitoring.api import encode


def test_rules_are_raw_only_compatible_and_bounded():
    rules = [r for g in charts.recording_rules(100)['groups'] for r in g['rules']]
    assert len(rules) == charts.RULE_COUNT == 32
    assert len({(r['record'], tuple(sorted(r['labels'].items()))) for r in rules}) == 32
    for r in rules:
        assert 'monitoring_chart_' not in r['expr']
        assert 'monitoring_perses_' not in r['expr']
        assert 'vector(time()) >= 100' in r['expr']
        assert set(r['labels']) <= {'path', 'environment', 'schema', 'node'}
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


def test_api_cannot_publish_native_fields_even_during_old_catchup():
    points = [{'ts': ts, 'nodes': {'prefill': {'cpu': 25, 'requests': 3}},
               'mooncake': {'capacity': {'used': 3, 'total': 10, 'ratio': .3}}} for ts in (5,100,105)]
    for env in ('a3-vllm', 'dcu-pd', 'xpu-pd'):
        body = encode(points, env, exclude_native=True)
        for path in charts.PATHS[env]:
            assert 'path=' + json.dumps(path) + ',' not in body
        assert 'path="nodes.prefill.requests"' in body
    assert 'path="nodes.prefill.cpu"' in encode(points, 'dcu-pd', exclude_native=True)
