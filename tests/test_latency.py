import copy
import json
import math
import random
from pathlib import Path

import pytest
from monitoring.latency import LATENCIES, LatencyWindow, quantile_buckets, snapshot
from monitoring.replay import decode_export, replay


def incident(which):
    raw = json.loads((Path(__file__).parent / 'fixtures/e2e-20260914-1759.json').read_text())
    return [{'name': r['metric']['__name__'], 'labels': {k: v for k, v in r['metric'].items() if k != '__name__'}, 'value': r[which][1]} for r in raw]


def test_incident_old_bug_and_fixed_order_independence():
    a, b = incident('first'), incident('last')
    name = 'sglang:e2e_request_latency_seconds_bucket'
    def old(rows, after):
        def winning(row):
            le = row['labels'].get('le')
            flag = 'true' if le == '+Inf' or after and le == '2400.0' else 'false'
            return row['labels'].get('is_streaming') == flag
        return {r['labels']['le']: r['value'] for r in sorted(rows, key=winning) if r['name'] == name}
    before, after = old(a, False), old(b, True)
    delta = {k: after[k] - before[k] for k in before}
    assert delta['1800.0'] == 0 and delta['2400.0'] == 9520 and delta['+Inf'] == 2
    assert 1800 + 600 * (2 * .95 / 9520) == pytest.approx(1800.1197479)
    assert quantile_buckets(after, before, .95) is None
    for seed in range(20):
        random.Random(seed).shuffle(a); random.Random(seed + 1).shuffle(b)
        w = LatencyWindow()
        for ts in range(0, 61, 5):
            p = w.add(a if ts < 60 else b, ts)
        assert p['percentiles']['e2e'] == {'samples': 2, 'p50': 6, 'p95': 98, 'p99': 99.6}


def rows(n=10, other=20):
    result = []
    for short, metric in LATENCIES.items():
        for stream, count in [('true', n), ('false', other)]:
            labels = {'model_name': 'm', 'engine_type': 'decode', 'is_streaming': stream}
            for edge, v in [('1', count), ('100', count * 2), ('2400', count * 2), ('+Inf', count * 2)]:
                result.append({'name': 'sglang:' + metric + '_bucket', 'labels': {**labels, 'le': edge}, 'value': v})
            result.append({'name': 'sglang:' + metric + '_count', 'labels': labels, 'value': count * 2})
    return result


def history(failure=None):
    w = LatencyWindow()
    for ts in range(0, 131, 5):
        current = rows(100 + ts, 200 + ts)
        if ts == 40 and failure: failure(current)
        p = w.add(current, ts)
        if ts == 65: affected = p
    return affected, p


@pytest.mark.parametrize('failure', ['reset', 'missing', 'identity', 'bounds', 'conflict', 'infinite', 'nonmonotonic', 'count', 'incompatible'])
def test_invalid_middle_observation_and_recovery(failure):
    def change(rs):
        selected = [r for r in rs if 'e2e_' in r['name'] and r['labels']['is_streaming'] == 'true']
        if failure == 'reset':
            for r in selected: r['value'] = 0
        elif failure == 'missing': rs.remove(selected[0])
        elif failure == 'identity':
            for r in selected: r['labels']['model_name'] = 'new'
        elif failure in ('bounds', 'incompatible'): selected[0]['labels']['le'] = '2'
        elif failure == 'conflict': rs.append({**selected[0], 'value': 999})
        elif failure == 'infinite': selected[0]['value'] = math.inf
        elif failure == 'nonmonotonic': selected[0]['value'] = 999999
        elif failure == 'count': selected[-1]['value'] += 1
    broken, recovered = history(change)
    assert broken['percentiles']['e2e']['samples'] is None
    assert broken['percentiles']['ttft']['samples'] is not None
    assert recovered['percentiles']['e2e']['samples'] > 0


def test_zero_traffic_and_real_long_latency():
    w = LatencyWindow()
    for ts in range(0, 61, 5): p = w.add(rows(), ts)
    assert p['percentiles']['e2e'] == dict(p50=None, p95=None, p99=None, samples=0)
    a = {'100': 0, '1800': 0, '2400': 0, '+Inf': 0}
    b = {'100': 0, '1800': 0, '2400': 1, '+Inf': 1}
    assert quantile_buckets(b, a, .95) == 2370
    b['2400'] = 0
    assert quantile_buckets(b, a, .95) is None


def exported(conflict=False, down=False, gap=False):
    output = []
    for ts in range(0, 136, 5):
        if gap and 35 <= ts <= 50: continue
        for r in rows(100 + ts, 200 + ts) + [{'name': 'up', 'value': 0 if down and ts == 40 else 1, 'labels': {}}]:
            metric = {'__name__': r['name'], 'job': 'sglang-decode', 'instance': 'host:1', 'environment': 'dcu-pd', **r['labels']}
            item = {'metric': metric, 'timestamps': [ts * 1000], 'values': [r['value']]}
            output.extend([item, copy.deepcopy(item)])
            if conflict and ts == 40 and 'e2e_' in r['name']:
                output.append({**item, 'values': [r['value'] + 1]})
    return output








@pytest.mark.asyncio
@pytest.mark.parametrize('elapsed,failure,expected', [(3,False,2),(7,False,.1),(7,True,.1)])
async def test_slow_cycle_does_not_add_another_five_seconds(monkeypatch,elapsed,failure,expected):
    import asyncio
    import monitoring.api as api
    service=api.Service()
    clock=iter([100,100+elapsed])
    # Do not replace the event loop clock: patch the API time facade only.
    class Clock:
        monotonic=staticmethod(lambda:next(clock))
        time=staticmethod(lambda:1000)
    async def cycle():
        service.watermark=995
        if failure:raise OSError('VM temporarily unavailable')
    sleeps=[]
    async def sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError()
    monkeypatch.setattr(api,'time',Clock)
    monkeypatch.setattr(service,'cycle',cycle)
    monkeypatch.setattr(api.asyncio,'sleep',sleep)
    try:
        with pytest.raises(asyncio.CancelledError):await service.run()
        assert sleeps==[expected]
    finally:await service.client.aclose()
