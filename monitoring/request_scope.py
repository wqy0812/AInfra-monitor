"""Streaming request scope; resource/cache observations retain their own semantics."""
import re

SCHEMA = 'request-streaming-v1'
PATH_REGEX = r'nodes\.(prefill|decode)\.(requests|input_tokens|output_tokens|decode_tokens|latency_window_seconds|percentiles\.(ttft|itl|e2e)\.(p50|p95|p99|samples)|resources\.(queue|service_requests)\..+)'


def streaming_rows(rows):
    return [r for r in rows if r.get('source_labels', r['labels']).get('is_streaming') == 'true']


def is_request_path(path):
    return re.fullmatch(PATH_REGEX, path) is not None
