import asyncio
import json

import pytest

from monitoring.perses_acceleration import AccelerationService, atomic_json


def setup(tmp_path):
    clock = [200000]
    panels = [{'id': group, 'revision': 'v1', 'group': group, 'variables': {}, 'expression': 'vector(1)'}
              for group in ('cpu', 'a3')]
    catalog = {'schema': 1, 'steps': [5], 'groups': ['cpu', 'a3'], 'panels': panels}
    service = AccelerationService('http://vm', tmp_path, catalog, now=lambda: clock[0])
    for panel in panels:
        service.watermarks[service.job_key(panel, 5)] = {'start': 190000, 'watermark': 199940}
    admin = {'disabled_groups': [], 'invalidated': [], 'parallel_a3_backfill': True,
             'backfill': {'seconds': 43200, 'requested_at': 200000}, 'backfill_priority_until': 207200}
    atomic_json(service.admin_path, admin)
    service.initialize_backfill(admin)
    return service, admin, clock


@pytest.mark.asyncio
async def test_two_workers_persist_disjoint_watermarks_and_cancel_together(tmp_path):
    service, admin, clock = setup(tmp_path)
    entered, release, persisted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = set(); completed = set(); peak = [0]
    async def materialize(panel, step, start, end, *, backfill):
        assert backfill and (end-start)//step+1 <= 60
        group = panel['group']; assert group not in active
        active.add(group); peak[0] = max(peak[0], len(active))
        if len(active) == 2: entered.set()
        try:
            await release.wait()
            if group in completed:
                persisted.set()
                await asyncio.Event().wait()
            completed.add(group)
            return end
        finally:
            active.remove(group)
    service.materialize = materialize
    service.online = 1; service.source_ready = lambda: False
    worker = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert {k.split(':')[1] for k in service.busy} == {'cpu', 'a3'}
        release.set()
        await asyncio.wait_for(persisted.wait(), 2)
        stored = json.loads(service.backfill_path.read_text())
        assert all(s['watermark'] == s['start'] + 59*5 for s in stored.values())
        assert peak[0] == 2
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError): await worker
        assert not service.busy and not active
        await service.close()


@pytest.mark.asyncio
async def test_runtime_flag_change_cannot_schedule_an_owned_batch_twice(tmp_path):
    service, admin, clock = setup(tmp_path)
    batch = service.next_batch(admin, 'a3')
    service.busy.add('backfill:' + batch[1])
    serial = dict(admin, parallel_a3_backfill=False)
    # CPU complete, A3 is still being written by the now-disabled helper.
    service.backfills['cpu:v1:5']['watermark'] = service.backfills['cpu:v1:5']['end']
    assert service.next_batch(serial, 'a3') is None
    assert service.next_batch(serial) is None
    service.busy.clear()
    assert service.next_batch(serial)[1] == 'a3:v1:5'
    await service.close()


@pytest.mark.asyncio
async def test_priority_expires_or_finishes_and_normal_live_priority_returns(tmp_path):
    service, admin, clock = setup(tmp_path)
    service.watermarks['cpu:v1:5']['watermark'] = 190000
    assert service.next_batch(admin)[-1] is True
    assert service.next_batch(admin, 'a3')[-1] is True
    clock[0] = admin['backfill_priority_until']
    assert not service.backfill_priority(admin)
    assert service.next_batch(admin)[-1] is False
    assert service.next_batch(admin, 'a3') is None
    clock[0] = 200000
    for state in service.backfills.values(): state['watermark'] = state['end']
    assert not service.backfill_priority(admin)
    assert not service.backfill_priority(dict(admin, disabled_groups=['cpu', 'a3']))
    await service.close()


@pytest.mark.asyncio
async def test_normal_mode_yields_to_online_queries_and_model_loop(tmp_path):
    service, admin, clock = setup(tmp_path)
    admin.pop('backfill_priority_until'); atomic_json(service.admin_path, admin)
    service.online = 1; service.source_ready = lambda: False
    calls = []
    async def materialize(*args, **kwargs): calls.append(True)
    service.materialize = materialize
    worker = asyncio.create_task(service.run())
    try:
        await asyncio.sleep(.05)
        assert not calls and not service.busy
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError): await worker
        await service.close()
