"""Independent validity expectations and old/new equality on a disposable VM."""
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'perses'))
from project_queries import Queries
from gateway_generation import ENDED
from query_slimming import RESULTS, generation_expression, histogram
from query_slimming_fixtures import legacy_panel

STEPS = (5, 15, 20, 60, 120, 600, 3600)
HIST_CASES = ('normal', 'zero', 'absent', 'gap', 'stale', 'down', 'restart', 'reset',
              'missing-bucket', 'missing-count', 'missing-infinite', 'extra-bucket',
              'bad-count', 'bad-raw', 'bad-rate', 'multi-bad', 'partial-instance', 'group-restart', 'mixed-groups')
GEN_CASES = ('normal', 'zero', 'absent-result', 'gap-result', 'reset-result', 'stale-result',
             'down', 'restart', 'multi-instance')


@pytest.fixture(scope='module')
def vm():
    url = os.environ.get('QUERY_SLIMMING_TEST_VM_URL')
    if not url:
        pytest.skip('Set QUERY_SLIMMING_TEST_VM_URL to a disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:'), 'Never inject fixtures into production'
    client = httpx.Client(base_url=url, trust_env=False, timeout=60)
    prefix = 'slim-' + uuid.uuid4().hex[:8]
    start = int(time.time() // 5) * 5 - 8000
    end = start + 7500
    lines = []

    def emit(name, value, ts, labels):
        tags = ','.join(k + '=' + json.dumps(v) for k, v in labels.items())
        lines.append(f'{name}{{{tags}}} {value} {ts * 1000}')
        if len(lines) >= 20000:
            flush()

    def flush():
        if lines:
            client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n').raise_for_status()
            lines.clear()

    for case in HIST_CASES:
        for instance in ('n1', 'n2'):
            for i in range(1501):
                ts = start + 5 * i
                if case == 'absent' or (case == 'gap' and i == 1498) or (case == 'stale' and i >= 1495):
                    continue
                labels = dict(environment=prefix + '-h-' + case, job='fixture', instance=instance, backend='b', model='m')
                if case == 'mixed-groups':
                    labels.update(backend=instance, model=instance)
                emit('up', int(not (case == 'down' and i == 1498)), ts, labels)
                emit('boot', end - 10 if case == 'restart' and i >= 1498 else start - 8000, ts, labels)
                emit('group_boot', end - 10 if case == 'group-restart' and i >= 1498 else start - 8000, ts, labels)
                val = 0 if case == 'zero' else i * 5 + 100
                if case == 'reset' and i >= 1498:
                    val -= 7400
                for bound, factor in [('1', 1), ('2', 2), ('+Inf', 3)] + ([('4', 3)] if case == 'extra-bucket' else []):
                    if ((case == 'missing-bucket' or (case == 'partial-instance' and instance == 'n2')) and bound == '2') or (case == 'missing-infinite' and bound == '+Inf'):
                        continue
                    v = val * factor
                    if (case == 'bad-raw' or (case == 'mixed-groups' and instance == 'n2')) and bound == '1': v = val * 4
                    if case == 'multi-bad': v = val * (4 - factor)
                    if case == 'bad-rate': v = val * 4 if bound == '1' else val * factor + 100000
                    emit('slim_latency_bucket', v, ts, dict(labels, le=bound))
                if case != 'missing-count':
                    v = val * (1 if case == 'multi-bad' else 3)
                    if case == 'bad-rate': v += 100000
                    if case == 'bad-count': v += 1
                    emit('slim_latency_count', v, ts, labels)
    for case in GEN_CASES:
        for instance in ('n1', 'n2'):
            for i in range(1501):
                ts = start + 5 * i
                labels = dict(environment=prefix + '-g-' + case, job='aigate', instance=instance)
                emit('up', int(not (case == 'down' and i == 1498)), ts, labels)
                emit('aigate_error_metrics_start_time_seconds', end - 10 if case == 'restart' and i >= 1498 else start - 8000,
                     ts, dict(labels, request_scope='all'))
                for result, factor in [('completed', 4), ('error', 3), ('client_cancelled', 1), ('client_disconnected', 2), ('unknown', 3)]:
                    if result == 'client_cancelled':
                        if case == 'absent-result' or (instance == 'n2' and ((case == 'gap-result' and i == 1498) or (case == 'stale-result' and i >= 1495))):
                            continue
                    val = 0 if case == 'zero' else (i + 100) * 5 * factor
                    if result == 'client_cancelled' and instance == 'n2' and case == 'reset-result' and i >= 1498:
                        val -= 7400
                    emit(ENDED, val, ts, dict(labels, request_scope='all', result=result))
    flush()
    client.get('/internal/force_flush').raise_for_status()

    def query(expression, step):
        r = client.post('/api/v1/query_range', data={'query': expression.replace('$__interval', str(step) + 's'),
                        'start': end, 'end': end, 'step': step, 'nocache': '1', 'latency_offset': '1ms'})
        r.raise_for_status()
        data = r.json()
        assert data['status'] == 'success' and not data.get('warnings') and not data.get('isPartial')
        return data['data']['result']
    try:
        yield query, prefix
    finally:
        client.close()


def normalized(rows):
    return {json.dumps(r['metric'], sort_keys=True): r['values'] for r in rows}


@pytest.mark.parametrize('step', STEPS)
@pytest.mark.parametrize('case', HIST_CASES)
def test_histogram_old_new_and_independent_validity(vm, case, step):
    query, prefix = vm
    q = Queries(prefix + '-h-' + case, 'fixture', origin='boot')
    old = q.histogram_quantiles('slim_latency', ['1', '2', '+Inf'], 'environment,backend,model', 'group_boot')
    # The synthetic three-bucket family intentionally uses the legacy pairwise
    # constructor; never let an optimized constructor make this an identity test.
    assert old.count('> ignoring(le)') == 4 and 'label_map(' not in old
    a, b = query(old, step), query(histogram(old), step)
    assert normalized(a) == normalized(b)
    assert bool(b) == (case in ('normal', 'mixed-groups')), (case, step, b)
    if b:
        expected = {'P50': 1.5, 'P95': 2, 'P99': 2}
        assert len(b) == 3
        for row in b:
            if case == 'mixed-groups': assert row['metric']['backend'] == row['metric']['model'] == 'n1'
            assert float(row['values'][0][1]) == pytest.approx(expected[row['metric']['perses_quantile']])


@pytest.mark.parametrize('step', STEPS)
@pytest.mark.parametrize('case', GEN_CASES)
def test_generation_preserves_per_result_veto_and_zero(vm, case, step):
    query, prefix = vm
    env = prefix + '-g-' + case
    panel = legacy_panel('a3-monitoring', 'gateway-requests', 'generation-0')
    expressions = [q['spec']['plugin']['spec']['query'] for q in panel['spec']['queries'][1:]]
    assert len(expressions) == 3 and all('environment="a3-vllm"' in expression for expression in expressions)
    expressions = [expression.replace('environment="a3-vllm"', 'environment="' + env + '"') for expression in expressions]
    new = query(generation_expression(expressions[0]), step)
    expected = []
    for i, (expression, (_, label)) in enumerate(zip(expressions, RESULTS), 1):
        for row in query(expression, step):
            row['metric'].update(perses_series=label, perses_order='%02d' % i)
            expected.append(row)
    assert normalized(expected) == normalized(new)
    labels = {r['metric']['perses_series'] for r in new}
    want = set() if case in ('down', 'restart') else {label for _, label in RESULTS}
    if case in ('absent-result', 'gap-result', 'reset-result', 'stale-result'):
        want.remove('客户端取消')
    assert labels == want
    for row in new:
        value = 0 if case == 'zero' else 2 * (1 + [label for _, label in RESULTS].index(row['metric']['perses_series']))
        assert float(row['values'][0][1]) == pytest.approx(value)
