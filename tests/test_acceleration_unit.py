"""Acceleration contracts exercised offline, including durable publication retries."""
import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from monitoring import perses_acceleration as core


@pytest_asyncio.fixture
async def acceleration(tmp_path):
    panel = {'id': 'cpu', 'revision': 'v1', 'group': 'cpu', 'variables': {'node': ['.*', 'a']},
             'expression': 'metric{node=~"$node"}[$__interval]'}
    catalog = {'schema': 1, 'steps': [5, 60], 'groups': ['cpu'], 'panels': [panel]}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    clock = [1000.0]
    service = core.AccelerationService('http://fixture/', tmp_path, catalog, client=client, now=lambda: clock[0])
    try:
        yield service, panel, clock
    finally:
        await service.close()
        await client.aclose()


@pytest.mark.parametrize('value,expected', [('1ms', .001), ('2s', 2), ('2.5m', 150), ('1h', 3600), ('5', 5)])
def test_duration_accepts_vm_units(value, expected):
    assert core.duration(value) == expected


@pytest.mark.parametrize('value', ['NaN', 'Inf', '-Inf', '3d', '', '5S', 'bad'])
def test_duration_rejects_unbounded_or_unknown_units(value):
    with pytest.raises(ValueError):
        core.duration(value)


@pytest.mark.parametrize('labels', [[], {'node': 1}, None])
def test_stored_label_identity_requires_string_mapping(labels):
    encoded = base64.urlsafe_b64encode(json.dumps(labels).encode())
    with pytest.raises(ValueError, match='Invalid stored labels'):
        core.decode_identity(encoded)


@pytest.mark.parametrize('values', [[[100, 'NaN']], [[101, '1']], [[100, '1'], [100, '2']]])
def test_sample_grid_rejects_nan_off_grid_and_duplicates(values):
    with pytest.raises(ValueError):
        core.points_by_time([{'metric': {'node': 'a'}, 'values': values}], [100])


def test_series_roundtrip_preserves_empty_ticks_labels_and_vm_order():
    a = core.metric_identity({'node': 'a', 'perses_quantile': '0.99'})
    b = core.metric_identity({'node': 'a', 'perses_quantile': '0.50'})
    other = core.metric_identity({'node': 'b'})
    values = {105: {a: '2'}, 100: {a: '0', b: '1', other: '3'}, 110: {}}
    rows = core.result_rows(values, {'node': 'a'})
    assert [row['metric']['perses_quantile'] for row in rows] == ['0.50', '0.99']
    assert core.points_by_time(rows, [100, 105, 110]) == {100: {a: '0', b: '1'}, 105: {a: '2'}, 110: {}}
    assert core.equal_values({'a': '0.1'}, {'a': '0.10000000001'})
    assert not core.equal_values({'a': '1'}, {'b': '1'})
    assert not core.equal_values({'a': '1'}, {'a': '2'})


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'status': 'error'}, {'status': 'success', 'data': {'resultType': 'vector'}},
    {'status': 'success', 'data': {'resultType': 'matrix'}, 'isPartial': True},
    {'status': 'success', 'data': {'resultType': 'matrix'}, 'warnings': ['partial']},
])
async def test_vm_query_rejects_partial_or_wrong_results(acceleration, body):
    service, _, _ = acceleration
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    try:
        with pytest.raises(ValueError, match='Unexpected VM result'):
            await service.vm_query('metric', 100, 110, 5)
    finally:
        await service.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['negative', 'fractional', 'off_grid', 'duplicate_series', 'bad_labels'])
async def test_cached_storage_does_not_accept_corrupt_publication(acceleration, fault):
    service, panel, _ = acceleration
    identity = core.metric_identity({'node': 'a'})
    tags = {'generation': 'g', '__name__': core.PREFIX + 'complete'}
    rows = [{'metric': tags, 'values': [[100, '-1' if fault == 'negative' else '0.5' if fault == 'fractional' else '1']]}]
    if fault == 'off_grid':
        rows[0]['values'][0][0] = 101
    if fault in ('duplicate_series', 'bad_labels'):
        tags = {'generation': 'g', '__name__': core.PREFIX + 'value', 'series': identity if fault == 'duplicate_series' else 'bad'}
        rows.extend([{'metric': tags, 'values': [[100, '1']]}] * (2 if fault == 'duplicate_series' else 1))
    service.vm_query = AsyncMock(return_value=rows)
    with pytest.raises(ValueError):
        await service.stored(panel, 5, [100])


@pytest.mark.asyncio
async def test_ambiguous_markers_and_missing_samples_are_not_coverage(acceleration):
    service, panel, _ = acceleration
    rows = [{'metric': {'generation': 'g', '__name__': core.PREFIX + 'complete'}, 'values': [[100, '0'], [105, '1']]},
            {'metric': {'generation': 'old', '__name__': core.PREFIX + 'complete'}, 'values': [[100, '0'], [110, '0']]}]
    service.vm_query = AsyncMock(return_value=rows)
    assert await service.stored(panel, 5, [100, 105, 110]) == {110: {}}


@pytest.mark.asyncio
@pytest.mark.parametrize('start,end', [(101, 110), (100, 111), (100, 400), (940, 945)])
async def test_materialization_rejects_unsafe_ranges_before_io(acceleration, start, end):
    service, panel, _ = acceleration
    service.stored = AsyncMock()
    with pytest.raises(ValueError, match='Unsafe materialization range'):
        await service.materialize(panel, 5, start, end)
    service.stored.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('backfill', [False, True])
async def test_materialization_freezes_values_and_retries_without_duplicate_import(acceleration, backfill):
    service, panel, clock = acceleration
    identity = core.metric_identity({'node': 'a'})
    samples = {100: {identity: '0'}, 105: {identity: '2'}}
    prefix = 'perses-backfill-pending-' if backfill else 'perses-pending-'
    path = service.state / (prefix + core.digest(service.job_key(panel, 5)) + '.json')
    service.stored = AsyncMock(return_value={})
    source = [{'metric': {'node': 'a'}, 'values': [[100, '0'], [105, '2']]}]
    verified = [{'metric': {'series': identity}, 'values': [[100, '0'], [105, '2']]}]
    service.vm_query = AsyncMock(side_effect=[source, [], []])
    service.import_lines = AsyncMock()
    with pytest.raises(core.PublicationPending, match='Materialized values'):
        await service.materialize(panel, 5, 100, 105, backfill=backfill)
    frozen = json.loads(path.read_text())
    assert frozen['digest'] == core.digest(samples) and 'complete_sent_at' not in frozen
    with pytest.raises(core.PublicationPending):
        await service.materialize(panel, 5, 100, 110, backfill=backfill)
    assert service.import_lines.await_count == 1
    assert service.vm_query.await_count == 3  # Source computed once, two read-backs.
    clock[0] += core.VISIBILITY_TIMEOUT
    service.vm_query.side_effect = None
    service.vm_query.return_value = []
    with pytest.raises(ValueError, match='visibility timeout'):
        await service.materialize(panel, 5, 100, 110, backfill=backfill)
    assert 'values_sent_at' not in json.loads(path.read_text())
    service.vm_query.return_value = verified
    service.stored.side_effect = [{}, samples]
    assert await service.materialize(panel, 5, 100, 110, backfill=backfill) == 105
    assert service.import_lines.await_count == 3  # Identical values replay, then completion.
    assert service.import_lines.call_args_list[0] == service.import_lines.call_args_list[1]
    assert not path.exists()


@pytest.mark.asyncio
async def test_empty_results_publish_completion_and_corrupt_pending_never_imports(acceleration):
    service, panel, _ = acceleration
    service.stored = AsyncMock(side_effect=[{}, {100: {}, 105: {}}])
    service.vm_query = AsyncMock(return_value=[])
    service.import_lines = AsyncMock()
    assert await service.materialize(panel, 5, 100, 105) == 105
    assert service.import_lines.await_count == 1
    assert all('monitoring_perses_complete' in line for line in service.import_lines.call_args.args[0])
    path = service.state / ('perses-pending-' + core.digest(service.job_key(panel, 5)) + '.json')
    path.write_text(json.dumps({'start': 100, 'end': 105, 'values': {'100': {}}, 'digest': 'broken'}))
    service.stored.side_effect = None
    service.stored.return_value = {}
    with pytest.raises(ValueError, match='Invalid durable'):
        await service.materialize(panel, 5, 100, 105)
    with pytest.raises(ValueError, match='watermark'):
        await service.materialize(panel, 5, 105, 110)
    assert service.import_lines.await_count == 1 and path.exists()


@pytest.mark.asyncio
async def test_response_cache_lru_expiry_and_admin_invalidation(acceleration, monkeypatch):
    service, _, clock = acceleration
    monkeypatch.setattr(core, 'RESPONSE_CACHE_ITEMS', 2)
    monkeypatch.setattr(core, 'RESPONSE_CACHE_BYTES', 8)
    version = core.digest(service.admin())
    response = (200, b'abc', 'application/json')
    for key in ('a', 'b'):
        service.remember_response(key, version, response)
    assert service.cached_response('a', version) == response
    service.remember_response('c', version, response)
    assert service.cached_response('b', version) is None and service.response_bytes == 6
    service.remember_response('a', version, (200, b'1', 'application/json'))
    assert service.response_bytes == 4
    service.remember_response('huge', version, (200, b'x' * 9, 'application/json'))
    assert 'huge' not in service.responses
    clock[0] += 30
    assert service.cached_response('a', version) is None and service.response_bytes == 3
    assert service.cached_response('c', 'new admin') is None and service.response_bytes == 0
    service.remember_response('stale', 'old admin', response)
    assert not service.responses


def query_pairs(service, panel, **changes):
    params = dict(query=service.expression(panel, 5), start='100', end='110', step='5', nocache='1')
    params.update(changes)
    return list(params.items())


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'step': '7'}, {'step': '5.5'}, {'start': 'NaN'}, {'start': '120'},
    {'end': '1000000'}, {'latency_offset': '0'}, {'latency_offset': '5m'},
    {'timeout': '1s'}, {'query': 'unknown'},
])
async def test_unsupported_queries_preserve_raw_vm_semantics(acceleration, changes):
    service, panel, _ = acceleration
    service.forward = AsyncMock(return_value=(200, b'raw', 'text/plain'))
    service.stored = AsyncMock()
    pairs = query_pairs(service, panel, **changes)
    assert await service.execute('GET', 'api/v1/query_range', pairs) == (200, b'raw', 'text/plain')
    service.forward.assert_awaited_once_with('GET', 'api/v1/query_range', pairs)
    service.stored.assert_not_called()
    assert service.online == 0


@pytest.mark.asyncio
async def test_partial_cache_queries_only_missing_grid_and_preserves_warnings(acceleration):
    service, panel, _ = acceleration
    identity = core.metric_identity({'node': 'a'})
    service.stored = AsyncMock(return_value={100: {identity: '0'}, 110: {identity: '2'}})
    piece = {'data': {'result': [{'metric': {'node': 'a'}, 'values': [[105, '1']]}]},
             'warnings': ['warn', 'warn'], 'infos': ['info', 'info']}
    service.forward = AsyncMock(return_value=(200, json.dumps(piece).encode(), 'application/json'))
    status, body, _ = await service.execute('GET', 'api/v1/query_range', query_pairs(service, panel))
    result = json.loads(body)
    assert status == 200 and result['data']['result'][0]['values'] == [[100, '0'], [105, '1'], [110, '2']]
    assert result['warnings'] == ['warn'] and result['infos'] == ['info']
    params = dict(service.forward.call_args.args[2])
    assert params['start'] == params['end'] == '105'


@pytest.mark.asyncio
async def test_aligned_complete_empty_cache_never_falls_back(acceleration):
    service, panel, _ = acceleration
    service.stored = AsyncMock(return_value={100: {}, 105: {}, 110: {}})
    service.forward = AsyncMock(side_effect=AssertionError('empty is a complete result'))
    pairs = query_pairs(service, panel, nocache='0')
    for _ in range(2):
        status, body, _ = await service.execute('GET', 'api/v1/query_range', pairs)
        assert status == 200 and json.loads(body)['data']['result'] == []
    assert service.stored.await_count == 1 and service.counters['response_cache_hits'] == 1


@pytest.mark.asyncio
async def test_reader_cancellation_does_not_cancel_shared_query_and_close_cleans_tasks(acceleration):
    service, panel, _ = acceleration
    entered, finish = asyncio.Event(), asyncio.Event()
    async def execute(*args):
        entered.set()
        await finish.wait()
        return (200, b'complete', 'text/plain')
    service.execute = AsyncMock(side_effect=execute)
    pairs = query_pairs(service, panel)
    first = asyncio.create_task(service.request('GET', 'api/v1/query_range', pairs))
    await entered.wait()
    second = asyncio.create_task(service.request('GET', 'api/v1/query_range', pairs))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    assert await second == (200, b'complete', 'text/plain')
    await asyncio.sleep(0)
    assert service.execute.await_count == 1 and not service.inflight
    finish.clear()
    pending = asyncio.create_task(service.request('GET', 'api/v1/query_range', pairs))
    await asyncio.sleep(0)
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not service.inflight and not service.client.is_closed  # Borrowed pool belongs to the owner.


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [core.PublicationPending('values', 100), ValueError('bad source'), httpx.ConnectError('offline')])
async def test_worker_failures_keep_watermark_and_release_slot(acceleration, monkeypatch, error):
    service, panel, clock = acceleration
    job = service.job_key(panel, 5)
    service.watermarks[job] = {'start': 100, 'watermark': 95}
    service.next_batch = lambda *a: (100, job, panel, 5, 110, False)
    service.materialize = AsyncMock(side_effect=error)
    async def sleep(_):
        service.stopping = True
    monkeypatch.setattr(core.asyncio, 'sleep', sleep)
    await service.run_worker()
    assert service.watermarks[job]['watermark'] == 95 and not service.busy
    if isinstance(error, core.PublicationPending):
        assert service.counters['visibility_waits'] == 1 and service.retry_at[job] == clock[0] + 5
        assert service.waiting[job] == {'phase': 'values', 'since': 100}
    else:
        assert service.counters['worker_failures'] == 1 and service.retry_at[job] == clock[0] + 30
        assert type(error).__name__ in service.failures[job]


@pytest.mark.asyncio
async def test_proxy_rejects_writes_bad_forms_and_large_queries(acceleration):
    service, _, _ = acceleration
    service.request = AsyncMock(return_value=(200, b'ok', 'text/plain'))
    app = FastAPI()
    core.install_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        assert (await client.get('/internal/perses/api/v1/query')).status_code == 503
        app.state.perses_acceleration = service
        assert (await client.get('/internal/perses/api/v1/import')).status_code == 403
        assert (await client.post('/internal/perses/api/v1/query', json={})).status_code == 415
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        assert (await client.post('/internal/perses/api/v1/query', content=b'a' * (1024 * 1024 + 1), headers=headers)).status_code == 413
        response = await client.post('/internal/perses/api/v1/query?query=url', content=b'query=body&empty=', headers=headers)
        assert response.status_code == 200 and response.text == 'ok'
        service.request.assert_awaited_once_with('POST', 'api/v1/query', [('query', 'body'), ('empty', ''), ('query', 'url')])
        service.request.side_effect = httpx.ConnectError('offline')
        assert (await client.get('/internal/perses/api/v1/query')).status_code == 503


@pytest.mark.asyncio
async def test_proxy_forward_preserves_get_and_form_post_parameters(acceleration):
    service, _, _ = acceleration
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(418, content=b'original error', headers={'content-type': 'text/plain'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service.client = client
        pairs = [('query', 'a+b'), ('match[]', 'one'), ('match[]', 'two')]
        assert await service.forward('GET', 'api/v1/query', pairs) == (418, b'original error', 'text/plain')
        assert list(seen[0].url.params.multi_items()) == pairs
        assert await service.forward('POST', 'api/v1/query', pairs) == (418, b'original error', 'text/plain')
        assert seen[1].content == b'query=a%2Bb&match%5B%5D=one&match%5B%5D=two'
        assert seen[1].headers['content-type'] == 'application/x-www-form-urlencoded'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,rows', [(503, []), (200, []), (200, [{}, {}])])
async def test_grid_failure_cannot_become_valid_cache_coverage(acceleration, status, rows):
    service, _, _ = acceleration
    service.forward = AsyncMock(return_value=(status, json.dumps({'data': {'result': rows}}).encode(), 'application/json'))
    with pytest.raises(ValueError):
        await service.grid('GET', [('query', 'original')])


@pytest.mark.asyncio
async def test_administrative_state_corruption_falls_back_and_reports_error(acceleration):
    service, panel, _ = acceleration
    service.admin_path.write_text('{"disabled_groups":0,"invalidated":[]}')
    assert service.status()['state_error'] == 'ValueError'
    service.forward = AsyncMock(return_value=(200, b'raw', 'text/plain'))
    assert await service.execute('GET', 'api/v1/query_range', query_pairs(service, panel)) == (200, b'raw', 'text/plain')
    assert service.online == 0 and service.counters['raw_requests'] == 1


@pytest.mark.asyncio
async def test_completed_pending_is_removed_without_recomputing_source(acceleration):
    service, panel, _ = acceleration
    path = service.state / ('perses-pending-' + core.digest(service.job_key(panel, 5)) + '.json')
    path.write_text('{"start":100,"end":105}')
    service.stored = AsyncMock(return_value={100: {}, 105: {}})
    service.vm_query = AsyncMock()
    assert await service.materialize(panel, 5, 100, 105) == 105
    assert not path.exists()
    service.vm_query.assert_not_called()


@pytest.mark.asyncio
async def test_worker_success_and_bad_state_release_slots_without_stale_errors(acceleration, monkeypatch):
    service, panel, _ = acceleration
    job = service.job_key(panel, 5)
    service.watermarks[job] = {'start': 100, 'watermark': 95}
    service.failures.update({job: 'old error', 'state': 'old error'})
    service.waiting[job] = {'phase': 'old'}
    service.next_batch = lambda *a: (100, job, panel, 5, 110, False)
    service.materialize = AsyncMock(return_value=110)
    async def stop(_):
        service.stopping = True
    monkeypatch.setattr(core.asyncio, 'sleep', stop)
    await service.run_worker()
    assert service.watermarks[job]['watermark'] == 110
    assert not service.busy and not service.failures and not service.waiting
    assert json.loads(service.watermark_path.read_text())[job]['watermark'] == 110
    service.stopping = False
    service.admin_path.write_text('bad')
    await service.run_worker()
    assert service.failures['state'] == 'JSONDecodeError' and not service.busy
