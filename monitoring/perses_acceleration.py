"""Versioned, exact-grid Perses results with explicit coverage and raw fallback.

No import-time I/O. The optional worker has its own watermarks and never writes
monitoring_chart_* or the model history state. A completed empty result is not a
cache miss. Unsupported expressions/parameters retain VictoriaMetrics behavior.
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

PREFIX = 'monitoring_perses_'
MAX_POINTS = 10000
SEAL_SECONDS = 60
MAX_GAPS = 4
VISIBILITY_TIMEOUT = 120
RESPONSE_CACHE_SECONDS = 30
RESPONSE_CACHE_BYTES = 16 * 1024 * 1024
RESPONSE_CACHE_ITEMS = 128
FAST_PARAMETERS = {'query', 'start', 'end', 'step', 'nocache', 'latency_offset'}
READ_ENDPOINT = re.compile(r'api/v1/(?:query|query_range|labels|series|metadata|label/[a-zA-Z0-9_-]+/values)\Z')


class PublicationPending(ValueError):
    """An accepted import is not query-visible yet; yield the worker slot."""
    def __init__(self, phase, since):
        super().__init__(phase + ' failed read-back: waiting for visibility')
        self.phase, self.since = phase, since


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
        self.watermark_path = self.state / 'perses-acceleration-watermarks.json'
        self.backfill_path = self.state / 'perses-acceleration-backfill.json'
        self.admin_path = self.state / 'perses-acceleration-admin.json'
        self.watermarks = json.loads(self.watermark_path.read_text()) if self.watermark_path.exists() else {}
        self.backfills = json.loads(self.backfill_path.read_text()) if self.backfill_path.exists() else {}
        self.last_lane = 'backfill'
        self.busy = set()
        self.inflight = {}
        self.responses = OrderedDict()
        self.response_bytes = 0
        self.response_admin = None
        self.online = 0
        self.slots = asyncio.Semaphore(2)
        self.stopping = False
        self.failures = {}
        self.waiting = {}
        self.retry_at = {}
        self.counters = {'fast_requests': 0, 'raw_requests': 0, 'shared_requests': 0,
                         'worker_failures': 0, 'visibility_waits': 0, 'response_cache_hits': 0}

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

    def status(self):
        jobs = []
        for key, state in self.watermarks.items():
            step = int(key.rsplit(':', 1)[1])
            due = int((self.now() - SEAL_SECONDS) // step) * step
            history = self.backfills.get(key)
            coverage_start = history['start'] if history and history['watermark'] == history['end'] else state['start']
            jobs.append({'job': key, 'started_at': state['start'], 'coverage_start': coverage_start,
                         'processed_at': state['watermark'],
                         'lag_seconds': max(0, due - state['watermark']) if due >= state['start'] else 0,
                         'error': self.failures.get(key), 'waiting': self.waiting.get(key),
                         'backfill': dict(history, error=self.failures.get('backfill:' + key),
                                          waiting=self.waiting.get('backfill:' + key)) if history else None})
        try:
            admin = self.admin()
            state_error = self.failures.get('state')
        except (OSError, ValueError) as error:
            admin = {}; state_error = type(error).__name__
        return {'enabled': True, 'groups': self.catalog['groups'], 'seal_seconds': SEAL_SECONDS,
                'steps': self.steps, 'jobs': jobs, 'counters': dict(self.counters),
                'parallel_a3_backfill': bool(admin.get('parallel_a3_backfill')),
                'backfill_priority_active': self.backfill_priority(admin),
                'backfill_priority_until': admin.get('backfill_priority_until'),
                'active_batches': sorted(self.busy), 'online_queries': self.online,
                'disabled_groups': admin.get('disabled_groups'), 'state_error': state_error}

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

    def selector(self, panel, step, generation=None, kind=None):
        tags = {'query_id': panel['id'], 'revision': panel['revision'], 'step': str(step)}
        if generation is not None:
            tags['generation'] = generation
        labels = ','.join(key + '=' + json.dumps(value) for key, value in tags.items())
        if kind:
            return PREFIX + kind + '{' + labels + '}'
        return '{__name__=~"monitoring_perses_(value|complete)",' + labels + '}'

    async def stored(self, panel, step, ticks, *, nocache=True):
        selector = self.selector(panel, step)
        expression = selector + ' and (timestamp(' + selector + ') == time())'
        rows = await self.vm_query(expression, ticks[0], ticks[-1], step, nocache=nocache)
        expected, actual = {}, {}
        allowed = set(ticks)
        for row in rows:
            tags = row['metric']
            generation = tags['generation']
            kind = tags['__name__']
            if kind != PREFIX + 'complete':
                identity = tags['series']
                decode_identity(identity)
            for timestamp, value in row['values']:
                if timestamp not in allowed:
                    raise ValueError('Stored grid differs')
                if kind == PREFIX + 'complete':
                    count = number(value)
                    if count < 0 or not count.is_integer():
                        raise ValueError('Invalid completion count')
                    expected.setdefault(timestamp, []).append((generation, int(count)))
                else:
                    records = actual.setdefault((timestamp, generation), {})
                    if identity in records:
                        raise ValueError('Duplicate cached series')
                    records[identity] = value
        completed = {}
        for timestamp, markers in expected.items():
            if len(markers) == 1:
                generation, count = markers[0]
                records = actual.get((timestamp, generation), {})
                if len(records) == count:
                    completed[timestamp] = records
        return completed

    async def import_lines(self, lines):
        if lines:
            response = await self.client.post(self.vm + '/api/v1/import/prometheus',
                content='\n'.join(lines) + '\n', timeout=5)
            response.raise_for_status()

    def line(self, panel, step, generation, kind, value, timestamp, identity=None):
        selector = self.selector(panel, step, generation, kind)
        if identity is not None:
            selector = selector[:-1] + ',series=' + json.dumps(identity) + '}'
        return selector + ' ' + str(value) + ' ' + str(int(timestamp * 1000))

    async def materialize(self, panel, step, start, end, *, backfill=False):
        prefix = 'perses-backfill-pending-' if backfill else 'perses-pending-'
        pending_path = self.state / (prefix + digest(self.job_key(panel, step)) + '.json')
        pending = json.loads(pending_path.read_text()) if pending_path.exists() else None
        if pending:
            if pending['start'] != start:
                raise ValueError('Pending batch does not match watermark')
            end = pending['end']
        ticks = list(range(start, end + 1, step))
        if len(ticks) > 60 or start % step or end % step or end > self.now() - SEAL_SECONDS:
            raise ValueError('Unsafe materialization range')
        existing = await self.stored(panel, step, ticks)
        if len(existing) == len(ticks):
            pending_path.unlink(missing_ok=True)
            return end
        if pending:
            values = {int(t): samples for t, samples in pending['values'].items()}
            if set(values) != set(ticks) or digest(values) != pending['digest']:
                raise ValueError('Invalid durable pending batch')
        else:
            rows = await self.vm_query(self.expression(panel, step), start, end, step, timeout=4)
            values = points_by_time(rows, ticks)
            # Freeze the source result before the first import. Retries and
            # process restarts write identical samples, even after source edits.
            pending = {'start': start, 'end': end, 'values': values, 'digest': digest(values)}
            atomic_json(pending_path, pending)
        # Crash recovery must never overwrite already published points.
        missing = {t: samples for t, samples in values.items() if t not in existing}
        # Stable storage series: never create a new label value per batch.
        generation = digest(self.job_key(panel, step))
        async def send_once(phase, lines):
            if phase not in pending:
                await self.import_lines(lines)
                pending[phase] = self.now()
                atomic_json(pending_path, pending)

        def waiting(phase, description):
            since = pending[phase]
            if self.now() - since >= VISIBILITY_TIMEOUT:
                # A partial/lost write is a real failure. Permit an identical
                # replay on the next attempt; frozen values are never recomputed.
                pending.pop(phase)
                atomic_json(pending_path, pending)
                raise ValueError(description + ' failed read-back after visibility timeout')
            raise PublicationPending(description, since)

        if any(missing.values()):
            selector = self.selector(panel, step, generation, 'value')
            async def verify_values():
                stored_rows = await self.vm_query(selector + ' and (timestamp(' + selector + ') == time())', start, end, step)
                verified = {t: {} for t in missing}
                for row in stored_rows:
                    for timestamp, value in row['values']:
                        if timestamp in verified:
                            verified[timestamp][row['metric']['series']] = value
                return all(equal_values(samples, verified[timestamp]) for timestamp, samples in missing.items())
            await send_once('values_sent_at', [self.line(panel, step, generation, 'value', value, timestamp, identity)
                for timestamp, samples in missing.items() for identity, value in samples.items()])
            if not await self.read_back(verify_values):
                waiting('values_sent_at', 'Materialized values')
        await send_once('complete_sent_at', [self.line(panel, step, generation, 'complete', len(samples), timestamp)
                                           for timestamp, samples in missing.items()])
        async def verify_complete():
            checked = await self.stored(panel, step, ticks)
            return len(checked) == len(ticks) and all(equal_values(checked[t], samples) for t, samples in missing.items())
        if not await self.read_back(verify_complete):
            waiting('complete_sent_at', 'Completion markers')
        pending_path.unlink(missing_ok=True)
        return end

    @staticmethod
    async def read_back(check):
        # VM imports and visibility are separate. Do not hold the single worker
        # across flush cycles; the durable batch will be checked on a later turn.
        return await check()

    def initialize_backfill(self, admin):
        request = admin.get('backfill')
        if not request:
            return
        seconds = request.get('seconds')
        if seconds not in (43200, 86400):
            raise ValueError('Only bounded 12/24-hour backfills are supported')
        changed = False
        for key, forward in self.watermarks.items():
            if 'backfill:' + key in self.busy:
                continue
            step = int(key.rsplit(':', 1)[1])
            start = forward['start'] - seconds
            if key in self.backfills:
                current = self.backfills[key]
                if start < current['start']:
                    raise ValueError('Backfill expansion requires a new reviewed request')
                if start == current['start']:
                    continue
                # Range shrink is a scheduling change, never a VM deletion.
                # Change frozen pending state before its watermark so a crash
                # between the two atomic writes can safely repeat this migration.
                pending_path = self.state / ('perses-backfill-pending-' + digest(key) + '.json')
                if pending_path.exists():
                    pending = json.loads(pending_path.read_text())
                    if pending['end'] < start:
                        pending_path.replace(pending_path.with_name(pending_path.stem + '-before-' + str(start) + '.json'))
                    elif pending['start'] < start:
                        pending['values'] = {int(t): v for t, v in pending['values'].items() if int(t) >= start}
                        pending.update(start=start, digest=digest(pending['values']))
                        atomic_json(pending_path, pending)
                current.update(start=start, watermark=max(current['watermark'], start - step))
                self.failures.pop('backfill:' + key, None)
                self.waiting.pop('backfill:' + key, None)
                self.retry_at.pop('backfill:' + key, None)
                changed = True
                continue
            self.backfills[key] = {'start': start, 'end': forward['start'] - step,
                                   'watermark': start - step, 'requested_at': request['requested_at']}
            changed = True
        if changed:
            atomic_json(self.backfill_path, self.backfills)

    def backfill_priority(self, admin):
        return bool(admin.get('backfill') and self.now() < admin.get('backfill_priority_until', 0)
                    and any(s['watermark'] < s['end'] and
                            self.panels[key.rsplit(':', 2)[0]]['group'] not in admin.get('disabled_groups', [])
                            for key, s in self.backfills.items()))

    def next_batch(self, admin, historical_group=None):
        parallel = bool(admin.get('parallel_a3_backfill'))
        priority = self.backfill_priority(admin)
        if historical_group is not None and not parallel:
            return None
        forward, history = [], []
        urgent_live = False
        for panel in self.panels.values():
            if panel['group'] not in self.catalog['groups'] or panel['group'] in admin['disabled_groups']:
                continue
            for step in self.steps:
                key = self.job_key(panel, step)
                state = self.watermarks[key]
                start = state['watermark'] + step
                due = int((self.now() - SEAL_SECONDS) // step) * step
                urgent_live |= due - state['watermark'] > max(300, step)
                end = min(due, start + (min(60, max(1, 300 // step)) - 1) * step)
                if historical_group is None and key not in self.busy and start <= end and self.retry_at.get(key, 0) <= self.now():
                    forward.append((due - state['watermark'], (start, key, panel, step, end, False)))
                selected = panel['group'] == historical_group if historical_group else not (parallel and panel['group'] == 'a3')
                if selected and 'backfill:' + key not in self.busy and admin.get('backfill') and key in self.backfills:
                    state = self.backfills[key]
                    start = state['watermark'] + step
                    end = min(state['end'], start + 59 * step, due)
                    if start <= end and self.retry_at.get('backfill:' + key, 0) <= self.now():
                        history.append((start, key, panel, step, end, True))
        if historical_group and urgent_live and not priority:
            return None
        urgent = [entry for lag, entry in forward if lag > max(300, entry[3])]
        if urgent and not priority:
            return min(urgent, key=lambda item: (item[0], item[1]))
        if history and (priority or not forward or self.last_lane != 'backfill'):
            # Finish admission steps first; retain every supported resolution.
            return min(history, key=lambda item: (
                ('cpu', 'dcu', 'a3').index(item[2]['group']),
                (60, 120, 3600, 600, 20, 15, 5).index(item[3]), item[0], item[1]))
        if forward:
            return min((entry for _, entry in forward), key=lambda item: (item[0], item[1]))
        return None

    async def run(self):
        self.state.mkdir(parents=True, exist_ok=True)
        for panel in self.panels.values():
            for step in self.steps:
                key = self.job_key(panel, step)
                if key not in self.watermarks:
                    start = math.ceil(self.now() / step) * step
                    self.watermarks[key] = {'start': start, 'watermark': start - step}
        atomic_json(self.watermark_path, self.watermarks)
        extra = asyncio.create_task(self.run_worker(historical_group='a3'))
        try:
            await self.run_worker()
        finally:
            extra.cancel()
            await asyncio.gather(extra, return_exceptions=True)

    async def run_worker(self, historical_group=None):
        while not self.stopping:
            try:
                admin = self.admin()
                self.initialize_backfill(admin)
                if (self.online or not self.source_ready()) and not self.backfill_priority(admin):
                    await asyncio.sleep(.5)
                    continue
                batch = self.next_batch(admin, historical_group)
                if batch is None:
                    await asyncio.sleep(.5)
                    continue
                start, job, panel, step, end, backfill = batch
                if historical_group is None:
                    self.last_lane = 'backfill' if backfill else 'forward'
                key = 'backfill:' + job if backfill else job
                self.busy.add(key)
                try:
                    async with asyncio.timeout(12):
                        end = await self.materialize(panel, step, start, end, backfill=backfill)
                    states, path = (self.backfills, self.backfill_path) if backfill else (self.watermarks, self.watermark_path)
                    states[job]['watermark'] = end
                    atomic_json(path, states)
                    self.failures.pop('state', None)
                    self.failures.pop(key, None)
                    self.waiting.pop(key, None)
                except PublicationPending as pending:
                    self.counters['visibility_waits'] += 1
                    self.waiting[key] = {'phase': pending.phase, 'since': pending.since}
                    self.retry_at[key] = self.now() + 5
                except (TimeoutError, httpx.HTTPError, ValueError, KeyError) as error:
                    self.counters['worker_failures'] += 1
                    self.failures[key] = type(error).__name__ + ': ' + str(error)[:160]
                    self.retry_at[key] = self.now() + 30
                finally:
                    self.busy.discard(key)
                await asyncio.sleep(.1)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError) as error:
                self.failures['state'] = type(error).__name__
                await asyncio.sleep(5)

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
