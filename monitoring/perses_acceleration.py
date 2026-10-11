"""Read-only Perses query adapter: exact matching, validated cache and raw fallback.

Native recording production lives in vmalert. This module never writes metrics.
"""
import asyncio
import base64
import hashlib
import itertools
import json
import math
import re
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

MAX_POINTS = 10000
SEAL_SECONDS = 60
MAX_GAPS = 4
RESPONSE_CACHE_SECONDS = 30
RESPONSE_CACHE_BYTES = 16 * 1024 * 1024
RESPONSE_CACHE_ITEMS = 128
FAST_PARAMETERS = {'query', 'start', 'end', 'step', 'nocache', 'latency_offset'}
READ_ENDPOINT = re.compile(r'api/v1/(?:query|query_range|labels|series|metadata|label/[a-zA-Z0-9_-]+/values)\Z')

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True) + '\n')
    temporary.replace(path)

def number(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('Non-finite timestamp')
    return value

def duration(text):
    try:
        return number(text)
    except ValueError:
        match = re.fullmatch(r'(\d+(?:\.\d+)?)(ms|s|m|h)', text)
        if not match:
            raise ValueError('Unsupported duration')
        return float(match[1]) * {'ms': .001, 's': 1, 'm': 60, 'h': 3600}[match[2]]

def interval_spellings(step):
    values = {str(step) + 's'}
    if step % 60 == 0:
        values.add(str(step // 60) + 'm')
    if step % 3600 == 0:
        values.add(str(step // 3600) + 'h')
    return values

def metric_identity(labels):
    return base64.urlsafe_b64encode(json.dumps(labels, sort_keys=True, separators=(',', ':')).encode()).decode()

def decode_identity(identity):
    labels = json.loads(base64.urlsafe_b64decode(identity))
    if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
        raise ValueError('Invalid stored labels')
    return labels

def points_by_time(rows, ticks):
    allowed = set(ticks)
    values = {timestamp: {} for timestamp in ticks}
    for row in rows:
        identity = metric_identity(row['metric'])
        for timestamp, value in row['values']:
            if timestamp not in allowed or identity in values[timestamp]:
                raise ValueError('Unexpected timestamp or duplicate source result')
            numeric = float(value)
            if math.isnan(numeric):
                raise ValueError('NaN is not a stored sample')
            values[timestamp][identity] = str(value)
    return values

def equal_values(left, right):
    return left.keys() == right.keys() and all(
        a == right[key] or math.isclose(float(a), float(right[key]), rel_tol=1e-9, abs_tol=1e-10)
        for key, a in left.items())

def result_rows(values, bindings=None):
    rows, labels_by_identity = {}, {}
    for timestamp, samples in sorted(values.items()):
        for identity, value in samples.items():
            if identity not in labels_by_identity:
                labels = decode_identity(identity)
                accepted = not bindings or all(re.fullmatch(pattern, labels.get('node', '')) is not None for pattern in bindings.values())
                labels_by_identity[identity] = labels if accepted else None
            labels = labels_by_identity[identity]
            if labels is None:
                continue
            rows.setdefault(identity, {'metric': labels, 'values': []})['values'].append([timestamp, value])
    def order(identity):
        labels = rows[identity]['metric']
        # These frozen histogram_quantiles expressions append the quantile tag
        # after their original grouping tags. Match VM's native series order so
        # rank/engine colours do not change when the datasource is switched.
        return tuple(sorted((k, v) for k, v in labels.items() if k != 'perses_quantile')) + (
            (('perses_quantile', labels['perses_quantile']),) if 'perses_quantile' in labels else ())
    return [rows[key] for key in sorted(rows, key=order)]

class AccelerationService:
    def __init__(self, vm, state, catalog, *, client=None, now=time.time, source_ready=None):
        self.vm = vm.rstrip('/')
        self.state = Path(state)
        self.catalog = catalog
        self.now = now
        self.source_ready = source_ready or (lambda: True)
        self.client = client or httpx.AsyncClient(trust_env=False, timeout=45,
            limits=httpx.Limits(max_connections=6))
        self.owns_client = client is None
        self.steps = tuple(catalog['steps'])
        if catalog['schema'] != 1 or not self.steps or any(not isinstance(s, int) or s < 5 or s % 5 for s in self.steps):
            raise ValueError('Invalid acceleration catalog')
        self.panels = {p['id']: p for p in catalog['panels']}
        if len(self.panels) != len(catalog['panels']):
            raise ValueError('Duplicate catalog panel')
        self.matches = {}
        for panel in self.panels.values():
            keys = sorted(panel['variables'])
            for values in itertools.product(*(panel['variables'][key] for key in keys)):
                bindings = dict(zip(keys, values))
                for step in self.steps:
                    for spelling in interval_spellings(step):
                        expression = self.expression(panel, step, bindings, spelling)
                        key = (hashlib.sha256(expression.encode()).hexdigest(), step)
                        self.matches[key] = (panel, bindings)
        self.admin_path = self.state / 'perses-acceleration-admin.json'
        self.inflight = {}
        self.responses = OrderedDict()
        self.response_bytes = 0
        self.response_admin = None
        self.online = 0
        self.slots = asyncio.Semaphore(2)
        self.stopping = False
        self.counters = {'fast_requests': 0, 'raw_requests': 0, 'shared_requests': 0, 'response_cache_hits': 0}

    @staticmethod
    def expression(panel, step, bindings=None, spelling=None):
        expression = panel['expression'].replace('$__interval', spelling or str(step) + 's')
        for name in panel['variables']:
            expression = expression.replace('$' + name, (bindings or {}).get(name, '.*'))
        return expression

    def job_key(self, panel, step):
        return panel['id'] + ':' + panel['revision'] + ':' + str(step)

    def admin(self):
        if not self.admin_path.exists():
            return {'disabled_groups': [], 'invalidated': []}
        value = json.loads(self.admin_path.read_text())
        if not isinstance(value.get('disabled_groups'), list) or not isinstance(value.get('invalidated'), list):
            raise ValueError('Invalid acceleration administrative state')
        return value

    def permitted(self, panel, timestamp, admin):
        if panel['group'] in admin['disabled_groups']:
            return False
        return not any(item['start'] <= timestamp <= item['end']
                       and item.get('panel', '*') in ('*', panel['id']) for item in admin['invalidated'])

    async def vm_query(self, expression, start, end, step, *, timeout=45, nocache=True):
        response = await self.client.post(self.vm + '/api/v1/query_range', data={
            'query': expression, 'start': start, 'end': end, 'step': step,
            'nocache': '1' if nocache else '0', 'latency_offset': '1ms', 'timeout': str(timeout) + 's'}, timeout=timeout + 1)
        response.raise_for_status()
        body = response.json()
        if (body.get('status') != 'success' or body.get('data', {}).get('resultType') != 'matrix'
                or body.get('isPartial') or body.get('warnings')):
            raise ValueError('Unexpected VM result')
        return body['data']['result']

    async def forward(self, method, endpoint, pairs):
        kwargs = {'params': pairs} if method == 'GET' else {'content': httpx.QueryParams(pairs).__str__().encode(),
                                                          'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}
        async with self.slots:
            response = await self.client.request(method, self.vm + '/' + endpoint, **kwargs)
        return response.status_code, response.content, response.headers.get('content-type', 'application/json')

    async def grid(self, method, pairs):
        params = [(key, 'vector(time())' if key == 'query' else value) for key, value in pairs]
        status, body, _ = await self.forward(method, 'api/v1/query_range', params)
        if status != 200:
            raise ValueError('Could not resolve original VM time grid')
        result = json.loads(body)['data']['result']
        if len(result) != 1:
            raise ValueError('Unexpected time grid')
        return [timestamp for timestamp, value in result[0]['values']]

    def cached_response(self, key, admin):
        if self.response_admin != admin:
            self.responses.clear(); self.response_bytes = 0; self.response_admin = admin
        entry = self.responses.get(key)
        if entry is None:
            return None
        expires, response = entry
        if expires <= self.now():
            self.responses.pop(key); self.response_bytes -= len(response[1])
            return None
        self.responses.move_to_end(key)
        self.counters['response_cache_hits'] += 1
        self.counters['fast_requests'] += 1
        return response

    def remember_response(self, key, admin, response):
        # Only complete sealed windows call this method. An invalidation that
        # arrived during the query must not allow its old result into the cache.
        if admin != digest(self.admin()) or len(response[1]) > RESPONSE_CACHE_BYTES:
            return
        if self.response_admin != admin:
            self.responses.clear(); self.response_bytes = 0; self.response_admin = admin
        previous = self.responses.pop(key, None)
        if previous: self.response_bytes -= len(previous[1][1])
        self.responses[key] = (self.now() + RESPONSE_CACHE_SECONDS, response)
        self.response_bytes += len(response[1])
        while len(self.responses) > RESPONSE_CACHE_ITEMS or self.response_bytes > RESPONSE_CACHE_BYTES:
            _, (_, removed) = self.responses.popitem(last=False)
            self.response_bytes -= len(removed[1])

    async def accelerated(self, method, endpoint, pairs):
        params = dict(pairs)
        match = None
        try:
            step_value = duration(params.get('step', ''))
            step = int(step_value)
            if endpoint == 'api/v1/query_range' and step == step_value and len(params) == len(pairs) and set(params) <= FAST_PARAMETERS:
                match = self.matches.get((hashlib.sha256(params.get('query', '').encode()).hexdigest(), step))
            start, end = number(params.get('start', '')), number(params.get('end', ''))
            if start > end or step_value <= 0 or (end - start) / step_value + 1 > MAX_POINTS:
                match = None
            if 'latency_offset' in params and not 0 < duration(params['latency_offset']) <= SEAL_SECONDS:
                match = None
        except (ValueError, OverflowError):
            match = None
        if match is None:
            self.counters['raw_requests'] += 1
            return await self.forward(method, endpoint, pairs)
        panel, bindings = match
        admin = self.admin()
        if panel['group'] in admin['disabled_groups']:
            return await self.forward(method, endpoint, pairs)
        cache_key = (method, endpoint, tuple(pairs))
        admin_version = digest(admin)
        allow_response_cache = params.get('nocache') in (None, '0') and end <= self.now() - SEAL_SECONDS
        if allow_response_cache:
            response = self.cached_response(cache_key, admin_version)
            if response is not None: return response
        # Aligned sealed history has the same grid in both VM cache modes.
        # Nonintegral or moving-tail requests still ask VM to resolve its grid.
        if start % step == end % step == 0 and end <= self.now() - SEAL_SECONDS:
            ticks = list(range(int(start), int(end) + 1, step))
        else:
            ticks = await self.grid(method, pairs)
        if not ticks or any(t % step != 0 for t in ticks) or any(b - a != step for a, b in zip(ticks, ticks[1:])):
            return await self.forward(method, endpoint, pairs)
        # Publication/read-back always bypasses cache. Online reads can use VM's
        # cache when requested; current invalidations are applied below regardless.
        cached = await self.stored(panel, step, ticks, nocache=params.get('nocache') not in (None, '0'))
        cached = {t: samples for t, samples in cached.items()
                  if t <= self.now() - SEAL_SECONDS and self.permitted(panel, t, admin)}
        if not cached:
            self.counters['raw_requests'] += 1
            return await self.forward(method, endpoint, pairs)
        gaps = []
        for timestamp in ticks:
            if timestamp in cached:
                continue
            if gaps and timestamp == gaps[-1][1] + step:
                gaps[-1][1] = timestamp
            else:
                gaps.append([timestamp, timestamp])
        if len(gaps) > MAX_GAPS:
            return await self.forward(method, endpoint, pairs)
        if not gaps:
            response = {'status': 'success', 'data': {'resultType': 'matrix', 'result': result_rows(cached, bindings)}}
            self.counters['fast_requests'] += 1
            result = (200, json.dumps(response, separators=(',', ':')).encode(), 'application/json')
            if allow_response_cache: self.remember_response(cache_key, admin_version, result)
            return result
        # VM owns the grid, including nocache and sub-second behavior. All raw
        # pieces use exactly that grid; they do not evaluate the full old range.
        values = points_by_time(result_rows(cached, bindings), list(cached))
        warnings, infos = [], []
        for left, right in gaps:
            piece = dict(params, start=str(left), end=str(right))
            status, body, _ = await self.forward(method, endpoint, list(piece.items()))
            if status != 200:
                return status, body, 'application/json'
            result = json.loads(body)
            part_ticks = [t for t in ticks if left <= t <= right]
            values.update(points_by_time(result['data']['result'], part_ticks))
            warnings.extend(result.get('warnings', [])); infos.extend(result.get('infos', []))
        response = {'status': 'success', 'data': {'resultType': 'matrix', 'result': result_rows(values)}}
        if warnings:
            response['warnings'] = list(dict.fromkeys(warnings))
        if infos:
            response['infos'] = list(dict.fromkeys(infos))
        self.counters['fast_requests'] += 1
        return 200, json.dumps(response, separators=(',', ':')).encode(), 'application/json'

    async def execute(self, method, endpoint, pairs):
        self.online += 1
        try:
            try:
                return await self.accelerated(method, endpoint, pairs)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError, OSError):
                self.counters['raw_requests'] += 1
                return await self.forward(method, endpoint, pairs)
        finally:
            self.online -= 1

    async def request(self, method, endpoint, pairs):
        try:
            admin_version = digest(self.admin())
        except (OSError, ValueError, TypeError):
            admin_version = None
        key = (method, endpoint, tuple(pairs), admin_version)
        task = self.inflight.get(key)
        if task is None:
            task = asyncio.create_task(self.execute(method, endpoint, pairs))
            self.inflight[key] = task
            def finished(done):
                if self.inflight.get(key) is done:
                    self.inflight.pop(key, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
        else:
            self.counters['shared_requests'] += 1
        return await asyncio.shield(task)

    async def close(self):
        self.stopping = True
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.owns_client:
            await self.client.aclose()

def install_routes(app):
    @app.api_route('/internal/perses/{endpoint:path}', methods=['GET', 'POST'])
    async def proxy(endpoint: str, request: Request):
        service = getattr(request.app.state, 'perses_acceleration', None)
        if service is None:
            return JSONResponse({'status': 'error', 'error': 'Acceleration is not configured'}, status_code=503)
        if not READ_ENDPOINT.fullmatch(endpoint):
            return JSONResponse({'status': 'error', 'error': 'Endpoint is not allowed'}, status_code=403)
        pairs = list(request.query_params.multi_items())
        if request.method == 'POST':
            if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                return JSONResponse({'status': 'error', 'error': 'Expected form query'}, status_code=415)
            body = await request.body()
            if len(body) > 1024 * 1024:
                return JSONResponse({'status': 'error', 'error': 'Query too large'}, status_code=413)
            # Go's ParseForm gives body parameters precedence over URL values.
            pairs = parse_qsl(body.decode(), keep_blank_values=True) + pairs
        try:
            status, body, content_type = await service.request(request.method, endpoint, pairs)
            return Response(body, status_code=status, media_type=content_type)
        except httpx.HTTPError:
            return JSONResponse({'status': 'error', 'error': 'Metrics query unavailable'}, status_code=503)
