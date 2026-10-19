import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import httpx
import pytest

from monitoring import probes, probe_worker as worker


def event(value):
    return ('data: ' + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) + '\n\n').encode()


def output(field='content'):
    return event({'choices': [{'index': 0, 'delta': {field: '你好'}}]})


FINISH = event({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
DONE = event('[DONE]')


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay = chunks, delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize('body,expected', [
    (output()+FINISH+DONE, 'success'),
    (output('reasoning_content')+FINISH+DONE, 'success'),
    (output('reasoning')+FINISH+DONE, 'success'),
    (output()+FINISH.replace(b'stop', b'length')+DONE, 'success'),
    (FINISH+DONE, 'empty_output'),
    (output()+FINISH, 'incomplete_stream'),
    (output()+DONE, 'incomplete_stream'),
    (output()+FINISH+b'data: [DONE]', 'incomplete_stream'),
    (output()+event({'error': {'message': 'secret'}})+DONE, 'stream_error'),
    (event('{bad}')+DONE, 'invalid_stream'),
    (event([])+DONE, 'invalid_stream'),
    (output()+FINISH+DONE+output(), 'invalid_stream'),
    (output()+FINISH.replace(b'stop', b'error')+DONE, 'invalid_stream'),
])
async def test_protocol(body, expected):
    stream = Chunks([body[i:i+3] for i in range(0, len(body), 3)])
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream; charset=utf-8'}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await worker.probe(client, 'http://model.test/v1/chat/completions')
    assert result['reason'] == expected
    assert stream.closed and len(requests) == 1
    assert json.loads(requests[0].content) == worker.BODY
    assert result['duration_seconds'] >= 0
    if body.startswith(output()):
        assert result['first_output_seconds'] is not None
    assert 'secret' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,expected', [('timeout', 'timeout'), ('http', 'http_error'),
                                         ('connect', 'connection_error'), ('html', 'invalid_stream')])
async def test_failures_are_not_retried(mode, expected):
    calls = []
    stream = Chunks([output(), FINISH, DONE], delay=.05)
    def respond(request):
        calls.append(request)
        if mode == 'connect':
            raise httpx.ConnectError('private host')
        return httpx.Response(503 if mode == 'http' else 200,
                              headers={'Content-Type': 'text/html' if mode == 'html' else 'text/event-stream'}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await worker.probe(client, 'http://model.test/v1/chat/completions', timeout=.01 if mode == 'timeout' else 50)
    assert result['reason'] == expected and len(calls) == 1
    assert 'private' not in json.dumps(result)


def test_sse_comments_crlf_and_size_limit():
    stream = worker.Stream()
    for byte in (b': heartbeat\r\n\r\n'+output()+FINISH+DONE).replace(b'\n', b'\r\n'):
        stream.feed(bytes([byte]))
    stream.finish()
    with pytest.raises(worker.ProbeFailure, match='invalid_stream'):
        worker.Stream().feed(b'x' * (worker.MAX_BYTES+1))


@pytest.mark.parametrize('newline', [b'\n', b'\r\n', b'\r'])
@pytest.mark.parametrize('chunk_size', [1, 3, 65536])
@pytest.mark.parametrize('prefix', [b'', b'\xef\xbb\xbf'])
def test_complete_sse_line_endings_and_bom(newline, chunk_size, prefix):
    payload = prefix + (output()+FINISH+DONE).replace(b'\n', newline)
    stream = worker.Stream()
    for start in range(0, len(payload), chunk_size):
        stream.feed(payload[start:start+chunk_size])
        stream.feed(b'')
    stream.finish()
    assert stream.output and stream.finished and stream.done


@pytest.mark.parametrize('newline', [b'\n', b'\r\n', b'\r'])
def test_sse_terminal_data_line_still_requires_an_empty_line(newline):
    stream = worker.Stream()
    stream.feed((output()+FINISH+b'data: [DONE]\n').replace(b'\n', newline))
    with pytest.raises(worker.ProbeFailure, match='incomplete_stream'):
        stream.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize('api_key', ['', 'synthetic-business-key'])
async def test_cycle_sends_business_credentials_only_to_model(api_key):
    calls = []
    def respond(request):
        calls.append(request)
        if request.url.host == 'model.test':
            assert request.headers.get('Authorization') == ('Bearer ' + api_key if api_key else None)
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  content=output()+FINISH+DONE)
        assert request.url.host == 'vm.test'
        assert 'Authorization' not in request.headers
        assert b'synthetic-business-key' not in request.content
        return httpx.Response(204)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await worker.cycle(client, 'http://vm.test', 'a3-vllm',
                                    'http://model.test/v1/chat/completions', api_key=api_key)
    assert result['reason'] == 'success' and len(calls) == 2
    assert 'synthetic-business-key' not in json.dumps(result)


def observation(now, reason='success'):
    return {'reason': reason, 'started_at': now-2, 'finished_at': now, 'duration_seconds': 2,
            'first_output_seconds': .1, 'http_status': 200}


def test_raw_observations_gaps_staleness_and_partial_writes():
    now = 100000
    raw = probes.samples('a3-vllm', observation(now-121)) + probes.samples('dcu-pd', observation(now-1, 'timeout'))
    partial = probes.samples('xpu-pd', observation(now-1)).splitlines()[0]
    data = probes.decode(raw+partial+'\n'+probes.samples('ppu', observation(now-1)), now-86400, now)
    a3, xpu, dcu = data['services']
    assert [service['offset_seconds'] for service in data['services']] == [0, 20, 40]
    assert a3['status'] == 'unknown' and a3['latest']['status'] == 'success'
    assert xpu['status'] == 'unknown' and xpu['latest']['reason'] == 'incomplete_observation'
    assert dcu['status'] == 'failure'
    assert len(a3['records']) == 1  # No forward filling or generated history.
    assert probes.decode('', now-86400, now)['services'][0]['latest'] is None


def test_conflicting_or_invalid_observations_are_unknown():
    value = observation(100000)
    raw = probes.samples('a3-vllm', value)
    conflicting = probes.samples('a3-vllm', {**value, 'reason': 'timeout'})
    assert probes.decode(raw+conflicting, 90000, 100000)['services'][0]['status'] == 'unknown'
    assert probes.decode(raw.replace('"values": [0]', '"values": [99]'), 90000, 100000)['services'][0]['status'] == 'unknown'


def test_schedule_skips_missed_slots_and_staggers():
    assert [worker.next_run(900, offset) for offset in probes.OFFSETS.values()] == [960, 920, 940]
    assert worker.next_run(1200, 0) == 1260
    assert worker.next_run(9000, 0) == 9060
    for offset in probes.OFFSETS.values():
        assert worker.next_run(900+offset, offset) == 960+offset
        assert worker.next_run(900+offset-.1, offset) == 900+offset
        assert worker.next_run(913+offset, offset) == 960+offset


def test_probe_contract_and_freshness_boundary():
    raw = probes.samples('a3-vllm', observation(1000))
    data = probes.decode(raw, 1, 1120)
    assert data['interval_seconds'] == 60
    assert data['stale_after_seconds'] == 120
    assert data['services'][0]['status'] == 'success'
    expired = probes.decode(raw, 1, 1121)['services'][0]
    assert expired['status'] == 'unknown' and expired['latest']['status'] == 'success'


@pytest.mark.asyncio
async def test_default_request_deadline_is_fifty_seconds_and_closes_stream(monkeypatch):
    timeout = asyncio.timeout
    deadlines = []
    def accelerated(seconds):
        deadlines.append(seconds)
        return timeout(.01)
    monkeypatch.setattr(worker.asyncio, 'timeout', accelerated)
    stream = Chunks([output(), FINISH, DONE], delay=.05)
    def respond(request):
        assert request.extensions['timeout'] == {'connect': 10, 'read': 50, 'write': 50, 'pool': 50}
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await worker.probe(client, 'http://model.test/v1/chat/completions')
    assert deadlines == [50]
    assert result['reason'] == 'timeout' and stream.closed


@pytest.mark.asyncio
async def test_import_has_a_total_deadline_even_with_continuous_response_bytes(monkeypatch):
    timeout = asyncio.timeout
    deadlines = []
    def accelerated(seconds):
        deadlines.append(seconds)
        return timeout(.025)
    monkeypatch.setattr(worker.asyncio, 'timeout', accelerated)
    generate = AsyncMock(return_value=observation(time.time()))
    monkeypatch.setattr(worker, 'probe', generate)
    stream = Chunks([b' ']*100, delay=.005)
    requests = []
    def respond(request):
        requests.append(request)
        assert request.extensions['timeout']['read'] == 5
        return httpx.Response(200, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(TimeoutError):
            await worker.cycle(client, 'http://vm.test', 'a3-vllm', 'http://model.test/v1/chat/completions')
    assert deadlines == [5]
    assert generate.await_count == len(requests) == 1
    assert stream.closed


@pytest.mark.asyncio
async def test_suspended_scheduler_skips_elapsed_slot_without_burst(monkeypatch):
    clock = [901.]
    monkeypatch.setattr(worker.time, 'time', lambda: clock[0])
    waits = []
    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds + (6 if len(waits) == 1 else 0)
    monkeypatch.setattr(worker.asyncio, 'sleep', sleep)
    calls = []
    async def cycle(*args, **kwargs):
        calls.append(clock[0])
        raise asyncio.CancelledError()
    monkeypatch.setattr(worker, 'cycle', cycle)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_target(None, 'http://vm.test', 'a3-vllm', 'http://model.test', 0)
    assert waits == [59, 54]
    assert calls == [1020]


@pytest.mark.asyncio
async def test_failed_import_does_not_repeat_generation(monkeypatch):
    generate = AsyncMock(return_value=observation(time.time()))
    monkeypatch.setattr(worker, 'probe', generate)
    client = SimpleNamespace(post=AsyncMock(side_effect=httpx.ReadTimeout('lost import response')))
    with pytest.raises(httpx.ReadTimeout):
        await worker.cycle(client, 'http://vm.test', 'a3-vllm', 'http://model.test/v1/chat/completions')
    assert generate.await_count == client.post.await_count == 1


@pytest.mark.asyncio
async def test_read_api_never_generates_and_uses_raw_export(monkeypatch):
    from monitoring import api
    calls = []
    def respond(request):
        calls.append(request)
        assert request.method == 'GET' and request.url.path == '/api/v1/export'
        assert 'environment=~"a3-vllm|xpu-pd|dcu-pd"' in request.url.params['match[]']
        return httpx.Response(200, text=probes.samples('dcu-pd', observation(time.time()-1)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services={
            'dcu-pd': SimpleNamespace(client=client, slots=asyncio.Semaphore(2))})))
        monkeypatch.setattr(worker, 'probe', AsyncMock(side_effect=AssertionError('must stay read-only')))
        data = await probes.probe_history(request)
        assert data['services'][2]['status'] == 'success' and len(calls) == 1


@pytest.mark.asyncio
async def test_read_api_storage_failure(monkeypatch):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services={
        'dcu-pd': SimpleNamespace(client=None, slots=asyncio.Semaphore(2))})))
    monkeypatch.setattr(probes, 'history', AsyncMock(side_effect=httpx.ConnectError('offline')))
    with pytest.raises(HTTPException) as error:
        await probes.probe_history(request)
    assert error.value.status_code == 503


def test_configuration_validates_full_gateway_urls(tmp_path):
    path = tmp_path/'targets.json'
    targets = {env: f'http://{name.lower()}.test/v1/chat/completions' for env, name in probes.TARGETS}
    path.write_text(json.dumps(targets))
    assert worker.load_targets(path) == {env: worker.Target(url) for env, url in targets.items()}
    for invalid in ({key: value for key, value in targets.items() if key != 'dcu-pd'},
                    {**targets, 'ppu': 'http://ppu.test/v1/chat/completions'},
                    {**targets, 'unknown': 'http://unknown.test/v1/chat/completions'}):
        path.write_text(json.dumps(invalid))
        with pytest.raises(ValueError, match='Configure exactly'):
            worker.load_targets(path)
    for bad in ('http://test/health', 'http://secret:password@test/v1/chat/completions', 'file:///v1/chat/completions'):
        path.write_text(json.dumps({**targets, 'a3-vllm': bad}))
        with pytest.raises(ValueError):
            worker.load_targets(path)


def test_configuration_accepts_optional_keys_without_exposing_them(tmp_path):
    path = tmp_path/'targets.json'
    targets = {env: f'http://{name.lower()}.test/v1/chat/completions' for env, name in probes.TARGETS}
    configured = {'url': targets['a3-vllm'], 'api_key': 'synthetic-business-key'}
    path.write_text(json.dumps({**targets, 'a3-vllm': configured}))
    loaded = worker.load_targets(path)
    assert loaded['a3-vllm'] == worker.Target(configured['url'], configured['api_key'])
    assert loaded['xpu-pd'].api_key == ''
    assert configured['api_key'] not in repr(loaded)
    for bad in (None, 123, 'synthetic\nsecret', 'synthetic\rsecret', 'synthetic secret', '密钥', 'x' * 8193):
        path.write_text(json.dumps({**targets, 'a3-vllm': {**configured, 'api_key': bad}}))
        with pytest.raises(ValueError, match='Probe API keys'):
            worker.load_targets(path)


@pytest.mark.asyncio
async def test_single_worker_lock_and_shutdown_do_not_record_cancellations(tmp_path, monkeypatch):
    path = tmp_path/'targets.json'
    path.write_text(json.dumps({env: {'url': f'http://{name.lower()}.test/v1/chat/completions',
                                   'api_key': 'synthetic-' + env} for env, name in probes.TARGETS}))
    monkeypatch.setenv('PROBE_TARGETS_FILE', str(path))
    monkeypatch.setenv('STATE_DIR', str(tmp_path))
    started = []
    async def blocked(*args, api_key):
        started.append(args[2])
        assert args[3] == f'http://{dict(probes.TARGETS)[args[2]].lower()}.test/v1/chat/completions'
        assert args[4] == probes.OFFSETS[args[2]]
        assert api_key == 'synthetic-' + args[2]
        await asyncio.Event().wait()
    monkeypatch.setattr(worker, 'run_target', blocked)
    app = SimpleNamespace(state=SimpleNamespace())
    async with worker.lifespan(app):
        await asyncio.sleep(0)
        assert set(started) == {'a3-vllm', 'xpu-pd', 'dcu-pd'}
        assert 'ppu' not in started
        assert all(not task.done() for task in app.state.tasks)
        with pytest.raises(BlockingIOError):
            async with worker.lifespan(SimpleNamespace(state=SimpleNamespace())):
                raise AssertionError('duplicate worker started')
    assert all(task.cancelled() for task in app.state.tasks)
    # Shutdown releases the singleton lock; no stale lease survives a restart.
    async with worker.lifespan(app):
        pass


@pytest.mark.asyncio
async def test_scheduler_is_serial_even_when_generation_takes_time(monkeypatch):
    active, peak, calls = 0, 0, 0
    async def delayed(*args, api_key):
        nonlocal active, peak, calls
        assert api_key == 'synthetic-key'
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.01)
        active -= 1
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr(worker, 'cycle', delayed)
    monkeypatch.setattr(worker, 'next_run', lambda now, offset: now+.001)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_target(None, 'http://vm.test', 'a3-vllm', 'http://model.test', 0, api_key='synthetic-key')
    assert calls == 2 and peak == 1
