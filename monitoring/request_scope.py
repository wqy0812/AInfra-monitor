"""Metric-specific populations; preserve source identity before aggregating."""
import json
import math
import re

SCHEMA = 'request-metrics-v2'
PATH_REGEX = r'nodes\.(prefill|decode)\.(requests|input_tokens|output_tokens|decode_tokens|latency_window_seconds|percentiles\.(ttft|itl|e2e)\.(p50|p95|p99|samples)|resources\.(queue|service_requests)\..+)'


def compatible_identities(identities):
    """Only merge mutually exclusive stream partitions of the same source."""
    bases, scopes = set(), set()
    for identity in identities:
        labels = json.loads(identity)
        scope = labels.pop('is_streaming', None)
        if scope not in (None, 'true', 'false'):
            return False
        scopes.add(scope)
        bases.add(json.dumps(labels, sort_keys=True))
    return len(bases) == 1 and not (None in scopes and len(scopes) > 1)


def request_counters(rows, name):
    counters = {}
    for row in rows:
        if row['name'] != name:
            continue
        labels = row.get('source_labels', row['labels'])
        value = row['value']
        if any(k.endswith('_rank') for k in labels) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return None
        identity = json.dumps(labels, sort_keys=True)
        if identity in counters and counters[identity] != value:
            return None
        counters[identity] = value
    return counters if compatible_identities(counters) else None


def is_request_path(path):
    return re.fullmatch(PATH_REGEX, path) is not None
