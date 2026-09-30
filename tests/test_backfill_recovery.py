import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import backfill_recovery as recovery


def setup(tmp_path, monkeypatch, *, failure=False, complete=True):
    clock = [1000.0]; changes = []
    for name, value in {
        'shadow-started.json': {'container_id': 'new', 'image': 'image'},
        'prepared.json': {'protected': {'vm': 'same'}},
        'perses_acceleration_catalog.json': {'groups': ['a3'], 'steps': [5], 'panels': [{'id': 'a3', 'group': 'a3', 'revision': 'v1'}]},
    }.items(): (tmp_path / name).write_text(json.dumps(value))
    monkeypatch.setattr(recovery, 'time', SimpleNamespace(time=lambda: clock[0], sleep=lambda s: clock.__setitem__(0,clock[0]+s)))
    monkeypatch.setattr(recovery, 'inspect', lambda _: {'Id': 'new', 'Image': 'image'})
    monkeypatch.setattr(recovery, 'protected', lambda: {'vm': 'same'})
    monkeypatch.setattr(recovery, 'update', lambda *a, **k: changes.append((a[1],k)))
    def health():
        done = complete and clock[0] >= 1060
        normal = clock[0] >= 1120
        return {'status': 'ok', 'environments': {'a3': {'error': None,'processed_at':clock[0]-(5 if normal else 90)}},
            'perses_acceleration': {'jobs': [{'job':'a3:v1:5','lag_seconds':0 if normal else 3000,'error':None,
                'backfill':{'watermark':20 if done else 0,'end':20,'error':'partial write' if failure else None}}],
                'disabled_groups':[],'state_error':None,'parallel_a3_backfill':not changes,'backfill_priority_active':not done}}
    monkeypatch.setattr(recovery, 'health', health)
    return changes,clock


def test_backfill_impact_allowed_then_restore_and_wait_for_real_recovery(tmp_path, monkeypatch):
    changes,clock = setup(tmp_path,monkeypatch)
    recovery.main(tmp_path)
    assert changes == [('parallel-a3-off',{})]
    ready = json.loads((tmp_path/'recovery-ready.json').read_text())
    assert ready['at'] == 1120 and ready['model_lags']['a3'] == 5
    assert ready['state'] == 'ready_for_acceptance'
    assert not (tmp_path/'shadow-observation.json').exists()


def test_partial_writes_still_stop_group_during_maintenance(tmp_path,monkeypatch):
    changes,clock = setup(tmp_path,monkeypatch,failure=True,complete=False)
    with pytest.raises(RuntimeError,match='materialization failure'): recovery.main(tmp_path)
    assert ('disable',{'group':'a3'}) in changes
    assert changes[-1] == ('parallel-a3-off',{})
    assert not (tmp_path/'recovery-ready.json').exists()


def test_stale_supervisor_does_not_change_new_owner(tmp_path,monkeypatch):
    changes,clock = setup(tmp_path,monkeypatch)
    monkeypatch.setattr(recovery,'inspect',lambda _: {'Id':'replacement','Image':'image2'})
    with pytest.raises(AssertionError,match='API changed'): recovery.main(tmp_path)
    assert not changes
