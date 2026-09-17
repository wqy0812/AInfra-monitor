"""Real VM acceptance; GATEWAY_TEST_VM_URL must point to a disposable local VM."""
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from monitoring.gateway_live import expressions, STAGES, IDLE, OLDEST, samples

CASES = ('normal', 'idle', 'absent', 'gap', 'stale', 'restart', 'partial', 'down', 'stage-change', 'missing-stage')


@pytest.fixture(scope='module')
def fixture_vm():
    url = os.environ.get('GATEWAY_TEST_VM_URL')
    if not url:
        pytest.skip('Set GATEWAY_TEST_VM_URL to an isolated local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:'), 'Never inject fixtures into a remote service'
    base = int(time.time() // 5) * 5 - len(CASES) * 1000 - 600
    client = httpx.Client(base_url=url, trust_env=False, timeout=20)
    lines, ends = [], {}
    for number, case in enumerate(CASES):
        start = base + number * 1000
        ends[case] = start + 300
        for environment, scale in [('dcu-pd', 1), ('a3-vllm', 2)]:
            for i in range(61):
                if case == 'absent' or (case == 'gap' and i == 59) or (case == 'stale' and i > 55):
                    continue
                ts = start + 5 * i
                def emit(name, value, **labels):
                    tags = {'job': 'aigate', 'environment': environment, 'instance': case, **labels}
                    if name.startswith('aigate_'):
                        tags.setdefault('request_scope', 'streaming' if name == 'aigate_stream_idle_max_seconds' else 'all')
                    label = ','.join(k + '=' + json.dumps(v) for k, v in tags.items())
                    lines.append(f'{name}{{{label}}} {value} {ts * 1000}')
                emit('up', 0 if case == 'down' and i == 59 else 1)
                emit('aigate_profile_start_time_seconds', start + 295 if case == 'restart' and i >= 59 else start - 600)
                emit('aigate_live_backend_groups', 2)
                for backend in ('first', 'second'):
                    if case == 'partial' and backend == 'second' and i > 55:
                        continue
                    age = 0 if case == 'idle' else ((100 if backend == 'first' else 400) + i) * scale
                    idle = 0 if case == 'idle' else ((9 if backend == 'first' else 3) + i) * scale
                    emit('aigate_inflight_oldest_age_seconds', age, backend=backend)
                    emit('aigate_stream_idle_max_seconds', idle, backend=backend)
                    emit('aigate_inflight_oldest_age_seconds', 999999, backend=backend, request_scope='streaming')
                    emit('aigate_stream_idle_max_seconds', 999999, backend=backend, request_scope='all')
                    stage = 'writing_client' if backend == 'first' or (case == 'stage-change' and i >= 59) else 'streaming'
                    for name in STAGES:
                        if case == 'missing-stage' and backend == 'second' and name == 'streaming' and i >= 59:
                            continue
                        emit('aigate_inflight_oldest_stage', int(name == stage and age > 0), backend=backend, stage=name)
    client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n').raise_for_status()
    client.get('/internal/force_flush').raise_for_status()
    try:
        yield client, ends
    finally:
        client.close()


@pytest.mark.parametrize('environment,project,scale', [('dcu-pd', 'dcu-monitoring', 1), ('a3-vllm', 'a3-monitoring', 2)])
@pytest.mark.parametrize('step', [15, 60, 300])
@pytest.mark.parametrize('case', CASES)
def test_real_vm_gates_aggregation_and_dashboard_parity(fixture_vm, environment, project, scale, step, case):
    client, ends = fixture_vm
    end = ends[case]
    panels = json.loads((Path(__file__).parents[1] / 'perses/projects' / project / 'dashboards/gateway-generation.json').read_text())['spec']['panels']
    results = {}
    for field, query in expressions(environment, step).items():
        params = {'query': query, 'start': end - step, 'end': end, 'step': step, 'nocache': '1'}
        response = client.get('/api/v1/query_range', params=params)
        response.raise_for_status()
        results[field] = samples(response.json()['data']['result'], environment, field, end, end)
        panel = panels['live-idle-max' if field == IDLE else 'live-oldest']
        params['query'] = panel['spec']['queries'][0]['spec']['plugin']['spec']['query'].replace('$__interval', f'{step}s')
        reference = client.get('/api/v1/query_range', params=params)
        reference.raise_for_status()
        assert results[field] == samples(reference.json()['data']['result'], environment, field, end, end)
    if case in ('absent', 'gap', 'stale', 'restart', 'partial', 'down'):
        assert results == {IDLE: {}, OLDEST: {}}
    elif case in ('stage-change', 'missing-stage'):
        assert results[OLDEST] == {}
        assert results[IDLE][end][IDLE] == 69 * scale
    elif case == 'idle':
        assert results[IDLE][end][IDLE] == results[OLDEST][end][OLDEST] == 0
        assert results[OLDEST][end]['stage_name'] == '无在途请求'
    else:
        assert results[IDLE][end][IDLE] == 69 * scale
        assert results[OLDEST][end] == {OLDEST: 460 * scale, 'backend': 'second', 'stage': 'streaming', 'stage_name': '读取后续流'}
