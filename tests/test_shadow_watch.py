import io
import json
import sys
import concurrent.futures
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import shadow_watch as watch
from admin import update


def setup(tmp_path, monkeypatch, *, lag=0, coverage=10):
    state = tmp_path / 'admin.json'; publication = tmp_path / 'publication.json'
    state.write_text(json.dumps({'disabled_groups': [], 'invalidated': []}))
    publication.write_text(json.dumps({'groups': [], 'merges': []}))
    (tmp_path / 'perses_acceleration_catalog.json').write_text(json.dumps({'groups': ['cpu'], 'panels': [{'id': 'p', 'group': 'cpu'}]}))
    monkeypatch.setattr(watch, 'STATE', state); monkeypatch.setattr(watch, 'PUBLICATION', publication)
    clock = [1000.0]
    monkeypatch.setattr(watch.time, 'time', lambda: clock[0])
    monkeypatch.setattr(watch.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def response(*args, **kwargs):
        health = {'status': 'ok', 'environments': {'env': {'processed_at': clock[0]-5, 'error': None}},
                  'perses_acceleration': {'state_error': None, 'counters': {}, 'jobs': [
                      {'job': 'p:revision:5', 'started_at': 0, 'processed_at': coverage, 'lag_seconds': lag, 'error': None}]}}
        return io.StringIO(json.dumps(health))
    monkeypatch.setattr(watch.urllib.request, 'urlopen', response)
    return state, publication


def test_watch_stops_only_faulty_shadow_group_after_sustained_backlog(tmp_path, monkeypatch):
    state, publication = setup(tmp_path, monkeypatch, lag=605)
    watch.main(tmp_path, 1)
    assert json.loads(state.read_text())['disabled_groups'] == ['cpu']
    assert len((tmp_path / 'shadow-watch.jsonl').read_text().splitlines()) == 3
    assert json.loads(publication.read_text()) == {'groups': [], 'merges': []}


def test_watch_readiness_is_not_dashboard_publication(tmp_path, monkeypatch):
    state, publication = setup(tmp_path, monkeypatch, coverage=86400)
    watch.main(tmp_path, 1)
    assert json.loads((tmp_path / 'shadow-ready.json').read_text())['groups'] == ['cpu']
    assert json.loads(state.read_text())['disabled_groups'] == []
    assert json.loads(publication.read_text())['groups'] == []


def test_pause_recovery_grace_is_bounded_and_does_not_hide_persistent_backlog(tmp_path, monkeypatch):
    state, publication = setup(tmp_path, monkeypatch, lag=605)
    watch.main(tmp_path, 1, catchup_seconds=60)
    assert json.loads(state.read_text())['disabled_groups'] == ['cpu']
    rows = [json.loads(line) for line in (tmp_path / 'shadow-watch.jsonl').read_text().splitlines()]
    assert len(rows) == 5
    assert [r['groups']['cpu']['consecutive_bad'] for r in rows] == [0, 0, 1, 2, 3]


def test_concurrent_invalidation_and_watcher_stop_preserve_both_changes(tmp_path):
    path = tmp_path / 'admin.json'
    def invalidate(i):
        update(path, 'invalidate', start=i, end=i+1, reason='source correction')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(invalidate, i) for i in range(20)]
        pending.append(pool.submit(update, path, 'disable', group='cpu'))
        for future in pending: future.result()
    result = json.loads(path.read_text())
    assert sorted(x['start'] for x in result['invalidated']) == list(range(20))
    assert result['disabled_groups'] == ['cpu']
