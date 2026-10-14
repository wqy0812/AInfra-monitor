"""Execute the new overview queries against disposable local VictoriaMetrics."""
import json
import os
import time

import httpx
import pytest

from monitoring import accelerator_history as accelerator, gateway_live
from monitoring.native_schema import NODES

CASES = ('normal', 'zero', 'missing-prefill', 'partial-card', 'stale', 'gap', 'down',
         'reset', 'restart', 'invalid-card', 'partial-errors', 'long-normal', 'long-gap')


@pytest.fixture(scope='module')
def overview_vm():
    url = os.environ.get('MONITORING_TEST_VM_URL')
    if not url:
        pytest.skip('Disposable local VictoriaMetrics required')
    assert url == 'http://127.0.0.1:18543'
    client = httpx.Client(base_url=url, trust_env=False, timeout=20)
    start = int(time.time() // 5) * 5 - 18000
    lines, ends = [], {}
    for case in CASES:
        length = 740 if case.startswith('long-') else 80
        end = start + length * 5
        ends[case] = end
        for environment, nodes in NODES.items():
            scale = list(NODES).index(environment) + 1
            for i in range(length + 1):
                if ((case == 'stale' and i > length - 5) or
                        (case == 'gap' and i == length - 1) or (case == 'long-gap' and i == 400)):
                    continue
                ts = start + i * 5
                def emit(metric, value, **labels):
                    tags = {'environment': environment, **labels}
                    encoded = ','.join(k + '=' + json.dumps(v) for k, v in tags.items())
                    lines.append(f'{metric}{{{encoded}}} {value} {ts * 1000}')

                healthy = 0 if case == 'down' and i == length - 1 else 1
                gateway = {'job': 'aigate', 'instance': f'overview-{environment}-{case}'}
                emit('up', healthy, **gateway)
                origin = ts - 5 if case == 'restart' and i >= length - 2 else start - 600
                emit('aigate_error_metrics_start_time_seconds', origin, **gateway, request_scope='all')
                for group in range(2):
                    if case == 'partial-errors' and group == 1 and i > length - 5:
                        continue
                    value = 0 if case == 'zero' else (i + 100) * 5 * scale * (group + 1)
                    if case == 'reset' and i >= length - 2:
                        value = (i - length + 2) * 5 * scale * (group + 1)
                    emit('aigate_upstream_errors_total', value, **gateway, request_scope='all', backend=str(group))
                    emit('aigate_upstream_errors_total', (i + 100) * 9999, **gateway, request_scope='streaming', backend=str(group))
                for role, node in nodes.items():
                    if case == 'missing-prefill' and role == 'prefill':
                        continue
                    job, metric, device = {'dcu-pd': ('dcu-' + role, 'dcu_utilization_percent', 'device'),
                                           'a3-vllm': ('npu-a3', 'npu_chip_info_utilization', 'id'),
                                           'xpu-pd': ('xpu-hardware', 'node_xpu_utilization', 'devid')}[environment]
                    source = {'job': job, 'instance': f'overview-{node}-{case}', 'node': node}
                    emit('up', healthy, **source)
                    if environment == 'dcu-pd':
                        emit('dcu_sample_success', 1, **source)
                        emit('dcu_sample_timestamp_seconds', ts, **source)
                    for card in range(2):
                        if case == 'partial-card' and role == 'prefill' and card == 1:
                            continue
                        value = card * 40 + (40 if role == 'decode' else 0)
                        if case == 'zero':
                            value = 0
                        elif case == 'invalid-card':
                            value = -1
                        emit(metric, value, **source, **{device: str(card)})
        start = end + 150
    client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n').raise_for_status()
    client.get('/internal/force_flush').raise_for_status()

    def query(expression, case, step):
        end = ends[case]
        response = client.post('/api/v1/query_range', data={
            'query': expression, 'start': end - step, 'end': end, 'step': step,
            'nocache': 1, 'latency_offset': '1ms'})
        response.raise_for_status()
        body = response.json()
        assert body['status'] == 'success' and not body.get('isPartial')
        return end, body['data']['result']
    try:
        yield query
    finally:
        client.close()


@pytest.mark.parametrize('environment', NODES)
@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('step', [5, 15])
def test_actual_error_rates_and_node_averages(overview_vm, environment, case, step):
    end, rows = overview_vm(gateway_live.expressions(environment, step)[gateway_live.ERRORS], case, step)
    errors = gateway_live.samples(rows, environment, gateway_live.ERRORS, end, end)
    bad_errors = ('stale', 'gap', 'down', 'reset', 'restart', 'partial-errors')
    if case in bad_errors:
        assert errors == {}
    else:
        expected = 0 if case == 'zero' else 3 * (list(NODES).index(environment) + 1)
        assert errors[end][gateway_live.ERRORS] == pytest.approx(expected)
    end, rows = overview_vm(accelerator.expression(environment, step), case, step)
    values = accelerator.samples(rows, environment, end, end)
    if case in ('stale', 'invalid-card') or (step == 15 and case in ('gap', 'down')):
        assert values == {}
    else:
        expected = {'prefill': 0 if case in ('zero', 'partial-card') else 20, 'decode': 0 if case == 'zero' else 60}
        if case == 'missing-prefill':
            del expected['prefill']
        assert values[end] == expected


@pytest.mark.parametrize('environment', NODES)
@pytest.mark.parametrize('case', ['long-normal', 'long-gap'])
def test_long_step_cannot_bridge_hidden_scrape_gap(overview_vm, environment, case):
    step = 3605  # 30-day history step; the rate window still stays one minute.
    end, rows = overview_vm(gateway_live.expressions(environment, step)[gateway_live.ERRORS], case, step)
    errors = gateway_live.samples(rows, environment, gateway_live.ERRORS, end, end)
    end, rows = overview_vm(accelerator.expression(environment, step), case, step)
    values = accelerator.samples(rows, environment, end, end)
    if case == 'long-gap':
        assert errors == values == {}
    else:
        assert errors[end][gateway_live.ERRORS] == pytest.approx(3 * (list(NODES).index(environment) + 1))
        assert values[end] == {'prefill': 20, 'decode': 60}
