"""Validate production query expressions against isolated real VictoriaMetrics."""
import json
import math
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from gateway_generation import queries, ENDED, ERRORS, ORIGIN


def main():
    name = 'perses-generation-fixture'
    url = 'http://127.0.0.1:18538'
    if subprocess.run(['docker', 'inspect', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError('Fixture container already exists; refusing to replace it')
    report = {}
    with tempfile.TemporaryDirectory(prefix='perses-generation-') as folder:
        created = False
        try:
            subprocess.check_output(['docker', 'run', '-d', '--name', name, '--network', 'host', '--cpus', '.5', '--memory', '256m',
                '-v', folder + ':/storage', '--entrypoint', '/vm', 'monitoring-vm:1.151.0', '-storageDataPath=/storage',
                '-httpListenAddr=127.0.0.1:18538', '-memory.allowedBytes=128MiB'])
            created = True
            for _ in range(40):
                try:
                    urllib.request.urlopen(url + '/health', timeout=1).close()
                    break
                except OSError:
                    time.sleep(.2)
            else:
                raise RuntimeError('Fixture VM not ready')
            start = int(time.time() // 5) * 5 - 600
            cases = ['dcu-pd', 'a3-vllm', 'idle', 'absent', 'gap', 'stale', 'lifecycle', 'reset', 'partial', 'new-series', 'disappeared', 'old-gap', 'down']
            lines = []
            for case in cases:
                for i in range(61):
                    if case == 'gap' and i in (57, 58):
                        continue
                    if case == 'stale' and i > 54:
                        continue
                    if case == 'old-gap' and i in (30, 31):
                        continue
                    at = (start + i * 5) * 1000
                    labels = {'environment': case, 'job': 'aigate', 'instance': 'fixture'}
                    def emit(metric, value, extra=None):
                        tags = dict(labels, **(extra or {}))
                        if metric.startswith('aigate_'):
                            tags['request_scope'] = 'streaming'
                        label = '{' + ','.join(k + '=' + json.dumps(v) for k, v in tags.items()) + '}'
                        lines.append(f'{metric}{label} {value} {at}')
                    emit('up', 0 if case == 'down' and i == 58 else 1)
                    emit(ORIGIN, start + 275 if case == 'lifecycle' and i >= 55 else start - 600)
                    val = 10 if case == 'idle' else (i - 55 if case == 'reset' and i >= 55 else i)
                    scale = 2 if case == 'a3-vllm' else 1
                    for backend in ['first', 'second']:
                        if backend == 'second' and ((case == 'partial' and i in (57, 58)) or (case == 'disappeared' and i > 54)):
                            continue
                        extra = {'backend': backend, 'model': backend + '-model', 'stream': 'true'}
                        for result, weight in [('completed', 8), ('error', 2), ('client_cancelled', 1), ('client_disconnected', 1), ('unknown', 1)]:
                            emit(ENDED, val * weight * scale, dict(extra, result=result))
                        if case != 'absent' and (case != 'new-series' or i >= 59):
                            emit(ERRORS, val * 2 * scale, dict(extra, error_class='server_error', upstream_status='503'))
            request = urllib.request.Request(url + '/api/v1/import/prometheus', data=('\n'.join(lines) + '\n').encode())
            urllib.request.urlopen(request, timeout=20).close()
            urllib.request.urlopen(url + '/internal/force_flush', timeout=10).close()
            for step in (15, 60, 300):
                for case in cases:
                    result = []
                    for expression in queries(case):
                        params = {'query': expression.replace('$__interval', f'{step}s'), 'time': start + 300}
                        data = json.load(urllib.request.urlopen(url + '/api/v1/query?' + urllib.parse.urlencode(params), timeout=15))
                        assert data['status'] == 'success', data
                        rows = data['data']['result']
                        assert len(rows) <= 1, (case, rows)
                        result.append(float(rows[0]['value'][1]) if rows else None)
                    report[f'{case}/{step}'] = result
                    if case in ('dcu-pd', 'a3-vllm'):
                        scale = 2 if case == 'a3-vllm' else 1
                        expected = [26 / 5 * scale, 4 / 5 * scale, 200 / 13, 100 / 13, 100 / 13, 100 / 13]
                        assert all(a is not None and math.isclose(a, b, rel_tol=1e-6) for a, b in zip(result, expected)), (case, step, result)
                    elif case == 'idle':
                        assert result[:2] == [0, 0] and result[2:6] == [None] * 4, result
                    elif case in ('gap', 'stale', 'lifecycle', 'down') or (case == 'old-gap' and step == 300):
                        assert result == [None] * 6, (case, step, result)
                    elif case in ('reset', 'partial', 'disappeared'):
                        assert result[:6] == [None] * 6, (case, step, result)
                    elif case in ('absent', 'new-series'):
                        assert result[0] is not None and result[1:3] == [None, None], (case, result)
                    elif case == 'old-gap':
                        assert all(v is not None for v in result), result
            Path('semantics.json').write_text(json.dumps({'passed': True, 'scenarios': len(report), 'results': report}, indent=2))
            print(json.dumps({'passed': True, 'scenarios': len(report), 'queries': len(report) * len(queries('dcu-pd'))}))
        finally:
            if created:
                subprocess.run(['docker', 'rm', '-f', name], check=True, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
