"""Run isolated real VM queries on test4; never import into production VM."""
import json
import math
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from gateway_live import aggregate, oldest, STAGES

NAME = 'perses-gateway-live-fixture-20260915'
URL = 'http://127.0.0.1:18539'


def query(expression, at, step):
    data = urllib.parse.urlencode({'query': expression.replace('$__interval', f'{step}s'), 'time': at}).encode()
    result = json.load(urllib.request.urlopen(urllib.request.Request(URL + '/api/v1/query', data=data), timeout=15))
    assert result['status'] == 'success', result
    return result['data']['result']


def main():
    root = Path(sys.argv[1])
    storage = root / 'fixture-storage'
    assert root.is_dir() and not storage.exists(), 'Refuse to replace fixture storage'
    assert subprocess.run(['docker', 'inspect', NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
    storage.mkdir(mode=0o700)
    started = False
    try:
        subprocess.check_output(['docker', 'run', '-d', '--name', NAME, '--network', 'host', '--cpus', '.5', '--memory', '256m',
            '-v', str(storage) + ':/storage', '--entrypoint', '/vm', 'monitoring-vm:1.151.0', '-storageDataPath=/storage',
            '-httpListenAddr=127.0.0.1:18539', '-memory.allowedBytes=128MiB'])
        started = True
        for _ in range(50):
            try:
                urllib.request.urlopen(URL + '/health', timeout=1).close()
                break
            except OSError:
                time.sleep(.2)
        else:
            raise RuntimeError('Fixture did not start')
        start = int(time.time() // 5) * 5 - 600
        cases = ['dcu-pd', 'a3-vllm', 'idle', 'absent', 'gap', 'stale', 'down', 'restart', 'partial', 'disappeared', 'new-backend', 'stage-change', 'backend-migration', 'decrease']
        lines = []
        for case in cases:
            for i in range(61):
                if case == 'gap' and i == 59 or case == 'stale' and i >= 56:
                    continue
                ts = (start + i * 5) * 1000
                labels = {'environment': case, 'job': 'aigate', 'instance': 'fixture', 'node': 'fixture-node', 'service': 'aigate'}
                def emit(metric, value, extra=None):
                    tags = dict(labels, **(extra or {}))
                    if metric.startswith('aigate_'):
                        tags['request_scope'] = 'streaming'
                    label = '{' + ','.join(k + '=' + json.dumps(v) for k, v in tags.items()) + '}'
                    lines.append(f'{metric}{label} {value} {ts}')
                emit('up', 0 if case == 'down' and i == 59 else 1)
                emit('aigate_profile_start_time_seconds', start + 295 if case == 'restart' and i >= 59 else start - 600)
                emit('aigate_live_backend_groups', 2)
                if case == 'absent':
                    continue
                for backend in ('first', 'second'):
                    if backend == 'second' and ((case == 'partial' and i == 59) or (case == 'disappeared' and i >= 56) or (case == 'new-backend' and i < 59)):
                        continue
                    extra = {'backend': backend}
                    val = 0 if case == 'idle' else (2 if backend == 'first' else 3)
                    if case == 'backend-migration':
                        val = (5 if i < 58 else 0) if backend == 'first' else (0 if i < 58 else 5)
                    if case == 'decrease':
                        val = 9 if i < 59 else 1
                    emit('aigate_streams_waiting_first_output', val, extra)
                    emit('aigate_stream_first_output_wait_max_seconds', val * 10, extra)
                    age = 0 if case == 'idle' else (100 if backend == 'first' else 200)
                    emit('aigate_inflight_oldest_age_seconds', age, extra)
                    selected = 'streaming' if backend == 'first' else 'writing_client'
                    if case == 'stage-change' and backend == 'second' and i == 59:
                        selected = 'streaming'
                    for stage in STAGES:
                        emit('aigate_inflight_oldest_stage', int(case != 'idle' and stage == selected), dict(extra, stage=stage))
        request = urllib.request.Request(URL + '/api/v1/import/prometheus', data=('\n'.join(lines) + '\n').encode())
        urllib.request.urlopen(request, timeout=20).close()
        urllib.request.urlopen(URL + '/internal/force_flush', timeout=10).close()
        checks = []
        invalid = {'absent', 'gap', 'stale', 'down', 'restart', 'partial', 'disappeared', 'new-backend'}
        for step in (15, 60, 300):
            for case in cases:
                expressions = [aggregate('aigate_streams_waiting_first_output', case), aggregate('aigate_stream_first_output_wait_max_seconds', case, 'max'), oldest(case)]
                results = [query(q, start + 300, step) for q in expressions]
                if case in invalid:
                    assert all(not r for r in results), (case, step, results)
                else:
                    count = 0 if case == 'idle' else 2 if case == 'decrease' else 5
                    maximum = 0 if case == 'idle' else 10 if case == 'decrease' else 50 if case == 'backend-migration' else 30
                    assert len(results[0]) == len(results[1]) == 1
                    assert math.isclose(float(results[0][0]['value'][1]), count)
                    assert math.isclose(float(results[1][0]['value'][1]), maximum)
                    if case == 'stage-change':
                        assert not results[2], results[2]
                    else:
                        assert len(results[2]) == 1, results[2]
                        row = results[2][0]
                        assert float(row['value'][1]) == (0 if case == 'idle' else 200), row
                        assert row['metric']['stage_name'] == ('无在途请求' if case == 'idle' else STAGES['writing_client']), row
                        if case != 'idle':
                            assert row['metric']['backend'] == 'second'
                checks.append({'case': case, 'step': step, 'results': results})
        (root / 'live-semantics.json').write_text(json.dumps({'passed': True, 'scenarios': len(checks), 'checks': checks}, ensure_ascii=False, indent=2))
        print(json.dumps({'passed': True, 'scenarios': len(checks), 'queries': len(checks) * 3}))
    finally:
        if started:
            subprocess.run(['docker', 'rm', '-f', NAME], check=True, stdout=subprocess.DEVNULL)
        shutil.rmtree(storage)


if __name__ == '__main__':
    main()
