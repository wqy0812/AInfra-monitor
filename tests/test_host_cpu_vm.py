"""Real-VM CPU materialization acceptance using disposable localhost data."""
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from monitoring import host_cpu
from monitoring.api import encode, history_expression

CASES = ('normal', 'zero', 'down', 'gap', 'restart', 'reset', 'stale', 'absent')


@pytest.fixture(scope='module')
def cpu_vm():
    url = os.environ.get('HOST_CPU_TEST_VM_URL')
    if not url:
        pytest.skip('Set HOST_CPU_TEST_VM_URL to a disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:'), 'Never inject fixtures into a remote service'
    client = httpx.Client(base_url=url, trust_env=False, timeout=20)
    base = int(time.time() // 5) * 5 - 6000
    ends, lines = {}, []
    for number, case in enumerate(CASES):
        start = base + number * 600
        ends[case] = start + 300
        for role, (node, address) in host_cpu.NODES.items():
            for i in range(61):
                if case == 'absent' or (case == 'gap' and i == 58) or (case == 'stale' and i >= 56):
                    continue
                ts = start + i * 5
                labels = {'environment': 'a3-vllm', 'job': 'node-a3', 'node': node, 'instance': address + ':9100'}
                def emit(metric, value, **extra):
                    tags = ','.join(k + '=' + json.dumps(v) for k, v in {**labels, **extra}.items())
                    lines.append(f'{metric}{{{tags}}} {value} {ts * 1000}')
                emit('up', 0 if case == 'down' and i == 58 else 1)
                emit('node_boot_time_seconds', start + 290 if case == 'restart' and i >= 58 else start - 100)
                index = i - 58 if case == 'reset' and i >= 58 else i + 100
                for cpu in ('0', '1'):
                    for mode, rate in [('idle', 1 if case == 'zero' else .5), ('iowait', 0 if case == 'zero' else .1), ('user', 0 if case == 'zero' else .4)]:
                        emit('node_cpu_seconds_total', index * 5 * rate, cpu=cpu, mode=mode)
    client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n').raise_for_status()
    client.get('/internal/force_flush').raise_for_status()

    def query(expression, start, end, step=5):
        response = client.get('/api/v1/query_range', params={
            'query': expression, 'start': start, 'end': end, 'step': step,
            'latency_offset': '1ms', 'nocache': '1',
        })
        response.raise_for_status()
        return response.json()['data']['result']

    baseline = json.loads((Path(__file__).parent / 'fixtures/host-cpu-queries.json').read_text())
    for case, end in ends.items():
        start = end - 65
        raw = query(host_cpu.query_expression(), start, end)
        expected = {}
        for field, expression in baseline.items():
            expected.update(host_cpu.samples(query(expression, start, end), start, end, field))
        assert host_cpu.samples(raw, start, end) == pytest.approx(expected, rel=1e-12, abs=1e-10)
        points = [{'ts': ts, 'nodes': {'prefill': {}, 'decode': {}}} for ts in range(start, end + 1, 5)]
        host_cpu.attach([], points, host_cpu.samples(raw, start, end), 'ok')
        client.post('/api/v1/import/prometheus', content=encode(points, 'a3-vllm')).raise_for_status()
    # An old invalid A3 CPU series must never shadow the new schema.
    for end in ends.values():
        client.post('/api/v1/import/prometheus', content=(
            'monitoring_chart_value{environment="a3-vllm",schema="v1",path="nodes.prefill.cpu"} 999 ' + str(end * 1000) + '\n')).raise_for_status()
    client.get('/internal/force_flush').raise_for_status()
    try:
        yield query, ends
    finally:
        client.close()


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('step', [5, 15, 60])
@pytest.mark.parametrize('node', ['.*', 'a3-1', 'a3-2'])
@pytest.mark.parametrize('panel,normal', [('p0', 40), ('extra-iowait', 10)])
def test_panel_reads_valid_materialized_values_and_filters_nodes(cpu_vm, case, step, node, panel, normal):
    query, ends = cpu_vm
    document = json.loads((Path(__file__).parents[1] / 'perses/projects/a3-monitoring/dashboards/a3-hosts.json').read_text())
    expr = document['spec']['panels'][panel]['spec']['queries'][0]['spec']['plugin']['spec']['query']
    result = query(expr.replace('$node', node).replace('$__interval', f'{step}s'), ends[case], ends[case])
    if case not in ('normal', 'zero'):
        assert result == []
    else:
        assert len(result) == (2 if node == '.*' else 1)
        assert all(float(s['values'][-1][1]) == pytest.approx(normal if case == 'normal' else 0) for s in result)
        assert all(s['metric']['node'] == node for s in result) if node != '.*' else True


def test_history_schema_and_missing_data_are_not_zero(cpu_vm):
    query, ends = cpu_vm
    for case, end in ends.items():
        values = query(history_expression('a3-vllm', 'value', 5), end, end)
        valid = query(history_expression('a3-vllm', 'valid', 5), end, end)
        cpu = [s for s in values if s['metric']['path'] in host_cpu.PATHS]
        flags = [s for s in valid if s['metric']['path'] in host_cpu.PATHS]
        assert len(cpu) == len(flags) == 4
        assert all(s['metric']['schema'] == host_cpu.SCHEMA for s in cpu + flags)
        assert all(float(s['values'][-1][1]) == int(case in ('normal', 'zero')) for s in flags)


@pytest.mark.asyncio
async def test_recent_single_point_collect_avoids_vm_default_latency_offset(cpu_vm):
    url = os.environ['HOST_CPU_TEST_VM_URL']
    end = int(time.time() // 5) * 5 - 5
    lines = []
    for node, address in host_cpu.NODES.values():
        labels = f'environment="a3-vllm",job="node-a3",node="{node}",instance="{address}:9100"'
        for i in range(15):
            ts = end - 70 + 5 * i
            for metric, extra, value in [('up', '', 1), ('node_boot_time_seconds', '', end - 1000),
                                          ('node_cpu_seconds_total', ',cpu="0",mode="idle"', 100 + i * 2.5),
                                          ('node_cpu_seconds_total', ',cpu="0",mode="user"', 100 + i * 2),
                                          ('node_cpu_seconds_total', ',cpu="0",mode="iowait"', 100 + i * .5)]:
                lines.append(f'{metric}{{{labels}{extra}}} {value} {ts * 1000}')
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
        (await client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n')).raise_for_status()
        (await client.get('/internal/force_flush')).raise_for_status()
        values, status = await host_cpu.collect(client, url, end, end)
    assert status == 'ok'
    assert values == {(end, role, field): pytest.approx(value) for role in ('prefill', 'decode') for field, value in [('cpu', 40), ('cpu_iowait', 10)]}
