import copy
import json
import math
from pathlib import Path

from monitoring.calculator import Calculator
from monitoring.latency import LatencyWindow
from monitoring.api import encode, history_expression
from monitoring.request_scope import is_request_path
from monitoring.profile_counters import metric_selector
from monitoring.request_profile import selector
from test_latency import rows
from test_a3 import fixture, point


def test_nonstream_histograms_cannot_change_latency_or_invalidate_streams():
    mixed, clean = LatencyWindow(), LatencyWindow()
    for ts in range(0, 66, 5):
        data = rows(100 + ts, 1)
        streaming = [r for r in data if r['labels']['is_streaming'] == 'true']
        for r in data:
            if r['labels']['is_streaming'] == 'false':
                r['value'] = math.nan if ts % 10 else -100
                r['labels']['model_name'] = 'unrelated'
        assert mixed.add(data, ts) == clean.add(streaming, ts)


def test_unknown_request_scope_stays_missing_and_keeps_cache_resources():
    p = point(fixture(streaming=False))
    assert p['requests'] is p['decode_tokens'] is None
    assert p['request_by_reason'] == {}
    assert p['resources']['queue'] == {}
    assert p['percentiles']['ttft']['samples'] is None
    assert p['cache_60s']['ratio'] == .6
    assert p['resources']['kv_usage']
    calc = Calculator()
    data = rows()
    for r in data: r['labels'].pop('is_streaming')
    for ts in range(0, 66, 5): value = calc.metrics('decode', data, ts)
    assert value['percentiles']['e2e']['samples'] is None


def test_a3_nonstream_counts_and_latency_do_not_enter_streaming_results():
    original = fixture()
    mixed = copy.deepcopy(original)
    for _, samples in mixed.values():
        for data in samples.values():
            extra = []
            for r in data:
                if r['name'].startswith('vllm:'):
                    false = copy.deepcopy(r)
                    false['labels']['is_streaming'] = 'false'
                    false['value'] = 999999
                    # Cache/KV resources retain their own scope; add only request metrics.
                    if 'latency' in r['name'] or 'time_to_first' in r['name'] or 'request_success' in r['name'] or 'generation_tokens' in r['name'] or 'num_requests' in r['name']:
                        extra.append(false)
            data.extend(extra)
    assert point(mixed) == point(original)


def test_dynamic_request_fields_have_new_history_schema_and_no_old_fallback():
    path = 'nodes.decode.resources.service_requests.count'
    queue = 'nodes.decode.resources.queue.engine0'
    assert is_request_path(path) and is_request_path(queue)
    assert not is_request_path('nodes.decode.resources.kv_usage.ratio')
    p = {'ts': 100, 'nodes': {'decode': {'resources': {'service_requests': {'count': 3}, 'queue': {'engine0': 1}}}}}
    lines = encode([p]).splitlines()
    assert all('schema="request-streaming-v1"' in line for line in lines if path in line or queue in line)
    for env in ('dcu-pd','a3-vllm'):
        expr = history_expression(env, 'value', 5)
        assert 'schema="latency-v2"' not in expr
        assert 'schema="v1",path!~' in expr
        assert 'schema="request-streaming-v1",path=~' in expr
    assert 'request_scope="streaming"' in metric_selector({}, ['requests_started_total'])
    assert 'request_scope="streaming"' in selector('requests_routed_total')


def test_dashboard_has_one_nonstream_panel_and_scopes_other_gateway_queries():
    document = json.loads((Path(__file__).parents[1] / 'perses/dashboards/gateway-generation.json').read_text())
    panels = document['spec']['panels']
    nonstream = [p for p in panels.values() if p['spec']['display']['name'] == '非流式请求数']
    assert len(nonstream) == 1 and len(panels) == 18
    for key, panel in panels.items():
        for q in panel['spec']['queries']:
            expr = q['spec']['plugin']['spec']['query']
            if key == 'live-nonstream-count':
                assert 'aigate_nonstream_requests_total' in expr and 'request_scope="nonstreaming"' in expr
            else:
                assert 'aigate_nonstream_requests_total' not in expr
                assert 'request_scope="streaming"' in expr
                assert 'request_scope="nonstreaming"' not in expr


def test_saved_dashboard_request_selectors_match_generators():
    import re
    import sys
    directory = Path(__file__).parents[1] / 'perses'
    sys.path.insert(0, str(directory))
    from generate import derived_schema
    from generate_a3 import derived
    assert 'schema="request-streaming-v1"' in derived('nodes.$role.resources.queue..*')
    assert 'schema="v1"' in derived('nodes.$role.resources.kv_usage..*')
    for path in (directory / 'dashboards').glob('*.json'):
        doc = json.loads(path.read_text())
        for panel in doc['spec']['panels'].values():
            for q in panel['spec'].get('queries', []):
                expr = q['spec']['plugin']['spec']['query']
                for match in re.finditer(r'monitoring_chart_\w+\{([^}]*)\}', expr):
                    labels = match[1]
                    metric_path = re.search(r'path=~("(?:\\.|[^"\\])*")', labels)
                    if metric_path:
                        assert 'schema=' + json.dumps(derived_schema(json.loads(metric_path[1]))) in labels, path
