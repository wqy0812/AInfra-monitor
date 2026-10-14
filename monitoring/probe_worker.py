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


async def probe(client, url, *, api_key='', timeout=TIMEOUT):
    started, clock = time.time(), time.monotonic()
    status, reason = None, 'success'
    stream = Stream()
    headers = {'Accept': 'text/event-stream'}
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    try:
        async with asyncio.timeout(timeout):
            async with client.stream('POST', url, json=BODY, headers=headers,
                                     timeout=httpx.Timeout(timeout, connect=min(10, timeout))) as response:
                status = response.status_code
                if not 200 <= status < 300:
                    raise ProbeFailure('http_error')
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
            'http_status': status, 'reason': reason}


@dataclass(frozen=True)
class Target:
    url: str
    api_key: str = field(default='', repr=False)


def load_targets(path):
    values = json.loads(Path(path).read_text())
    if not isinstance(values, dict) or set(values) != {env for env, _ in TARGETS}:
        raise ValueError('Configure exactly a3-vllm, xpu-pd and dcu-pd business endpoints')
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


@asynccontextmanager
async def lifespan(app):
    targets = load_targets(os.environ['PROBE_TARGETS_FILE'])
    vm = os.environ.get('VM_URL', 'http://127.0.0.1:18428').rstrip('/')
    # Share this existing state directory across restarts and all worker instances.
    with open(Path(os.environ.get('STATE_DIR', '/state')) / 'probes.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                    limits=httpx.Limits(max_connections=6)) as client:
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
    try:
        response = await app.state.client.get(app.state.vm + '/health', timeout=2)
        response.raise_for_status()
        ready = all(not task.done() for task in app.state.tasks)
    except httpx.HTTPError:
        ready = False
    return JSONResponse({'status': 'ok' if ready else 'unavailable', 'targets': len(TARGETS)},
                        status_code=200 if ready else 503)
