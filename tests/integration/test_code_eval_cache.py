"""Verify the archived code-eval cache rollout protects active tasks."""
import pytest

pytestmark = pytest.mark.cross_project


def test_rollout_freeze_does_not_pause_active_tasks(tmp_path, monkeypatch, code_eval_root):
    import importlib.util
    path = code_eval_root / 'deploy/releases/legacy_20260908_20260916/bin/cache_monitor_host.py'
    if not path.is_file():
        pytest.skip("This integration requires the archived code-eval cache rollout script")
    spec = importlib.util.spec_from_file_location('legacy_cache_monitor_host', path)
    rollout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rollout)
    from app.db import DB
    database = DB(tmp_path/'platform.db')
    database.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', ('active','accuracy','running','{}',0,0,None,'{}'))
    monkeypatch.setattr(rollout,'DB',database.path)
    monkeypatch.setattr(rollout,'ROOT',tmp_path/'release')
    baseline = {n:{'container':{'Id':n}} for n in rollout.NAMES}
    monkeypatch.setattr(rollout,'read',lambda name:baseline)
    monkeypatch.setattr(rollout,'inspect',lambda name:{'Id':name})
    monkeypatch.setattr(rollout,'idle',lambda:None)
    with pytest.raises(AssertionError,match='Active task'):
        rollout.freeze()
    assert database.run('active')['status']=='running'
    assert not database.state('maintenance')
    assert not (rollout.ROOT/'frozen.json').exists()
