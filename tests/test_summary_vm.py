"""Real-VM acceptance for the summary dashboards using disposable localhost data."""
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'perses'))
from project_split import read_resources

ENV = {'dcu-monitoring': 'dcu-pd', 'a3-monitoring': 'a3-vllm', 'xpu-monitoring': 'xpu-pd'}
NODES = {'dcu-monitoring': ('dcu1', 'dcu2'), 'a3-monitoring': ('a3-1', 'a3-2'), 'xpu-monitoring': ('xpu-2', 'xpu-1')}
CASES = ('normal', 'down')
STEP = 15
GIB = 2 ** 30


def fixtures(base):
    lines, ends = [], {}
    for number, case in enumerate(CASES):
        start = base + number * 600
        ends[case] = start + 300
        for i in range(61):
            ts = start + i * 5

            def emit(metric, value, **labels):
                tags = ','.join(k + '=' + json.dumps(v) for k, v in labels.items())
                lines.append(f'{metric}{{{tags}}} {value} {ts * 1000}')
            for project, env in ENV.items():
                for role, p95 in (('prefill', 0.8), ('decode', 0.3)):
                    for quantile, value in (('p50', p95 / 4), ('p95', p95), ('p99', p95 * 3)):
                        path = f'nodes.{role}.percentiles.ttft.{quantile}'
                        emit('monitoring_chart_value', value, environment=env, schema='request-metrics-v2', path=path)
                        emit('monitoring_chart_valid', 1, environment=env, schema='request-metrics-v2', path=path)
                if case == 'down':
                    emit('up', 0, environment=env, job='node-extra', instance='down:9100')
            # DCU and XPU scheduler queues: two ranks per role.
            for env in ('dcu-pd', 'xpu-pd'):
                for role, values in (('prefill', (3, 5)), ('decode', (1, 2))):
                    labels = dict(environment=env, job='sglang-' + role, instance=role + ':30000')
                    emit('up', 1, **labels)
                    for rank, value in enumerate(values):
                        emit('sglang:num_queue_reqs', value, **labels, dp_rank=str(rank), tp_rank=str(rank), moe_ep_rank=str(rank))
            # A3 vLLM: two engines per node.
            for node, values in (('a3-1', (4, 6)), ('a3-2', (1, 0))):
                for port, value in zip((7100, 7101), values):
                    labels = dict(environment='a3-vllm', job='vllm-a3', node=node, instance=f'{node}:{port}')
                    emit('up', 1, **labels)
                    emit('vllm:num_requests_waiting', value, **labels, engine='0')
            # DCU cards: two per node, 64 GiB each.
            for role, node, used in (('prefill', 'dcu1', (10, 20)), ('decode', 'dcu2', (30, 5))):
                labels = dict(environment='dcu-pd', job='dcu-' + role, node=node, instance=node + ':19500')
                emit('up', 1, **labels)
                emit('dcu_sample_success', 1, **labels)
                emit('dcu_sample_timestamp_seconds', ts, **labels)
                for device, value in zip(('card0', 'card1'), used):
                    emit('dcu_memory_used_bytes', value * GIB, **labels, device=device)
                    emit('dcu_memory_total_bytes', 64 * GIB, **labels, device=device)
    return lines, ends


@pytest.fixture(scope='module')
def summary_vm():
    url = os.environ.get('SUMMARY_TEST_VM_URL')
    if not url:
        pytest.skip('Set SUMMARY_TEST_VM_URL to a disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:'), 'Never inject fixtures into a remote service'
    client = httpx.Client(base_url=url, trust_env=False, timeout=30)
    lines, ends = fixtures(int(time.time() // 5) * 5 - 6000)
    client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n').raise_for_status()
    client.get('/internal/force_flush').raise_for_status()

    def query(expression, end):
        response = client.get('/api/v1/query_range', params={
            'query': expression.replace('$__interval', f'{STEP}s'), 'start': end - 60, 'end': end, 'step': STEP,
            'latency_offset': '1ms', 'nocache': '1'})
        assert response.status_code == 200, response.text
        return {(r['metric'].get('role'), r['metric'].get('node')): float(r['values'][-1][1])
                for r in response.json()['data']['result'] if r['values'][-1][0] == end}
    try:
        yield query, ends
    finally:
        client.close()


def dashboards():
    return read_resources(ROOT / 'perses' / 'projects')['dashboards']


def summary(project):
    d = next(d for d in dashboards() if d['metadata']['project'] == project and d['metadata']['name'] == 'summary')
    return {key: [q['spec']['plugin']['spec'] for q in p['spec']['queries']] for key, p in d['spec']['panels'].items()}


def test_every_dashboard_query_parses_and_runs(summary_vm):
    query, ends = summary_vm
    for d in dashboards():
        for key, p in d['spec']['panels'].items():
            for q in p['spec']['queries']:
                query(q['spec']['plugin']['spec']['query'].replace('$role', '.*').replace('$node', '.*').replace('$device', '.*'), ends['normal'])


@pytest.mark.parametrize('project', ENV)
def test_latency_panel_shows_only_p95_per_role(summary_vm, project):
    query, ends = summary_vm
    result = query(summary(project)['backend-ttft'][0]['query'], ends['normal'])
    assert result == pytest.approx({('Prefill', None): 0.8, ('Decode', None): 0.3})


@pytest.mark.parametrize('project,expected', [('dcu-monitoring', (5, 2)), ('xpu-monitoring', (5, 2)), ('a3-monitoring', (6, 1))])
def test_queue_panel_takes_the_largest_rank_or_instance(summary_vm, project, expected):
    query, ends = summary_vm
    for spec, value in zip(summary(project)['backend-queue'], expected):
        assert list(query(spec['query'], ends['normal']).values()) == [value], spec['seriesNameFormat']


def test_card_memory_is_the_fullest_card_per_node_with_total_reference(summary_vm):
    query, ends = summary_vm
    used, total = summary('dcu-monitoring')['accelerator-memory']
    assert query(used['query'], ends['normal']) == pytest.approx({('Prefill', 'dcu1'): 20, ('Decode', 'dcu2'): 30})
    assert query(total['query'], ends['normal']) == pytest.approx({(None, None): 64})


@pytest.mark.parametrize('project', ENV)
@pytest.mark.parametrize('case,expected', [('normal', 0), ('down', 1)])
def test_scrape_down_counts_failed_targets_and_shows_zero_when_healthy(summary_vm, project, case, expected):
    query, ends = summary_vm
    assert query(summary(project)['scrape-down'][0]['query'], ends[case]) == {(None, None): expected}
