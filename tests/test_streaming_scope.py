"""Population regressions: nonstream data affects only metrics that include it."""
import copy
import json
import math
from pathlib import Path
import pytest
from monitoring.calculator import Calculator, request_counters
from monitoring.latency import LatencyWindow
from monitoring.monitor_series import counter_rate
from monitoring.api import encode, history_expression
from monitoring.profile_counters import metric_selector, summary_rows
from monitoring.request_profile import selector
from test_latency import rows
from test_a3 import fixture, point


def test_native_nonstream_contributes_to_all_backend_latencies():
    mixed, clean = LatencyWindow(), LatencyWindow()
    for ts in range(0, 66, 5):
        data = rows(100 + ts, 200 + 3*ts)
        streaming = [r for r in data if r['labels']['is_streaming'] == 'true']
        a, b = mixed.add(data, ts), clean.add(streaming, ts)
    assert a['percentiles']['ttft']['samples'] == 4*b['percentiles']['ttft']['samples']
    assert a['percentiles']['e2e']['samples'] == 4*b['percentiles']['e2e']['samples']
    bad = copy.deepcopy(data)
    for r in bad:
        if r['labels']['is_streaming'] == 'false': r['value'] = math.nan
    p = mixed.add(bad, 70)
    assert p['percentiles']['ttft']['samples'] is None
    assert p['percentiles']['e2e']['samples'] is None


def test_native_unlabelled_itl_and_a3_metrics_remain_usable():
    window = LatencyWindow()
    for ts in range(0, 66, 5):
        data = [r for r in rows(100+ts) if 'inter_token_latency' in r['name'] and r['labels']['is_streaming']=='true']
        for r in data: r['labels'].pop('is_streaming')
        p = window.add(data, ts)
    assert p['percentiles']['itl']['samples'] == 130
    p = point(fixture())
    assert p['requests'] == 12 and p['decode_tokens'] == 40
    assert p['percentiles']['ttft']['samples'] == 480
    assert p['resources']['queue'] and p['cache_60s']['ratio'] == .6


def counters(stream=100, nonstream=200):
    return [{'name':'sglang:num_requests_total','labels':{'model_name':'m','is_streaming':flag},'value':n}
            for flag,n in [('true',stream),('false',nonstream)]]


def test_counts_merge_partitions_after_individual_reset_and_identity_checks():
    old = request_counters(counters())
    assert counter_rate(request_counters(counters(110,220)), old, 5) == 6
    # Overall sum rises, but one partition reset: the rate must remain unknown.
    assert counter_rate(request_counters(counters(1,1000)), old, 5) is None
    assert counter_rate(request_counters(counters()[:1]), old, 5) is None
    unlabelled = {'name':'sglang:num_requests_total','labels':{'model_name':'m'},'value':300}
    assert request_counters(counters()+[unlabelled]) is None
    assert sum(request_counters([unlabelled]).values()) == 300
    assert request_counters(counters()+[{**unlabelled,'labels':{'model_name':'other'}}]) is None


def test_tokens_use_both_partitions_and_scheduler_gauges_need_no_stream_label():
    calc = Calculator()
    for ts in (0,5):
        data = []
        for name in ('num_requests_total','prompt_tokens_total','generation_tokens_total'):
            data += [{**r,'name':'sglang:'+name} for r in counters(100+ts,200+2*ts)]
        data += [{'name':'sglang:num_queue_reqs','labels':{'model_name':'m','tp_rank':'0'},'value':7}]
        p = calc.metrics('decode',data,ts)
    assert p['rates']['requests'] == p['rates']['input_tokens'] == p['rates']['output_tokens'] == 3
    assert p['rank_gauges'][0]['value'] == 7
    assert p['request_scope'] == 'all'


def test_new_schema_has_no_old_request_fallback():
    path = 'nodes.decode.resources.service_requests.count'
    p = {'ts':100,'nodes':{'decode':{'resources':{'service_requests':{'count':3}}}}}
    assert all('schema="request-metrics-v2"' in line for line in encode([p]).splitlines() if path in line)
    for env in ('dcu-pd','a3-vllm'):
        expr = history_expression(env,'value',5)
        assert 'schema="request-metrics-v2",path=~' in expr
        assert 'request-streaming-v1' not in expr and 'latency-v2' not in expr
        assert 'schema="v1",path!~' in expr


def test_profile_scope_follows_metric_and_discards_old_general_streaming_samples():
    assert 'request_scope="all"' in selector('requests_routed_total')
    assert 'request_scope="streaming"' in selector('first_increment_seconds_count')
    assert 'request_scope=~"all|streaming"' in metric_selector({},['requests_started_total','first_increment_seconds_count'])
    rs = [{'metric':{'__name__':name,'request_scope':scope,'rollup':'last_over_time'},'value':[100,'2']}
          for name in ('aigate_requests_started_total','aigate_first_increment_seconds_count') for scope in ('all','streaming')]
    selected = [dict(identity) for identity in summary_rows(rs)]
    assert len(selected)==2
    assert {r['__name__']:r['request_scope'] for r in selected} == {'aigate_requests_started_total':'all','aigate_first_increment_seconds_count':'streaming'}


def test_saved_dashboard_queries_follow_the_current_metric_policy():
    import sys
    root = Path(__file__).parents[1]/'perses'
    sys.path.insert(0,str(root))
    from metric_scope import apply_scope
    from project_split import current_request_schema
    for p in list((root/'projects').glob('*/dashboards/*.json'))+list((root/'dashboards').glob('*.json')):
        doc=json.loads(p.read_text())
        for panel in doc['spec']['panels'].values():
            for query in panel['spec'].get('queries',[]):
                expr=query['spec']['plugin']['spec']['query']
                assert expr == apply_scope(expr), p
                assert expr == current_request_schema(expr), p
