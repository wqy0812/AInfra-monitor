"""Monitoring units, independent scheduler counters and historical windows."""
import collections
import json
import math
from .cache_metrics import TierWindow, QueryWindow
from .resource_series import resource_values

MAX_GAP = 20
WINDOW = 60


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def decode_counters(rows):
    """One deterministic PP/TP representative per independent DP group.

    Queue gauges enumerate schedulers even before their lazy decode counter exists.
    Missing representatives invalidate the sample instead of silently undercounting.
    Full labels remain in the identity so topology changes require a new baseline.
    """
    schedulers = [r for r in rows if r['name'] == 'sglang:num_running_reqs'
                  and r['labels'].get('engine_type') == 'decode']
    counters = [r for r in rows if r['name'] == 'sglang:realtime_tokens_total'
                and r['labels'].get('engine_type') == 'decode'
                and r['labels'].get('mode') == 'decode']
    if not schedulers or not counters:
        return None
    if len({r['labels'].get('model_name') for r in schedulers + counters}) != 1:
        return None
    groups = collections.defaultdict(list)
    for row in schedulers:
        labels = {k: v for k, v in row['labels'].items() if k != 'priority'}
        groups[labels.get('dp_rank', '')].append(labels)
    if '' in groups and len(groups) > 1:
        return None
    values = {}
    for row in counters:
        labels = {k: v for k, v in row['labels'].items() if k not in ('mode', 'priority')}
        key = json.dumps(labels, sort_keys=True)
        if key in values or not finite(row['value']) or row['value'] < 0:
            return None
        values[key] = row['value']
    selected = {}
    for group in groups.values():
        try:
            labels = min(group, key=lambda x: tuple(int(x.get(k, 0)) for k in ('pp_rank', 'tp_rank', 'moe_ep_rank')))
        except (ValueError, TypeError):
            return None
        key = json.dumps(labels, sort_keys=True)
        if key not in values:
            return None
        selected[key] = values[key]
    # An extra counter group is also a topology inconsistency.
    if {r['labels'].get('dp_rank', '') for r in counters} != set(groups):
        return None
    return selected


def counter_rate(current, previous, seconds):
    if not current or not previous or set(current) != set(previous) or not 0 < seconds < MAX_GAP:
        return None
    changes = [current[k] - previous[k] for k in current]
    return sum(changes) / seconds if all(finite(x) and x >= 0 for x in changes) else None


class CacheWindow:
    """Ratio of counter deltas, never an average of sample percentages."""
    def __init__(self):
        self.points = collections.deque()

    def add(self, sample):
        empty = {'ratio': None, 'window_seconds': None, 'input_tokens': None, 'hit_tokens': None}
        if not sample or not finite(sample.get('input_tokens')) or not sample.get('cache_sources'):
            self.points.clear()
            return empty
        now = sample['ts']
        counters = sample['cache_sources']
        if not all(finite(v) and v >= 0 for v in counters.values()):
            self.points.clear()
            return empty
        if self.points:
            old = self.points[-1]
            valid = (0 < now - old['ts'] < MAX_GAP
                     and sample['input_tokens'] >= old['input_tokens']
                     and set(counters) == set(old['cache_sources'])
                     and all(counters[k] >= old['cache_sources'][k] for k in counters))
            if not valid:
                self.points.clear()
        self.points.append(sample)
        # Retain the nearest baseline at or before 60 seconds; tolerate scrape jitter.
        while len(self.points) > 1 and now - self.points[1]['ts'] >= WINDOW:
            self.points.popleft()
        # With alternating scrape jitter, 65.01s / 59.99s are adjacent choices.
        # Use the valid newer baseline instead of creating a spurious empty point.
        while len(self.points) > 1 and now - self.points[0]['ts'] > 65:
            self.points.popleft()
        old = self.points[0]
        seconds = now - old['ts']
        if not 55 <= seconds <= 65:
            return empty
        inputs = sample['input_tokens'] - old['input_tokens']
        hits = sum(counters[k] - old['cache_sources'][k] for k in counters)
        return {'ratio': hits / inputs if inputs > 0 and 0 <= hits <= inputs else None,
                'window_seconds': seconds, 'input_tokens': inputs, 'hit_tokens': hits}


class CacheSeriesWindow:
    """Keep historical request accounting separate from effective prefill accounting."""
    def __init__(self):
        self.legacy = CacheWindow()
        self.tiers = TierWindow()
        self.semantics = None

    def add(self, sample):
        semantics = 'prefill-effective-v1' if sample and 'cache_effective' in sample else 'request-accounting-v1'
        if semantics != self.semantics or sample is None:
            self.legacy = CacheWindow()
            self.tiers = TierWindow()
        self.semantics = semantics
        if semantics == 'prefill-effective-v1':
            return self.tiers.add(sample)
        if sample and sample.get('cache_input_tokens') is not None:
            sample = {**sample, 'input_tokens': sample['cache_input_tokens']}
        result = self.legacy.add(sample)
        return {**result, 'semantics': semantics, 'device': None, 'host': None, 'storage': None,
                'reason': '历史请求入账口径' if result['ratio'] is not None else '正在积累数据或指标不可用'}


def fresh_store(snapshot):
    source = snapshot.get('mooncake') or {}
    observed = source.get('observed_at')
    if source.get('status') == 'ok' and finite(observed) and 0 <= snapshot['ts'] - observed < MAX_GAP:
        return source.get('data')
    return None


def fresh_source(snapshot, role, kind):
    source = snapshot.get('nodes', {}).get(role, {}).get(kind, {})
    observed = source.get('observed_at')
    if source.get('status') != 'ok' or not finite(observed) or not 0 <= snapshot['ts'] - observed < MAX_GAP:
        return None
    return source.get('data')


def chart_point(snapshot):
    out = {'ts': snapshot['ts'], 'nodes': {}}
    for role in snapshot.get('nodes', {}):
        metric = fresh_source(snapshot, role, 'metrics') or {}
        telemetry = fresh_source(snapshot, role, 'telemetry') or {}
        rates = metric.get('rates') or {}
        out['nodes'][role] = {
            'requests': rates.get('requests'), 'output_tokens': rates.get('output_tokens'),
            'decode_tokens': rates.get('decode_tokens'),
            'rate_interval_seconds': metric.get('rate_interval_seconds'),
            'percentiles': metric.get('percentiles') or {},
            'latency_window_seconds': metric.get('latency_window_seconds'),
            'cpu': (telemetry.get('host') or {}).get('cpu_percent'),
            'resources': resource_values(telemetry, metric),
        }
    return out


def history_points(snapshots, start):
    windows = {role: CacheSeriesWindow() for role in ('prefill', 'decode')}
    previous = {}
    store_window = QueryWindow()
    points = []
    for snapshot in snapshots:
        point = chart_point(snapshot)
        store = fresh_store(snapshot)
        if store and store.get('ts', 0) <= previous.get('mooncake', -1):
            store = None
        if store:
            previous['mooncake'] = store['ts']
        point['mooncake'] = {'capacity': (store or {}).get('capacity'), 'query_60s': store_window.add(store)}
        for role, window in windows.items():
            metric = fresh_source(snapshot, role, 'metrics')
            if metric and metric.get('cache_effective') is None and 'cache_effective' in metric and not metric.get('cache_schema'):
                metric = dict(metric)
                metric.pop('cache_effective', None)
            last = previous.get(role)
            # Repeated successful payloads are stale observations, not new samples.
            if metric and last is not None and metric['ts'] <= last:
                metric = None
            if metric:
                previous[role] = metric['ts']
            cache = window.add(metric)
            node = point['nodes'].setdefault(role, {})
            node['cache_60s'] = cache
            node['hicache'] = (metric or {}).get('hicache')
            if metric is None:
                for key in ('requests', 'output_tokens', 'decode_tokens'):
                    node[key] = None
                node['percentiles'] = {}
        if snapshot['ts'] >= start:
            points.append(point)
    return points


def downsample_points(points):
    stride = max(1, (len(points) + 717) // 719)
    selected = []
    gaps = {r: set() for r in ('prefill', 'decode')}
    cache_semantics = {}
    store_gap = False
    for index, point in enumerate(points):
        if (point.get('mooncake') or {}).get('capacity', None) is None or (point['mooncake']['capacity'] or {}).get('ratio') is None:
            store_gap = True
        for role, missing in gaps.items():
            node = point['nodes'].get(role, {})
            for key in ('requests', 'output_tokens', 'decode_tokens', 'cpu'):
                if node.get(key) is None:
                    missing.add(key)
            for key in ('ttft', 'itl', 'e2e'):
                if node.get('percentiles', {}).get(key, {}).get('p95') is None:
                    missing.add(key)
            cache = node.get('cache_60s') or {}
            if cache.get('ratio') is None or (role in cache_semantics and cache_semantics[role] != cache.get('semantics')):
                missing.add('cache')
            cache_semantics[role] = cache.get('semantics')
            if ((node.get('hicache') or {}).get('representative') or {}).get('ratio') is None:
                missing.add('hicache')
        if index % stride == 0 or index == len(points) - 1:
            for role, missing in gaps.items():
                point['nodes'].setdefault(role, {})['gap_before'] = sorted(missing)
                missing.clear()
            point.setdefault('mooncake', {})['gap_before'] = ['capacity'] if store_gap else []
            store_gap = False
            selected.append(point)
    return stride, selected


def history_projection():
    """Project only chart fields in SQLite; avoid loading 24h of host/rank payloads."""
    fields = []
    for role in ('prefill', 'decode'):
        base = '$.nodes.' + role
        for kind in ('metrics', 'telemetry'):
            for key in ('status', 'observed_at'):
                path = base + '.' + kind + '.' + key
                fields.extend(["'" + path + "'", "json_extract(data,'" + path + "')"])
        for field in ('ts', 'input_tokens', 'cache_sources', 'rates', 'percentiles', 'rate_interval_seconds', 'latency_window_seconds', 'cache_effective', 'cache_schema', 'hicache'):
            path = base + '.metrics.data.' + field
            fields.extend(["'" + path + "'", "json_extract(data,'" + path + "')"])
        path = base + '.telemetry.data.host.cpu_percent'
        fields.extend(["'" + path + "'", "json_extract(data,'" + path + "')"])
    for field in ('status', 'observed_at', 'data.ts', 'data.capacity', 'data.query_counters'):
        path = '$.mooncake.' + field
        fields.extend(["'" + path + "'", "json_extract(data,'" + path + "')"])
    return "json_set(json_object('ts',ts)," + ','.join(fields) + ')'
