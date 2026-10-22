"""Single-instance, staggered real SSE probes; the monitoring API stays read-only."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx

from .probes import INTERVAL, OFFSETS, TARGETS, samples

LOG = logging.getLogger(__name__)
TIMEOUT = 50
IMPORT_TIMEOUT = 5
MAX_BYTES = 1024 * 1024
BODY = {'messages': [{'role': 'user', 'content': '请简短地回复：你好。'}],
        'stream': True, 'max_tokens': 64, 'n': 1}


class ProbeFailure(Exception):
    pass


class Stream:
    def __init__(self):
        self.buffer = b''
        self.data = []
        self.size = 0
        self.output = False
        self.first_output_at = None
        self.finished = False
        self.done = False
        self.skip_lf = False
        self.first_line = True

    def event(self):
        if not self.data:
            return
        payload = '\n'.join(self.data)
        self.data.clear()
        if self.done:
            raise ProbeFailure('invalid_stream')
        if payload.strip() == '[DONE]':
            if not self.finished:
                raise ProbeFailure('incomplete_stream')
            self.done = True
            return
        try:
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError()
            if value.get('error') is not None:
                raise ProbeFailure('stream_error')
            choices = value.get('choices', [])
            if not isinstance(choices, list):
                raise ValueError()
            for choice in choices:
                if not isinstance(choice, dict) or choice.get('index', 0) != 0:
                    raise ValueError()
                delta = choice.get('delta', {})
                if not isinstance(delta, dict):
                    raise ValueError()
                for field in ('content', 'reasoning_content', 'reasoning'):
                    text = delta.get(field)
                    if text is not None and not isinstance(text, str):
                        raise ValueError()
                    if text:
                        if self.finished:
                            raise ValueError()
                        self.output = True
                        if self.first_output_at is None:
                            self.first_output_at = time.monotonic()
                finish = choice.get('finish_reason')
                if finish is not None:
                    if finish not in ('stop', 'length'):
                        raise ProbeFailure('invalid_stream')
                    self.finished = True
        except (ValueError, TypeError, KeyError):
            raise ProbeFailure('invalid_stream') from None

    def feed(self, chunk):
        self.size += len(chunk)
        if self.size > MAX_BYTES:
            raise ProbeFailure('invalid_stream')
        if self.skip_lf and chunk:
            self.skip_lf = False
            chunk = chunk.removeprefix(b'\n')
        self.buffer += chunk
        while match := re.search(rb'\r\n|\r|\n', self.buffer):
            line, self.buffer = self.buffer[:match.start()], self.buffer[match.end():]
            # CR already ends the line; only suppress a following LF across chunks.
            self.skip_lf = match.group() == b'\r' and not self.buffer
            try:
                line = line.decode('utf-8-sig' if self.first_line else 'utf-8')
            except UnicodeDecodeError:
                raise ProbeFailure('invalid_stream') from None
            self.first_line = False
            if not line:
                self.event()
            elif line.startswith('data:'):
                self.data.append(line[5:].removeprefix(' '))

    def finish(self):
        # An unterminated data event is not a complete SSE response.
        if self.buffer.strip() or self.data or not self.done:
            raise ProbeFailure('incomplete_stream')
        if not self.output:
            raise ProbeFailure('empty_output')


async def probe(client, url, *, api_key='', timeout=TIMEOUT, expected_router=''):
    started, clock = time.time(), time.monotonic()
    status, reason = None, 'success'
    stream = Stream()
    headers = {'Accept': 'text/event-stream'}
    actual_router = None
    if expected_router:
        headers['X-Aigate-Expected-Router'] = expected_router
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    try:
        async with asyncio.timeout(timeout):
            async with client.stream('POST', url, json=BODY, headers=headers,
                                     timeout=httpx.Timeout(timeout, connect=min(10, timeout))) as response:
                status = response.status_code
                actual_router = response.headers.get('X-Aigate-Backend')
                if actual_router and (len(actual_router) > 64 or not re.fullmatch(r'[A-Za-z0-9_-]+', actual_router)):
                    actual_router = None
                if not 200 <= status < 300:
                    raise ProbeFailure('http_error')
                if expected_router and actual_router != expected_router:
                    raise ProbeFailure('identity_mismatch')
                if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/event-stream':
                    raise ProbeFailure('invalid_stream')
                async for chunk in response.aiter_bytes():
                    stream.feed(chunk)
                stream.finish()
    except ProbeFailure as error:
        reason = str(error)
    except (TimeoutError, httpx.TimeoutException):
        reason = 'timeout'
    except httpx.HTTPError:
        reason = 'connection_error'
    return {'started_at': started, 'finished_at': time.time(), 'duration_seconds': time.monotonic()-clock,
            'first_output_seconds': stream.first_output_at-clock if stream.first_output_at is not None else None,
            'http_status': status, 'reason': reason, 'actual_router_id': actual_router}


@dataclass(frozen=True)
class Target:
    url: str
    api_key: str = field(default='', repr=False)


def load_targets(path):
    values = json.loads(Path(path).read_text())
    if not isinstance(values, dict) or set(values) != {env for env, _ in TARGETS}:
        raise ValueError('Configure exactly these business endpoints: ' + ', '.join(env for env, _ in TARGETS))
    targets = {}
    for environment, value in values.items():
        if isinstance(value, str):
            url, api_key = value, ''
        elif isinstance(value, dict) and 'url' in value and not set(value) - {'url', 'api_key'}:
            url, api_key = value['url'], value.get('api_key', '')
        else:
            raise ValueError('Probe targets must be URLs or objects with url and optional api_key')
        if not isinstance(url, str):
            raise ValueError('Probe target URL must be a string')
        if (not isinstance(api_key, str) or len(api_key) > 8192
                or any(not 33 <= ord(char) <= 126 for char in api_key)):
            raise ValueError('Probe API keys must contain only visible ASCII characters, up to 8192 bytes')
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != '/v1/chat/completions'):
            raise ValueError('Probe targets must be full gateway /v1/chat/completions URLs without credentials')
        targets[environment] = Target(url, api_key)
    if len({target.url for target in targets.values()}) != len(TARGETS):
        raise ValueError('Probe targets must be distinct gateway entrances')
    return targets


def next_run(now, offset):
    # Always choose a future slot, including after a restart or a missed period.
    return (math.floor((now-offset)/INTERVAL)+1)*INTERVAL+offset


async def cycle(client, vm, environment, url, *, api_key=''):
    result = await probe(client, url, api_key=api_key)
    # Bound the entire import, including a response that keeps trickling bytes.
    async with asyncio.timeout(IMPORT_TIMEOUT):
        response = await client.post(vm + '/api/v1/import', content=samples(environment, result), timeout=IMPORT_TIMEOUT)
        response.raise_for_status()
    return result


async def run_target(client, vm, environment, url, offset, *, api_key=''):
    while True:
        due = next_run(time.time(), offset)
        await asyncio.sleep(max(0, due-time.time()))
        # A suspended process skips an elapsed slot; it never catches up in bursts.
        if not 0 <= time.time()-due < 5:
            continue
        try:
            await cycle(client, vm, environment, url, api_key=api_key)
        except Exception as error:
            # Failed imports leave gaps. Never repeat a generation after a lost response.
            LOG.error('Probe observation not stored: %s (%s)', environment, type(error).__name__)


class ManagedProbes:
    """One scheduler owns dispatch, reloads and acknowledgements; probes remain serial."""
    def __init__(self, client, vm):
        from . import probe_catalog
        self.client, self.vm = client, vm
        self.targets, self.due, self.running = {}, {}, {}
        self.rounds, self.outcomes, self.coverage = {}, {}, {}
        self.error = None
        self.catalog_revision = None
        self.offset_path = Path(os.environ.get('STATE_DIR', '/state')) / 'probe-offsets.json'
        self.offsets = probe_catalog.read_json(self.offset_path)
        state = probe_catalog.read_json(probe_catalog.state_file('status'))
        self.intervals = state.get('pause_intervals', {})
        # Do not claim a process-down interval was a deliberate pause.
        last = state.get('observed_at', time.time())
        for intervals in self.intervals.values():
            if intervals and intervals[-1].get('end') is None:
                intervals[-1]['end'] = last

    def pause_history(self, identity, target, now):
        intervals = self.intervals.setdefault(identity, [])
        self.intervals[identity] = intervals = [entry for entry in intervals if (entry.get('end') or now) >= now-172800][-4096:]
        reason = target.get('pause_reason', '')
        active = intervals[-1] if intervals and intervals[-1].get('end') is None else None
        if active and active['reason'] != reason:
            active['end'] = now
            active = None
        if reason and not active:
            intervals.append({'start': now, 'end': None, 'reason': reason})

    async def execute(self, target):
        identity = target['probe_id']
        scheduled = target['scheduled_at']
        round_start = math.floor(scheduled / INTERVAL) * INTERVAL
        outcome = None
        try:
            result = await probe(self.client, target['url'], expected_router=target.get('expected_router', ''))
            result.update(scheduled_at=scheduled, source_probe_id=identity,
                          observation_source='switch' if target['probe_kind'] == 'gateway' else 'direct')
            body = samples(target.get('environment', ''), result, target=target)
            shared = []
            if target['probe_kind'] == 'gateway' and result['reason'] == 'success' and result.get('actual_router_id'):
                for foundation in target.get('foundation_snapshots', []):
                    if foundation['router_id'] != result['actual_router_id']:
                        continue
                    shared.append(foundation)
                    body += samples(foundation.get('environment', ''),
                                    result | {'observation_source': 'switch_shared'}, target=foundation)
            async with asyncio.timeout(IMPORT_TIMEOUT):
                response = await self.client.post(self.vm + '/api/v1/import',
                    content=body, timeout=IMPORT_TIMEOUT)
                response.raise_for_status()
            outcome = result
            for foundation in shared:
                key = self.signature(foundation)
                self.coverage[key] = max(round_start, self.coverage.get(key, 0))
        except Exception as error:
            LOG.error('Probe observation not stored: %s (%s)', identity, type(error).__name__)
        finally:
            if target['probe_kind'] == 'gateway':
                self.outcomes[(identity, round_start)] = (self.signature(target), outcome)

    @staticmethod
    def signature(target):
        return target.get('router_id'), target['url']

    def launch(self, target, scheduled):
        snapshot = dict(target, scheduled_at=scheduled)
        if target['probe_kind'] == 'gateway':
            snapshot['foundation_snapshots'] = [dict(f) for f in self.targets.values()
                if f['probe_kind'] == 'foundation' and f['dispatch_status'] == 'active']
        self.running[target['probe_id']] = asyncio.create_task(self.execute(snapshot))

    def resolve_rounds(self, now):
        for key, pending in list(self.rounds.items()):
            identity, minute = key
            target = self.targets.get(identity)
            if (not target or target['dispatch_status'] != 'active'
                    or self.signature(target) != pending['signature']):
                self.rounds.pop(key)
                continue
            if self.coverage.get(self.signature(target), -1) >= minute:
                self.rounds.pop(key)
                continue
            waiting = False
            deadline = pending['scheduled_at']
            for gateway in pending['gateways']:
                # A rebind/removal ends this source's coverage; old requests still
                # finish and retain their actual response identity in history.
                current = self.targets.get(gateway['probe_id'])
                if not current or self.signature(current) != self.signature(gateway) or current['dispatch_status'] != 'active':
                    continue
                source_deadline = minute + gateway['offset_seconds'] + TIMEOUT + IMPORT_TIMEOUT
                deadline = max(deadline, source_deadline)
                observation = self.outcomes.get((gateway['probe_id'], minute))
                if (not observation or observation[0] != self.signature(gateway)) and now < source_deadline:
                    waiting = True
            if waiting:
                continue
            self.rounds.pop(key)
            # No catch-up generation after a suspended process, and no overlap
            # with a previous direct request that is still draining.
            if now - deadline < 5 and identity not in self.running:
                self.launch(target, pending['scheduled_at'])

    async def tick(self, now=None):
        from . import probe_catalog
        now = time.time() if now is None else now
        for identity, task in list(self.running.items()):
            if task.done():
                self.running.pop(identity)
        control = probe_catalog.read_json(probe_catalog.state_file('control'))
        try:
            directory = probe_catalog.catalog()
            targets = probe_catalog.targets(value=directory, control=control, now=now)
            if targets is None:
                raise ValueError('Managed catalog is no longer configured')
            self.catalog_revision = directory['revision']
            self.error = None
        except (OSError, ValueError, TypeError, KeyError):
            self.error = 'catalog_unavailable'
            targets = [target | {'dispatch_status':'paused', 'pause_reason':'catalog_unavailable'}
                       for target in self.targets.values()]
        current = {target['probe_id']:target for target in targets}
        changed_offsets = False
        ready = []
        for identity, target in current.items():
            if identity not in self.offsets:
                self.offsets[identity] = target['offset_seconds']
                changed_offsets = True
            target['offset_seconds'] = self.offsets[identity]
            previous = self.targets.get(identity)
            paused = target['dispatch_status'] != 'active'
            if paused or previous != target or identity not in self.due:
                self.due[identity] = next_run(now, target['offset_seconds'])
            self.pause_history(identity, target, now)
            task = self.running.get(identity)
            if not paused and now >= self.due[identity]:
                due = self.due[identity]
                self.due[identity] = next_run(now, target['offset_seconds'])
                if now-due < 5 and task is None:
                    ready.append((target, due))
        if changed_offsets:
            probe_catalog.atomic_json(self.offset_path, self.offsets)
        for identity in self.targets.keys() - current.keys():
            self.due.pop(identity, None)
            self.pause_history(identity, {'pause_reason':'disabled'}, now)
        self.targets = current
        for target, due in ready:
            if target['probe_kind'] == 'gateway':
                self.launch(target, due)
            else:
                minute = math.floor(due / INTERVAL) * INTERVAL
                gateways = [dict(g) for g in current.values() if g['probe_kind'] == 'gateway'
                            and g['dispatch_status'] == 'active' and g['router_id'] == target['router_id']]
                self.rounds[(target['probe_id'], minute)] = {
                    'scheduled_at': due, 'signature': self.signature(target), 'gateways': gateways}
        self.resolve_rounds(now)
        self.outcomes = {key:value for key,value in self.outcomes.items() if key[1] >= now-180}
        self.coverage = {key:value for key,value in self.coverage.items() if value >= now-180}
        # Written after dispatch decisions. Matching operation IDs prove the
        # pause request has reached the scheduler, even if an old request drains.
        pauses = control.get('paused_routers') or {}
        probe_catalog.atomic_json(probe_catalog.state_file('status'), {
            'observed_at':now, 'control_revision':control.get('revision'),
            'catalog_revision':self.catalog_revision, 'target_ids':sorted(current),
            'paused_routers':sorted(pauses), 'pause_operations':pauses,
            'inflight':{identity:1 for identity, task in self.running.items() if not task.done()},
            'targets':len(current), 'error':self.error, 'pause_intervals':self.intervals,
        })

    async def run(self):
        try:
            while True:
                await self.tick()
                await asyncio.sleep(1)
        finally:
            for task in self.running.values():
                task.cancel()
            await asyncio.gather(*self.running.values(), return_exceptions=True)


@asynccontextmanager
async def lifespan(app):
    managed = bool(os.environ.get('MONITORING_PROBE_CATALOG_FILE') or os.environ.get('MONITORING_CATALOG_FILE'))
    targets = None if managed else load_targets(os.environ['PROBE_TARGETS_FILE'])
    vm = os.environ.get('VM_URL', 'http://127.0.0.1:18428').rstrip('/')
    # Share this existing state directory across restarts and all worker instances.
    with open(Path(os.environ.get('STATE_DIR', '/state')) / 'probes.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                    limits=httpx.Limits(max_connections=6)) as client:
            if managed:
                app.state.managed = ManagedProbes(client, vm)
                tasks = [asyncio.create_task(app.state.managed.run())]
            else:
                app.state.managed = None
                tasks = [asyncio.create_task(run_target(client, vm, env, targets[env].url, OFFSETS[env],
                                                       api_key=targets[env].api_key))
                         for env, _ in TARGETS]
            app.state.tasks, app.state.client, app.state.vm = tasks, client, vm
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/health')
async def health():
    managed = getattr(app.state, 'managed', None)
    try:
        response = await app.state.client.get(app.state.vm + '/health', timeout=2)
        response.raise_for_status()
        ready = all(not task.done() for task in app.state.tasks) and not (managed and managed.error)
    except httpx.HTTPError:
        ready = False
    return JSONResponse({'status': 'ok' if ready else 'unavailable',
                         'targets': len(managed.targets) if managed else len(TARGETS),
                         **({'mode': 'managed', 'catalog_revision': managed.catalog_revision} if managed else {})},
                        status_code=200 if ready else 503)
