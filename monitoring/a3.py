"""A3 vLLM observations: preserve endpoint identity and never bridge missing scrapes."""
import bisect
import collections
import json
import math
import os
from pathlib import Path
from .calculator import quantile_buckets
from .request_scope import SCHEMA
from .latency import snapshot, increments as histogram_increments
from . import a3_store, a3_effective

ENVIRONMENT = 'a3-vllm'
NODES = {'prefill': ('a3-1', '122.209.21.24'), 'decode': ('a3-2', '122.209.21.25')}
INSTANCE_COUNTS = {'prefill': 4, 'decode': 16}
LATENCIES = {'ttft': 'time_to_first_token_seconds', 'itl': 'inter_token_latency_seconds', 'e2e': 'e2e_request_latency_seconds'}
MAX_INTERVAL = 10  # A missed 5-second scrape invalidates the affected window.


def collection_coverage():
    """Deployment evidence only; never infer or rewrite historical completeness."""
    path = Path(os.environ.get('STATE_DIR', '/state')) / 'a3-collection-coverage.json'
    try:
        evidence = json.loads(path.read_text())
    except (OSError, ValueError):
        evidence = {}
    return {
        'expected_instances': dict(INSTANCE_COUNTS),
        'repair_complete_at': evidence.get('repair_complete_at'),
        'incomplete_confirmed_at': evidence.get('incomplete_confirmed_at'),
        'historical_policy': 'preserve_with_notice',
        'notice': 'A3 Decode 采集修复前仅覆盖 7100–7103；已确认的 DP=16 漏采期间历史不代表全量。历史原样保留，不乘倍数补算；更早拓扑未经核实，不推定漏采。修复后 Prefill 按 4、Decode 按 16 个引擎严格校验。',
    }


def valid_engine(rows, identity):
    """Each configured endpoint must expose exactly its own engine identity."""
    engine = str(int(identity[1].rsplit(':', 1)[1]) - 7100)
    seen = set()
    found = False
    for row in rows:
        if not row['name'].startswith('vllm:') or row['name'] in a3_effective.NAMES:
            continue
        labels = row['labels']
        if labels.get('engine') != engine or labels.get('instance') != identity[1] or labels.get('node') != identity[0]:
            return False
        key = (row['name'], json.dumps(labels, sort_keys=True))
        if key in seen:
            return False
        seen.add(key)
        found = True
    return found


def decode_export(lines):
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for obj in lines:
        labels = dict(obj['metric'])
        if labels.get('environment') != ENVIRONMENT or labels.get('job') not in ('vllm-a3', a3_store.JOB):
            continue
        name = labels.pop('__name__')
        identity = (a3_store.JOB if labels.get('job') == a3_store.JOB else labels.get('node'), labels.get('instance'))
        for ts, value in zip(obj['timestamps'], obj['values']):
            if labels.get('job') == a3_store.JOB or (isinstance(value, (float, int)) and math.isfinite(value)):
                groups[identity][ts / 1000].append({'name': name, 'labels': labels, 'value': value})
    return {key: (sorted(samples), samples) for key, samples in groups.items()}


def values(rows, name):
    selected = {}
    for row in rows:
        if row['name'] != name:
            continue
        key = json.dumps(row['labels'], sort_keys=True)
        value = row['value']
        if key in selected or not math.isfinite(value) or value < 0:
            return None
        selected[key] = value
    return selected or None


def increments(history, name):
    """Validate every intermediate observation, not just the window endpoints."""
    previous = first = None
    for _, rows in history:
        current = values(rows, name)
        if current is None or (previous is not None and (current.keys() != previous.keys() or any(current[k] < previous[k] for k in current))):
            return None
        if first is None:
            first = current
        previous = current
    return {k: previous[k] - first[k] for k in first} if first and len(history) >= 2 else None


def histogram(history, name):
    first = previous = None
    for _, rows in history:
        current, reason = snapshot(rows, 'vllm:' + name)
        if reason:
            return None
        if previous is not None:
            _, reason = histogram_increments(current, previous)
            if reason:
                return None
        if first is None:
            first = current
        previous = current
    if first is None or len(history) < 2:
        return None
    delta, reason = histogram_increments(previous, first)
    return {('+Inf' if math.isinf(k) else str(k)): v for k, v in delta.items()} if reason is None else None


def window(history):
    if len(history) < 2:
        return None
    eligible = [i for i, (ts, _) in enumerate(history[:-1]) if 55 <= history[-1][0] - ts <= 65]
    if not eligible:
        return None
    i = min(eligible, key=lambda i: abs(history[-1][0] - history[i][0] - 60))
    return history[i:]


def aggregate(histories, name, rate=False):
    total = 0
    for h in histories:
        delta = increments(h[-2:] if rate else h, 'vllm:' + name)
        if delta is None:
            return None
        total += sum(delta.values()) / (h[-1][0] - h[-2][0]) if rate else sum(delta.values())
    return total


def cache_ratio(histories, prefix):
    queries = aggregate(histories, prefix + 'queries_total')
    hits = aggregate(histories, prefix + 'hits_total')
    # Check each endpoint independently before allowing a combined ratio.
    valid = queries is not None and hits is not None
    for h in histories:
        q = increments(h, 'vllm:' + prefix + 'queries_total')
        v = increments(h, 'vllm:' + prefix + 'hits_total')
        valid = valid and q is not None and v is not None and sum(v.values()) <= sum(q.values())
    return (hits / queries if valid and queries > 0 else None, queries if valid else None, hits if valid else None)


def role_point(histories, complete):
    p = {'request_scope': 'all', 'request_schema': SCHEMA, 'request_scope_reason': '后端原生整体统计，不按客户端流式类型过滤', 'metric_scopes': {'ttft': 'native', 'itl': 'native', 'e2e': 'all'}, 'requests': None, 'decode_tokens': None, 'output_tokens': None, 'rate_interval_seconds': None,
         'latency_window_seconds': None, 'cache_60s': {'ratio': None, 'external_ratio': None, 'semantics': 'vllm-prefix-token-v1'},
         'percentiles': {k: dict.fromkeys(('p50', 'p95', 'p99', 'samples')) for k in LATENCIES},
         'resources': {'queue': {}, 'kv_usage': {}}, 'request_by_reason': {}}
    if not complete:
        p['cache_60s']['reason'] = '实例指标不完整或采集已过期'
        return p
    ready = all(len(h) >= 2 for h in histories)
    if ready:
        p['requests'] = aggregate(histories, 'request_success_total', rate=True)
        p['decode_tokens'] = p['output_tokens'] = aggregate(histories, 'generation_tokens_total', rate=True)
        p['rate_interval_seconds'] = sum(h[-1][0] - h[-2][0] for h in histories) / len(histories)
        if p['requests'] is not None:
            for h in histories:
                for labels, delta in increments(h[-2:], 'vllm:request_success_total').items():
                    reason = json.loads(labels).get('finished_reason', 'unknown')
                    p['request_by_reason'][reason] = p['request_by_reason'].get(reason, 0) + delta / (h[-1][0] - h[-2][0])
    for h in histories:
        rows = h[-1][1]
        for metric, group, suffix in [('num_requests_running', 'queue', 'running'), ('num_requests_waiting', 'queue', 'waiting'), ('kv_cache_usage_perc', 'kv_usage', 'ratio')]:
            v = values(rows, 'vllm:' + metric)
            if v and len(v) == 1:
                label = json.loads(next(iter(v)))
                p['resources'][group]['engine' + label.get('engine', 'unknown') + '_' + suffix] = next(iter(v.values()))
    windows = [window(h) for h in histories]
    if not all(windows):
        p['cache_60s']['reason'] = '正在积累约 60 秒连续观测'
        return p
    seconds = sum(h[-1][0] - h[0][0] for h in windows) / len(windows)
    p['latency_window_seconds'] = seconds
    for short, name in LATENCIES.items():
        buckets = [histogram(h, name) for h in windows]
        if any(b is None for b in buckets) or any(b.keys() != buckets[0].keys() for b in buckets):
            continue
        merged = {key: sum(b[key] for b in buckets) for key in buckets[0]}
        p['percentiles'][short] = {'samples': merged['+Inf'], **{q: quantile_buckets(merged, dict.fromkeys(merged, 0), v) for q, v in [('p50', .5), ('p95', .95), ('p99', .99)]}}
    ratio, queries, hits = cache_ratio(windows, 'prefix_cache_')
    external, _, _ = cache_ratio(windows, 'external_prefix_cache_')
    p['cache_60s'].update(ratio=ratio, external_ratio=external, input_tokens=queries, hit_tokens=hits, window_seconds=seconds,
                          reason=None if ratio is not None else '窗口内无前缀查询或计数不完整')
    return p


def replay(groups, start, end, emit_start=None, effective_since=None):
    previous = {}; histories = {}; snapshots = []; points = []
    for tick in range(int(start // 5) * 5, int(end // 5) * 5 + 1, 5):
        snapshot = {'ts': tick, 'environment': ENVIRONMENT, 'nodes': {}, 'enabled': True, 'source': 'victoriametrics', 'interval_seconds': 5, 'retention_hours': 720}
        point = {'ts': tick, 'environment': ENVIRONMENT, 'nodes': {}, 'mooncake': {}, 'source': 'victoriametrics'}
        snapshot['mooncake'] = a3_store.observe(groups, tick)
        point['mooncake'] = snapshot['mooncake']['data']
        for role, (node, address) in NODES.items():
            count = INSTANCE_COUNTS[role]
            expected = {(node, address + ':' + str(port)) for port in range(7100, 7100 + count)}
            active = set(); available = []; unexpected = set()
            for identity, (times, samples) in groups.items():
                if identity[0] != node:
                    continue
                i = bisect.bisect_right(times, tick) - 1
                ts = times[i] if i >= 0 else None
                rows = samples[ts] if ts is not None else []
                if identity not in expected:
                    if ts is not None and 0 <= tick - ts < 10:
                        unexpected.add(identity)
                    continue
                up = [r['value'] for r in rows if r['name'] == 'up']
                if ts is None or not 0 <= tick - ts < 10 or up != [1] or not valid_engine(rows, identity):
                    histories.pop(identity, None); previous.pop(identity, None)
                    continue
                active.add(identity)
                h = histories.setdefault(identity, [])
                if previous.get(identity) != ts:
                    if h and not 0 < ts - h[-1][0] < MAX_INTERVAL:
                        h.clear()
                    h.append((ts, rows)); previous[identity] = ts
                    while h and ts - h[0][0] > 70:
                        h.pop(0)
                if identity in expected:
                    available.append(h)
            complete = active == expected and len(available) == count and not unexpected
            if not complete:
                for h in available:
                    h[:] = h[-1:]
            if emit_start is not None and tick < emit_start:
                continue
            p = role_point(available, complete)
            if role == 'prefill':
                p['cache_60s']['effective'] = a3_effective.observe(available, complete, effective_since, tick)
            point['nodes'][role] = p
            observed = min((h[-1][0] for h in available), default=None)
            snapshot['nodes'][role] = {'node': node, 'metrics': {'status': 'ok' if complete else 'error', 'observed_at': observed,
                'error': None if complete else f'需要 {count} 个完整实例，当前可用 {len(available)}，异常新增 {len(unexpected)}',
                'data': {**p, 'rates': {k: p[k] for k in ('requests', 'decode_tokens', 'output_tokens')}, 'expected_instances': count, 'available_instances': len(available)}}}
        if emit_start is None or tick >= emit_start:
            snapshots.append(snapshot); points.append(point)
    return snapshots, points
