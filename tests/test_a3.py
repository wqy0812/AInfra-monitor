import copy
import json
import math
import pytest
from monitoring import a3
from monitoring.api import Service, encode


def fixture(drop=None, reset=None, idle=False, streaming=False, end=170):
    groups = {}
    for role, (node, host) in a3.NODES.items():
        for engine in range(a3.INSTANCE_COUNTS[role]):
            instance = host + ':' + str(7100 + engine)
            labels = dict(job='vllm-a3', environment='a3-vllm', node=node, instance=instance, engine=str(engine), model_name='m')
            if streaming:
                labels["is_streaming"] = "true"
            samples = {}
            for tick in range(100, end + 1, 5):
                n = 100 if idle else tick - 100 + 100
                if reset and (node, engine, tick) == reset:
                    n = 1
                rows = []
                def add(name, v, **extra):
                    rows.append(dict(name=name, value=v, labels={**labels, **extra}))
                add('up', 0 if drop == (node, engine, tick) else 1)
                add('vllm:request_success_total', n, finished_reason='stop')
                add('vllm:request_success_total', n * 2, finished_reason='length')
                add('vllm:generation_tokens_total', n * 10)
                for prefix in ['prefix_cache_', 'external_prefix_cache_']:
                    add('vllm:' + prefix + 'queries_total', n * 100)
                    add('vllm:' + prefix + 'hits_total', n * 60)
                for name in a3.LATENCIES.values():
                    for bound, value in [('0.1', n), ('1', n * 2), ('+Inf', n * 2)]:
                        add('vllm:' + name + '_bucket', value, le=bound)
                    add('vllm:' + name + '_count', n * 2)
                for name, v in [('num_requests_running', 0), ('num_requests_waiting', 0), ('kv_cache_usage_perc', .2)]:
                    add('vllm:' + name, v)
                samples[tick] = rows
            groups[(node, instance)] = (sorted(samples), samples)
    return groups


def point(groups, end=170):
    return a3.replay(groups, 100, end)[1][-1]['nodes']['decode']


def test_role_specific_instances_rates_cache_and_histograms():
    p = point(fixture())
    assert p['requests'] == 48 and p['decode_tokens'] == 160
    assert p['rate_interval_seconds'] == 5
    assert p['request_by_reason'] == {'stop': 16, 'length': 32}
    assert p['cache_60s']['ratio'] == .6 and p['cache_60s']['input_tokens'] == 96000
    assert p['percentiles']['ttft'] == {'samples': 1920, 'p50': .1, 'p95': pytest.approx(.91), 'p99': pytest.approx(.982)}
    snap, points = a3.replay(fixture(), 100, 170)
    assert points[-1]['nodes']['prefill']['decode_tokens'] == 40
    for role, count in [('prefill', 4), ('decode', 16)]:
        data = snap[-1]['nodes'][role]['metrics']['data']
        assert data['expected_instances'] == data['available_instances'] == count
        assert len(data['resources']['queue']) == count * 2
        assert len(data['resources']['kv_usage']) == count


def test_new_decode_endpoint_contributes_independently():
    g = fixture()
    for t, rows in g[('a3-2', '122.209.21.25:7115')][1].items():
        for r in rows:
            if r['name'] == 'vllm:generation_tokens_total':
                r['value'] *= 7
    assert point(g)['decode_tokens'] == 220


@pytest.mark.parametrize('failure', ['missing', 'stale', 'reset', 'duplicate_engine', 'duplicate_row', 'extra_down'])
def test_new_endpoint_failures_never_produce_partial_total(failure):
    g = fixture()
    key = ('a3-2', '122.209.21.25:7115')
    times, samples = g[key]
    if failure == 'missing':
        del g[key]
    elif failure == 'stale':
        g[key] = ([t for t in times if t <= 150], samples)
    elif failure == 'reset':
        for r in samples[170]:
            if r['name'] == 'vllm:generation_tokens_total':r['value'] = 0
    elif failure == 'duplicate_engine':
        for r in samples[170]:
            if r['name'].startswith('vllm:'):r['labels']['engine'] = '0'
    elif failure == 'duplicate_row':
        samples[170].append(copy.deepcopy(samples[170][1]))
    elif failure == 'extra_down':
        g[('a3-2', '122.209.21.25:7199')] = ([170], {170: [{'name': 'up', 'labels': {}, 'value': 0}]})
    assert point(g)['decode_tokens'] is None


def test_new_endpoint_recovers_without_bridging_missing_samples():
    g = fixture(drop=('a3-2', 15, 140))
    assert point(g, 140)['decode_tokens'] is None
    assert point(g, 145)['decode_tokens'] is None
    assert point(g, 150)['decode_tokens'] == 160
    assert point(g, 170)['percentiles']['ttft']['samples'] is None
    recovered = point(fixture(drop=('a3-2', 15, 140), end=210), 210)
    assert recovered['percentiles']['ttft']['samples'] == 1920
    assert recovered['cache_60s']['ratio'] == .6


def test_coverage_metadata_is_explicit_and_preserves_history(tmp_path, monkeypatch):
    monkeypatch.setenv('STATE_DIR', str(tmp_path))
    assert a3.collection_coverage()['repair_complete_at'] is None
    (tmp_path / 'a3-collection-coverage.json').write_text(json.dumps({'repair_complete_at': 500, 'incomplete_confirmed_at': 100}))
    coverage = a3.collection_coverage()
    assert coverage['repair_complete_at'] == 500
    assert coverage['incomplete_confirmed_at'] == 100
    assert coverage['historical_policy'] == 'preserve_with_notice'


def test_idle_has_zero_throughput_but_no_latency_or_cache_ratio():
    p = point(fixture(idle=True))
    assert p['requests'] == p['decode_tokens'] == 0
    assert p['cache_60s']['ratio'] is None
    assert p['percentiles']['ttft']['samples'] == 0
    assert p['percentiles']['ttft']['p95'] is None


@pytest.mark.parametrize('failure', ['down', 'missing', 'reset', 'new_engine', 'extra_instance', 'malformed_histogram'])
def test_incomplete_windows_do_not_create_valid_aggregates(failure):
    g = fixture(drop=('a3-2', 2, 140) if failure == 'down' else None, reset=('a3-2', 2, 140) if failure == 'reset' else None)
    key = ('a3-2', '122.209.21.25:7102');times, samples = g[key]
    if failure == 'missing':
        times.remove(140);del samples[140]
    if failure == 'new_engine':
        for t, rows in samples.items():
            if t >= 140:
                for r in rows:r['labels']['engine'] = '9'
    if failure == 'extra_instance':
        g[('a3-2', '122.209.21.25:7199')] = copy.deepcopy(g[key])
    if failure == 'malformed_histogram':
        for r in samples[165]:
            if r['name'] == 'vllm:time_to_first_token_seconds_bucket' and r['labels']['le'] == '0.1':r['value'] = 999999
    p = point(g)
    assert p['percentiles']['ttft']['p95'] is None
    if failure != 'malformed_histogram':assert p['cache_60s']['ratio'] is None


def test_one_reset_cannot_be_hidden_by_other_engine_growth():
    g = fixture(reset=('a3-2', 2, 170));p = point(g)
    assert p['requests'] is None and p['decode_tokens'] is None


def test_missing_endpoint_and_stale_data_are_errors():
    g = fixture();del g[('a3-2', '122.209.21.25:7102')]
    snapshots, _ = a3.replay(g, 100, 170)
    assert snapshots[-1]['nodes']['decode']['metrics']['status'] == 'error'
    assert point(g)['requests'] is None
    assert point(fixture(), end=190)['requests'] is None


def test_cache_invalid_endpoint_is_not_masked_by_others():
    g = fixture();rows = g[('a3-2', '122.209.21.25:7102')][1][170]
    for r in rows:
        if r['name'] == 'vllm:prefix_cache_hits_total':r['value'] += 100000
    assert point(g)['cache_60s']['ratio'] is None


def test_decode_keeps_instance_labels_and_environment_boundary():
    labels = dict(__name__='up', job='vllm-a3', node='a3-1', instance='host:7100', environment='a3-vllm')
    g = a3.decode_export([dict(metric=labels, timestamps=[100000], values=[1]),dict(metric={**labels,'environment':'dcu-pd'},timestamps=[105000],values=[999])])
    assert g[('a3-1','host:7100')][0] == [100]
    assert g[('a3-1','host:7100')][1][100][0]['labels']['instance'] == 'host:7100'


@pytest.mark.asyncio
async def test_environments_have_independent_watermarks_queries_and_history(tmp_path, monkeypatch):
    import monitoring.api as api
    monkeypatch.setattr(api, 'STATE', tmp_path)
    (tmp_path/'watermark.json').write_text('{"ts":100}')
    dcu, asc = Service(), Service('a3-vllm')
    assert dcu.watermark == 0 and asc.watermark == 0
    assert (tmp_path/'watermark.json').read_text() == '{"ts":100}'
    assert asc.watermark_file != dcu.watermark_file
    exprs = []
    async def query(expr, *args):
        exprs.append(expr);return []
    asc.query = query
    history = await asc.history(1, 100, 200)
    assert history['environment'] == 'a3-vllm'
    assert history['collection_coverage']['expected_instances'] == {'prefill': 4, 'decode': 16}
    assert all('environment="a3-vllm"' in x for x in exprs)
    assert dcu.cache == {}
    await dcu.client.aclose();await asc.client.aclose()
    encoded = encode(a3.replay(fixture(), 100, 170)[1][-1:], 'a3-vllm')
    assert 'environment="dcu-pd"' not in encoded
    assert 'cache_60s.external_ratio' in encoded


@pytest.mark.parametrize('failure', ['normal', 'down', 'missing', 'reset', 'idle'])
def test_realtime_warmup_optimization_is_exact(failure):
    g=fixture(drop=('a3-2', 2, 140) if failure=='down' else None,
              reset=('a3-2', 2, 140) if failure=='reset' else None, idle=failure=='idle')
    if failure=='missing':
        times,samples=g[('a3-2','122.209.21.25:7102')];times.remove(140);del samples[140]
    full=a3.replay(g,100,170)
    for start in (100,135,160,170,175):
        partial=a3.replay(g,100,170,emit_start=start)
        assert partial==tuple([p for p in items if p['ts']>=start] for items in full)
