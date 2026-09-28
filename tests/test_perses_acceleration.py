"""Failure isolation and exact-grid storage acceptance; fixtures stay on loopback."""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from monitoring.perses_acceleration import AccelerationService, PublicationPending, digest, install_routes, metric_identity, points_by_time


def catalog(panel, steps=(5, 15, 60)):
    return {'schema': 1, 'steps': list(steps), 'groups': ['cpu'], 'panels': [panel]}


@pytest_asyncio.fixture
async def vm_service(tmp_path):
    url = os.environ.get('PERSES_ACCELERATION_TEST_VM_URL')
    if not url:
        pytest.skip('Set PERSES_ACCELERATION_TEST_VM_URL to a disposable local VM')
    assert url.startswith('http://127.0.0.1:')
    token = uuid.uuid4().hex
    selector = 'acc_fixture{fixture="' + token + '",node=~"$node"}'
    panel = {'id': token, 'revision': 'v1', 'group': 'cpu', 'variables': {'node': ['.*', 'a', 'b']},
             'expression': selector + ' and (timestamp(' + selector + ') == time())'}
    start = int(time.time() // 60) * 60 - 1800
    service = AccelerationService(url, tmp_path, catalog(panel), now=lambda: start + 1200)
    lines = []
    for offset in range(-60, 961, 5):
        for node in ('a', 'b'):
            for rank in ('0', '1'):
                # Includes zero, gaps, disappearance/recovery and independent ranks.
                if 65 <= offset <= 90 or (node == 'b' and 120 <= offset < 150):
                    continue
                value = 0 if offset < 30 else int(rank) * 100 + offset / 7
                labels = f'fixture="{token}",node="{node}",rank="{rank}"'
                lines.append(f'acc_fixture{{{labels}}} {value} {(start + offset) * 1000}')
    await service.import_lines(lines)
    (await service.client.get(url + '/internal/force_flush')).raise_for_status()
    original_import = service.import_lines
    async def flushed(lines):
        await original_import(lines)
        (await service.client.get(url + '/internal/force_flush')).raise_for_status()
    service.import_lines = flushed
    try:
        yield service, panel, start
    finally:
        await service.close()


async def compare(service, panel, start, end, step, node='.*', extra=()):
    pairs = [('query', service.expression(panel, step, {'node': node})), ('start', str(start)),
             ('end', str(end)), ('step', str(step)), ('nocache', '1'), *extra]
    raw = await service.forward('GET', 'api/v1/query_range', pairs)
    actual = await service.request('GET', 'api/v1/query_range', pairs)
    assert actual[0] == raw[0] == 200
    left, right = [json.loads(body)['data']['result'] for _, body, _ in (raw, actual)]
    def canonical(rows):
        return {metric_identity(row['metric']): {t: float(v) for t, v in row['values']} for row in rows}
    left, right = canonical(left), canonical(right)
    assert left.keys() == right.keys()
    for identity in left:
        assert left[identity].keys() == right[identity].keys()
        assert right[identity] == pytest.approx(left[identity], rel=1e-9, abs=1e-10)
    return actual


@pytest.mark.asyncio
@pytest.mark.parametrize('step', [5, 15, 60])
async def test_real_vm_grid_labels_gaps_filters_old_history_and_tail(vm_service, step):
    service, panel, start = vm_service
    await service.materialize(panel, step, start, start + 180)
    # A completed business blank stays complete, without filling or raw fallback.
    covered = await service.stored(panel, step, list(range(start, start + 181, step)))
    assert len(covered) == 180 // step + 1
    if step in (5, 15):
        assert covered[start + 75] == {}
    for node in panel['variables']['node']:
        await compare(service, panel, start, start + 180, step, node)
        assert service.counters['fast_requests']
        await compare(service, panel, start - 60, start + 240, step, node)
    # Last 60 seconds and administratively invalidated history must use raw data.
    service.now = lambda: start + 210
    service.admin_path.write_text(json.dumps({'disabled_groups': [], 'invalidated': [
        {'panel': panel['id'], 'start': start + 30, 'end': start + 60}]}))
    await compare(service, panel, start - 60, start + 240, step)
    before = service.counters['fast_requests']
    await compare(service, panel, start + .123, start + 180.123, step)
    assert service.counters['fast_requests'] == before
    # Unknown options/steps must retain the raw query behavior.
    await compare(service, panel, start, start + 180, 7)
    await compare(service, panel, start, start + 180, step, extra=[('round_digits', '4')])
    await compare(service, panel, start, start + 180, step, extra=[('latency_offset', '5m')])
    await compare(service, panel, start, start + 180, step, extra=[('timeout', '2s')])
    assert service.counters['fast_requests'] == before


@pytest.mark.asyncio
async def test_empty_success_is_published_and_never_queries_source_again(vm_service):
    service, panel, start = vm_service
    panel['expression'] = 'missing_acceleration_fixture_' + panel['id']
    second = AccelerationService(service.vm, service.state, catalog(panel), client=service.client, now=service.now)
    second.import_lines = service.import_lines
    await second.materialize(panel, 5, start, start + 10)
    assert await second.stored(panel, 5, [start, start + 5, start + 10]) == {start: {}, start + 5: {}, start + 10: {}}
    calls = []
    forward = second.forward
    async def tracked(method, endpoint, pairs):
        calls.append(dict(pairs).get('query'))
        return await forward(method, endpoint, pairs)
    second.forward = tracked
    await compare(second, panel, start, start + 10, 5)
    assert calls == [panel['expression']]
    assert second.counters['fast_requests'] == 1
    await second.close()


@pytest.mark.asyncio
async def test_sealed_aligned_grid_matches_real_vm_in_both_cache_modes(vm_service):
    service, panel, start = vm_service
    for step in (5, 15, 20, 60, 120, 600, 3600):
        end = int((service.now() - 120) // step) * step
        left = end - 43200
        for nocache in ('1', '0', None):
            pairs = [('query', 'vector(1)'), ('start', str(left)), ('end', str(end)), ('step', str(step))]
            if nocache is not None: pairs.append(('nocache', nocache))
            native = await service.grid('GET', pairs)
            assert native == list(range(left, end + 1, step))


@pytest.mark.asyncio
async def test_online_cache_mode_keeps_publication_fresh_and_invalidation_immediate(vm_service):
    service, panel, start = vm_service
    observed = []
    original = service.vm_query
    async def tracked(*args, **kwargs):
        observed.append(kwargs.get('nocache', True))
        return await original(*args, **kwargs)
    service.vm_query = tracked
    await service.materialize(panel, 5, start, start + 180)
    assert observed and all(observed), 'Publication verification must always bypass caches'
    observed.clear()
    pairs = [('query', service.expression(panel, 5)), ('start', str(start)), ('end', str(start + 180)), ('step', '5'), ('nocache', '0')]
    assert (await service.request('GET', 'api/v1/query_range', pairs))[0] == 200
    assert observed == [False]
    assert (await service.request('GET', 'api/v1/query_range', pairs))[0] == 200
    assert observed == [False] and service.counters['response_cache_hits'] == 1
    forwarded = []; forward = service.forward
    async def tracked_forward(method, endpoint, pairs):
        forwarded.append(dict(pairs))
        return await forward(method, endpoint, pairs)
    service.forward = tracked_forward
    service.admin_path.write_text(json.dumps({'disabled_groups': [], 'invalidated': [
        {'panel': panel['id'], 'start': start + 30, 'end': start + 60}]}))
    assert (await service.request('GET', 'api/v1/query_range', pairs))[0] == 200
    assert [(int(p['start']), int(p['end'])) for p in forwarded] == [(start + 30, start + 60)]


@pytest.mark.asyncio
async def test_verified_response_cache_is_bounded_expires_and_respects_nocache(vm_service, monkeypatch):
    import monitoring.perses_acceleration as core
    service, panel, start = vm_service
    await service.materialize(panel, 5, start, start + 180)
    def pairs(end, nocache='0'):
        return [('query', service.expression(panel, 5)), ('start', str(start)), ('end', str(end)), ('step', '5'), ('nocache', nocache)]
    result = await service.request('GET', 'api/v1/query_range', pairs(start + 180))
    monkeypatch.setattr(core, 'RESPONSE_CACHE_BYTES', int(len(result[1]) * 1.5))
    for offset in (175, 170, 165):
        await service.request('GET', 'api/v1/query_range', pairs(start + offset))
        assert service.response_bytes <= core.RESPONSE_CACHE_BYTES
    assert len(service.responses) == 1
    await service.request('GET', 'api/v1/query_range', pairs(start + 165))
    hits = service.counters['response_cache_hits']; assert hits == 1
    await service.request('GET', 'api/v1/query_range', pairs(start + 165, '1'))
    assert service.counters['response_cache_hits'] == hits
    now = service.now(); service.now = lambda: now + 31
    await service.request('GET', 'api/v1/query_range', pairs(start + 165))
    assert service.counters['response_cache_hits'] == hits


@pytest.mark.asyncio
async def test_partial_history_and_realtime_tail_are_not_response_cached(vm_service):
    service, panel, start = vm_service
    await service.materialize(panel, 5, start, start + 180)
    for left, right in ((start - 30, start + 180), (start, int(service.now()))):
        pairs = [('query', service.expression(panel, 5)), ('start', str(left)), ('end', str(right)), ('step', '5'), ('nocache', '0')]
        for _ in range(2):
            assert (await service.request('GET', 'api/v1/query_range', pairs))[0] == 200
        assert not service.responses and service.counters['response_cache_hits'] == 0


@pytest.mark.asyncio
async def test_partial_write_restart_and_frozen_source_before_completion(vm_service):
    service, panel, start = vm_service
    original_import = service.import_lines
    async def partial(lines):
        await original_import(lines[:1])
    service.import_lines = partial
    async def once(check):
        return await check()
    service.read_back = once
    with pytest.raises(ValueError, match='values failed read-back'):
        await service.materialize(panel, 5, start, start + 10)
    assert await service.stored(panel, 5, [start, start + 5, start + 10]) == {}
    pending = next(service.state.glob('perses-pending-*.json'))
    frozen = json.loads(pending.read_text())
    second = AccelerationService(service.vm, service.state, catalog(panel), client=service.client, now=service.now)
    second.import_lines = original_import
    query = second.vm_query
    async def no_source(expression, *args, **kwargs):
        assert expression != panel['expression'].replace('$node', '.*'), 'Restart must reuse the durable source result'
        return await query(expression, *args, **kwargs)
    second.vm_query = no_source
    # An acknowledged partial write gets a bounded visibility grace period.
    # After that it must become a real failure and replay the identical batch.
    second.now = lambda: service.now() + 121
    with pytest.raises(ValueError, match='visibility timeout'):
        await second.materialize(panel, 5, start, start + 60)
    end = await second.materialize(panel, 5, start, start + 60)
    assert end == start + 10 and not pending.exists()
    actual = await second.stored(panel, 5, [start, start + 5, start + 10])
    assert {str(t): values for t, values in actual.items()} == frozen['values']
    await second.close()


@pytest.mark.asyncio
async def test_async_vm_visibility_yields_without_flush_or_reimport_and_survives_restart(vm_service):
    service, panel, start = vm_service
    imports, sources, waits = [], [], []
    query = service.vm_query
    async def tracked_query(expression, *args, **kwargs):
        if expression == service.expression(panel, 5):
            sources.append(expression)
        return await query(expression, *args, **kwargs)
    async def unflushed(lines):
        imports.append(lines)
        await AccelerationService.import_lines(service, lines)
    service.vm_query = tracked_query
    service.import_lines = unflushed
    active = service
    deadline = time.monotonic() + 30
    restarted = False
    while True:
        try:
            assert await active.materialize(panel, 5, start, start + 10) == start + 10
            break
        except PublicationPending as pending:
            waits.append(pending.phase)
            assert time.monotonic() < deadline
            if not restarted:
                active = AccelerationService(service.vm, service.state, catalog(panel), client=service.client, now=service.now)
                active.vm_query = tracked_query
                active.import_lines = unflushed
                restarted = True
            await asyncio.sleep(.5)
    assert waits and restarted
    assert len(sources) == 1 and len(imports) == 2
    assert len(await active.stored(panel, 5, [start, start + 5, start + 10])) == 3
    assert not list(service.state.glob('perses-pending*'))
    if active is not service:
        await active.close()


@pytest.mark.asyncio
async def test_backfill_pending_is_independent_of_forward_pending_and_resumes(vm_service):
    service, panel, start = vm_service
    original = service.import_lines
    async def partial(lines):
        await original(lines[:1])
    service.import_lines = partial
    for backfill, left in ((False, start + 300), (True, start)):
        with pytest.raises(PublicationPending):
            await service.materialize(panel, 5, left, left + 10, backfill=backfill)
    assert len(list(service.state.glob('*pending-*.json'))) == 2
    second = AccelerationService(service.vm, service.state, catalog(panel), client=service.client,
                                 now=lambda: service.now() + 121)
    second.import_lines = original
    for backfill, left in ((True, start), (False, start + 300)):
        with pytest.raises(ValueError, match='visibility timeout'):
            await second.materialize(panel, 5, left, left + 30, backfill=backfill)
        assert await second.materialize(panel, 5, left, left + 30, backfill=backfill) == left + 10
        assert len(await second.stored(panel, 5, [left, left + 5, left + 10])) == 3
    assert not list(service.state.glob('*pending-*.json'))
    await second.close()


@pytest.mark.asyncio
async def test_bounded_backfill_survives_restart_and_prioritizes_live_work(tmp_path):
    panel = {'id': 'cpu', 'revision': 'v1', 'group': 'cpu', 'variables': {}, 'expression': 'vector(1)'}
    service = AccelerationService('http://vm', tmp_path, catalog(panel), now=lambda: 200000)
    admin = {'disabled_groups': [], 'invalidated': [], 'backfill': {'seconds': 86400, 'requested_at': 200000}}
    for step in service.steps:
        start = 190000 // step * step
        service.watermarks[service.job_key(panel, step)] = {'start': start, 'watermark': 199920 // step * step}
    service.initialize_backfill(admin)
    original = json.loads(service.backfill_path.read_text())
    for key, state in original.items():
        step = int(key.rsplit(':', 1)[1])
        assert state['end'] - state['start'] + step == 86400
        assert state['end'] + step == service.watermarks[key]['start']
    service.last_lane = 'forward'
    batch = service.next_batch(admin)
    assert batch[-1] is True and (batch[4] - batch[0]) // batch[3] + 1 <= 60
    service.last_lane = 'backfill'
    assert service.next_batch(admin)[-1] is False
    urgent = service.job_key(panel, 5)
    service.watermarks[urgent]['watermark'] = 190000
    service.last_lane = 'forward'
    assert service.next_batch(admin)[1] == urgent and service.next_batch(admin)[-1] is False
    assert service.next_batch(dict(admin, disabled_groups=['cpu'])) is None
    for state in service.watermarks.values():
        state['watermark'] = 199980
    # Partial history is not contiguous 24h coverage, even when some ticks exist.
    assert all(j['coverage_start'] == j['started_at'] for j in service.status()['jobs'])
    service.backfills[urgent]['watermark'] = service.backfills[urgent]['end']
    job = next(j for j in service.status()['jobs'] if j['job'] == urgent)
    assert job['coverage_start'] == original[urgent]['start']
    second = AccelerationService('http://vm', tmp_path, catalog(panel), now=lambda: 300000)
    second.watermarks = service.watermarks
    second.initialize_backfill(admin)
    assert second.backfills == original, 'Restart must not extend or reset the historical request'
    await second.close(); await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('offset', [-30, -5])
async def test_shrink_to_12h_preserves_frozen_overlap_and_retires_only_old_pending(vm_service, offset):
    service, panel, start = vm_service
    key = service.job_key(panel, 5)
    service.watermarks[key] = {'start': start + 43200, 'watermark': start + 43200}
    service.backfills[key] = {'start': start - 43200, 'end': start + 43200 - 5,
                              'watermark': start + offset - 5, 'requested_at': 1}
    original = service.import_lines
    async def partial(lines): await original(lines[:1])
    service.import_lines = partial
    with pytest.raises(PublicationPending):
        await service.materialize(panel, 5, start + offset, start + offset + 10, backfill=True)
    pending_path = service.state / ('perses-backfill-pending-' + digest(key) + '.json')
    frozen = json.loads(pending_path.read_text())
    admin = {'backfill': {'seconds': 43200, 'requested_at': 2}}
    service.initialize_backfill(admin)
    assert service.backfills[key]['start'] == start
    assert service.backfills[key]['watermark'] == start - 5, 'Skipped history is not counted as completed samples'
    if offset == -30:
        assert not pending_path.exists()
        retired = list(service.state.glob('perses-backfill-pending-*-before-*.json'))
        assert len(retired) == 1 and json.loads(retired[0].read_text()) == frozen
    else:
        changed = json.loads(pending_path.read_text())
        assert changed['values'] == {t: v for t, v in frozen['values'].items() if int(t) >= start}
        assert changed['values_sent_at'] == frozen['values_sent_at']
        service.import_lines = original
        now = service.now(); service.now = lambda: now + 121
        with pytest.raises(ValueError, match='visibility timeout'):
            await service.materialize(panel, 5, start, start + 30, backfill=True)
        assert await service.materialize(panel, 5, start, start + 30, backfill=True) == start + 5
        assert len(await service.stored(panel, 5, [start, start + 5])) == 2
    # Repeated migration is idempotent; no expansion or progress reset.
    before = dict(service.backfills[key]); service.initialize_backfill(admin)
    assert service.backfills[key] == before
    with pytest.raises(ValueError, match='expansion'):
        service.initialize_backfill({'backfill': {'seconds': 86400, 'requested_at': 3}})


@pytest.mark.asyncio
async def test_calculation_failure_never_creates_completion(vm_service):
    service, panel, start = vm_service
    query = service.vm_query
    async def fail(expression, *args, **kwargs):
        if expression == service.expression(panel, 5):
            raise httpx.ReadTimeout('injected')
        return await query(expression, *args, **kwargs)
    service.vm_query = fail
    with pytest.raises(httpx.ReadTimeout):
        await service.materialize(panel, 5, start, start)
    assert not list(service.state.glob('perses-pending*'))
    assert await service.stored(panel, 5, [start]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [{'isPartial': True}, {'warnings': ['partial upstream data']}])
async def test_partial_success_cannot_be_published_as_empty(tmp_path, extra):
    async def transport(request):
        return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'matrix', 'result': []}, **extra})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service = AccelerationService('http://vm', tmp_path, {'schema': 1, 'steps': [5], 'groups': [], 'panels': []}, client=client)
        with pytest.raises(ValueError, match='Unexpected VM result'):
            await service.vm_query('vector(1)', 0, 5, 5)
        assert not list(tmp_path.iterdir())
        await service.close()


@pytest.mark.asyncio
async def test_inflight_sharing_cancellation_and_retry(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def transport(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={'status': 'success', 'data': {'result': []}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service = AccelerationService('http://vm', tmp_path, catalog({'id': 'x', 'revision': 'v1', 'group': 'cpu',
                    'variables': {}, 'expression': 'vector(1)'}), client=client)
        requests = [asyncio.create_task(service.request('POST', 'api/v1/query', [('query', 'unknown')])) for _ in range(8)]
        await entered.wait()
        requests[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await requests[0]
        release.set()
        results = await asyncio.gather(*requests[1:])
        assert len(calls) == 1 and len(set(results)) == 1
        assert service.counters['shared_requests'] == 7 and service.online == 0
        assert not service.inflight
        await service.request('POST', 'api/v1/query', [('query', 'unknown')])
        assert len(calls) == 2
        await service.close()


@pytest.mark.asyncio
async def test_proxy_restricts_writes_and_preserves_form_and_query_parameters(tmp_path):
    calls = []
    async def transport(request):
        calls.append((request.url, request.content))
        return httpx.Response(422, json={'status': 'error', 'error': 'original VM error'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service = AccelerationService('http://vm', tmp_path, {'schema': 1, 'steps': [5], 'groups': [], 'panels': []}, client=client)
        app = FastAPI(); app.state.perses_acceleration = service; install_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as caller:
            denied = await caller.post('/internal/perses/api/v1/import/prometheus', content='injected')
            assert denied.status_code == 403 and not calls
            result = await caller.post('/internal/perses/api/v1/query?nocache=1', data={'query': 'bad()'})
            assert result.status_code == 422 and result.json()['error'] == 'original VM error'
            assert calls[0][1] == b'query=bad%28%29&nocache=1'
        await service.close()
