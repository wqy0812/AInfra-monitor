"""Catalog replacement retires jobs without destroying their durable evidence."""
import asyncio
import copy
import json

import pytest

from monitoring.perses_acceleration import AccelerationService, digest


STEPS = (5, 15, 20, 60, 120, 600, 3600)


def service_with_retired_jobs(tmp_path):
    panel = {'id': 'a3/panel', 'revision': 'current', 'group': 'a3',
             'variables': {}, 'expression': 'vector(1)'}
    catalog = {'schema': 1, 'steps': list(STEPS), 'groups': ['a3'], 'panels': [panel]}
    service = AccelerationService('http://127.0.0.1:1', tmp_path, catalog,
                                  client=object(), now=lambda: 200000)
    current = {service.job_key(panel, step) for step in STEPS}
    retired = {'a3/panel:retired:' + str(step) for step in STEPS}
    # Cover a removed panel and an obsolete step as well as prior revisions.
    retired.update({'a3/removed:old:5', 'a3/panel:current:30'})
    service.watermarks = {key: {'start': 100000, 'watermark': 100000}
                          for key in current | retired}
    service.backfills = {key: {'start': 1, 'end': 99995, 'watermark': 5,
                             'requested_at': 10} for key in retired}
    admin = {'disabled_groups': [], 'invalidated': [],
             'backfill': {'seconds': 43200, 'requested_at': 200000},
             'backfill_priority_until': 207200}
    return service, panel, current, retired, admin


def test_status_only_reports_current_revision_and_supported_steps(tmp_path):
    service, panel, current, retired, _ = service_with_retired_jobs(tmp_path)
    for key in retired:
        service.failures[key] = 'retired failure'
        service.waiting['backfill:' + key] = {'phase': 'retired pending'}
    active = service.job_key(panel, 5)
    service.failures[active] = 'current failure'
    durable = copy.deepcopy((service.watermarks, service.backfills))

    status = service.status()

    assert {job['job'] for job in status['jobs']} == current
    assert next(job for job in status['jobs'] if job['job'] == active)['error'] == 'current failure'
    assert (service.watermarks, service.backfills) == durable


def test_backfill_creation_preserves_retired_state_and_pending_bytes(tmp_path):
    service, _, current, retired, admin = service_with_retired_jobs(tmp_path)
    missing_history = next(iter(retired))
    del service.backfills[missing_history]
    # These retired starts would trigger expansion errors if reprocessed.
    for history in service.backfills.values():
        history['start'] = 99990
    pending = {}
    for key in retired:
        path = tmp_path / ('perses-backfill-pending-' + digest(key) + '.json')
        path.write_bytes(b'original retired pending evidence\n')
        pending[path] = path.read_bytes()
    old_watermarks = copy.deepcopy(service.watermarks)
    old_backfills = copy.deepcopy(service.backfills)

    service.initialize_backfill(admin)

    assert service.watermarks == old_watermarks
    assert {key: service.backfills[key] for key in old_backfills} == old_backfills
    assert missing_history not in service.backfills
    assert set(service.backfills) - set(old_backfills) == current
    saved = json.loads(service.backfill_path.read_text())
    for key in current:
        step = int(key.rsplit(':', 1)[1])
        assert saved[key] == {'start': 56800, 'end': 100000 - step,
                              'watermark': 56800 - step, 'requested_at': 200000}
    assert all(path.read_bytes() == before for path, before in pending.items())


def test_retired_incomplete_backfills_do_not_activate_priority(tmp_path):
    service, panel, _, _, admin = service_with_retired_jobs(tmp_path)
    assert not service.backfill_priority(admin)
    key = service.job_key(panel, 5)
    service.backfills[key] = {'start': 1, 'end': 10, 'watermark': 5}
    assert service.backfill_priority(admin)
    assert not service.backfill_priority(dict(admin, disabled_groups=['a3']))
    service.backfills[key]['watermark'] = 10
    assert not service.backfill_priority(admin)


@pytest.mark.parametrize('parallel', [False, True])
def test_scheduler_only_selects_current_catalog_jobs(tmp_path, parallel):
    service, _, current, _, admin = service_with_retired_jobs(tmp_path)
    service.initialize_backfill(admin)
    admin['parallel_a3_backfill'] = parallel
    for historical_group in (None, 'a3'):
        batch = service.next_batch(admin, historical_group)
        if historical_group and not parallel:
            assert batch is None
        else:
            assert batch is not None and batch[1] in current
    for key in current:
        step = int(key.rsplit(':', 1)[1])
        service.watermarks[key]['watermark'] = int((service.now() - 60) // step) * step
        service.backfills[key]['watermark'] = service.backfills[key]['end']
    assert service.next_batch(admin) is None
    assert service.next_batch(admin, 'a3') is None


@pytest.mark.asyncio
async def test_restart_adds_new_jobs_without_removing_retired_files(tmp_path):
    service, _, current, retired, _ = service_with_retired_jobs(tmp_path)
    old_watermarks = {key: service.watermarks[key] for key in retired}
    service.watermark_path.write_text(json.dumps(old_watermarks))
    service.backfill_path.write_text(json.dumps(service.backfills))
    old_backfill_bytes = service.backfill_path.read_bytes()
    pending_path = tmp_path / ('perses-pending-' + digest(next(iter(retired))) + '.json')
    pending_path.write_bytes(b'pending immutable old revision\n')
    restarted = AccelerationService(service.vm, tmp_path, service.catalog,
                                     client=object(), now=service.now)
    async def completed_worker(historical_group=None):
        await asyncio.sleep(0)
    restarted.run_worker = completed_worker

    await restarted.run()

    saved = json.loads(restarted.watermark_path.read_text())
    assert set(saved) == current | retired
    assert {key: saved[key] for key in retired} == old_watermarks
    assert {job['job'] for job in restarted.status()['jobs']} == current
    assert restarted.backfill_path.read_bytes() == old_backfill_bytes
    assert pending_path.read_bytes() == b'pending immutable old revision\n'
