"""Identity-preserving latency windows shared by live and historical DCU replay."""
import collections
import json
import math

LATENCIES = {'ttft': 'time_to_first_token_seconds', 'itl': 'inter_token_latency_seconds', 'e2e': 'e2e_request_latency_seconds'}
from .request_scope import SCHEMA, compatible_identities
PATHS = tuple('nodes.' + role + '.' + field for role in ('prefill', 'decode')
              for field in ['latency_window_seconds'] + ['percentiles.' + k + '.' + q for k in LATENCIES for q in ('p50', 'p95', 'p99', 'samples')])
PATH_REGEX = r'nodes\.(prefill|decode)\.(latency_window_seconds|percentiles\.(ttft|itl|e2e)\.(p50|p95|p99|samples))'


def valid_buckets(buckets):
    if not buckets or math.inf not in buckets:
        return False
    ordered = sorted(buckets.items())
    return (all(k >= 0 and not math.isnan(k) and isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for k, v in ordered)
            and all(a[1] <= b[1] for a, b in zip(ordered, ordered[1:])))


def canonical(buckets):
    try:
        result = {float(k): v for k, v in buckets.items()}
        return result if len(result) == len(buckets) and valid_buckets(result) else None
    except (ValueError, TypeError, AttributeError):
        return None


def quantile_buckets(current, before, q):
    current, before = canonical(current), canonical(before)
    if current is None or before is None or current.keys() != before.keys() or not 0 < q <= 1:
        return None
    delta = {k: current[k] - before[k] for k in current}
    if not valid_buckets(delta) or delta[math.inf] == 0:
        return None
    target = delta[math.inf] * q
    lower = previous = 0.0
    for upper, count in sorted(delta.items()):
        if count >= target:
            if math.isinf(upper):
                return None
            return lower + (upper - lower) * (target - previous) / (count - previous)
        lower, previous = upper, count
    return None


def snapshot(rows, name):
    """Use the native population, preserving each source before merging."""
    buckets, counts = {}, {}
    seen = {}
    for row in rows:
        if row['name'] not in (name + '_bucket', name + '_count'):
            continue
        labels = dict(row.get('source_labels', row['labels']))
        bound = labels.pop('le', None)
        identity = json.dumps(labels, sort_keys=True)
        if any(k.endswith('_rank') for k in labels):
            return None, 'ambiguous_identity'
        value = row['value']
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return None, 'invalid_value'
        key = (row['name'], identity, bound)
        if key in seen:
            if seen[key] != value:
                return None, 'conflicting_sample'
            continue
        seen[key] = value
        if row['name'].endswith('_count'):
            if bound is not None or identity in counts:
                return None, 'invalid_count'
            counts[identity] = value
        else:
            try:
                edge = float(bound)
            except (TypeError, ValueError):
                return None, 'invalid_bound'
            group = buckets.setdefault(identity, {})
            if edge in group:
                return None, 'duplicate_bound'
            group[edge] = value
    if not buckets or buckets.keys() != counts.keys():
        return None, 'missing_series'
    if not compatible_identities(buckets):
        return None, 'ambiguous_identity'
    edges = next(iter(buckets.values())).keys()
    for identity, group in buckets.items():
        if not valid_buckets(group) or group[math.inf] != counts[identity]:
            return None, 'invalid_cumulative_buckets'
        if group.keys() != edges:
            return None, 'incompatible_bounds'
    return buckets, None


def increments(current, previous):
    if current.keys() != previous.keys():
        return None, 'identity_changed'
    merged = None
    for identity, group in current.items():
        old = previous[identity]
        if group.keys() != old.keys():
            return None, 'bounds_changed'
        delta = {k: group[k] - old[k] for k in group}
        if any(v < 0 for v in delta.values()):
            return None, 'counter_reset'
        if not valid_buckets(delta):
            return None, 'invalid_bucket_delta'
        if merged is None:
            merged = dict.fromkeys(group, 0)
        for k, v in delta.items():
            merged[k] += v
    return merged, None


class LatencyWindow:
    def __init__(self):
        self.histories = {k: collections.deque(maxlen=14) for k in LATENCIES}

    def clear(self):
        for history in self.histories.values():
            history.clear()

    def add(self, rows, ts):
        result = {'percentiles': {}, 'latencies': {}, 'latency_quality': {}, 'latency_window_seconds': None}
        windows = []
        for kind, metric in LATENCIES.items():
            history = self.histories[kind]
            current, reason = snapshot(rows, 'sglang:' + metric)
            out = dict.fromkeys(('p50', 'p95', 'p99', 'samples'))
            result['percentiles'][kind] = out
            if current is None:
                history.clear()
            else:
                if history:
                    if not 0 < ts - history[-1][0] < 20:
                        reason = 'scrape_gap'
                    else:
                        _, reason = increments(current, history[-1][1])
                    if reason:
                        history.clear()
                history.append((ts, current))
                while history and ts - history[0][0] > 65:
                    history.popleft()
                seconds = ts - history[0][0]
                if seconds >= 55:
                    merged, reason = increments(current, history[0][1])
                    if merged is not None:
                        windows.append(seconds)
                        out.update(samples=merged[math.inf], **{q: quantile_buckets(merged, dict.fromkeys(merged, 0), n) for q, n in [('p50', .5), ('p95', .95), ('p99', .99)]})
                        reason = 'no_requests' if merged[math.inf] == 0 else ('overflow_bucket' if out['p99'] is None else 'ok')
                else:
                    reason = reason or 'warming_up'
            result['latency_quality'][kind] = reason
        if windows:
            result['latency_window_seconds'] = min(windows)
        return result
