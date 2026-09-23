"""Execute on the target via SSH MCP. Preserve configuration and rollback container."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROLE, MODE = sys.argv[1:]
assert ROLE in ('monitoring', 'web') and MODE in ('prepare', 'switch', 'rollback')
VERSION = 'review-fixes-20260924'
ROOT = Path('/data2/monitoring/releases' if ROLE == 'monitoring' else '/data2/code-eval/releases') / VERSION
NAME = 'monitoring-api' if ROLE == 'monitoring' else 'code-eval-web'
PORT = 18430 if ROLE == 'monitoring' else 18080
TAG, BACKUP, FAILED = NAME + ':' + VERSION, NAME + '-before-' + VERSION, NAME + '-failed-' + VERSION
os.umask(0o077)


def cmd(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT).decode()


def inspect(name):
    return json.loads(cmd('docker', 'inspect', name))[0]


def exists(name):
    return subprocess.run(['docker', 'inspect', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def save(name, data):
    (ROOT / name).write_text(json.dumps(data, indent=2) + '\n')


def protected():
    return {c['Name']: [c['Id'], c['State']['StartedAt']] for c in json.loads(cmd('docker', 'inspect', *cmd('docker', 'ps', '-q').split())) if c['Name'] != '/' + NAME}


def get(path):
    with urllib.request.urlopen('http://127.0.0.1:' + str(PORT) + path, timeout=20) as response:
        return response.read().decode()


def hashes(manifest):
    code = 'import hashlib,json;from pathlib import Path;print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ' + repr(list(manifest)) + '}))'
    return json.loads(cmd('docker', 'exec', NAME, 'python', '-c', code))


def rollback(old, prepared):
    assert exists(BACKUP) and inspect(BACKUP)['Id'] == old['Id']
    if exists(NAME):
        assert inspect(NAME)['Image'] == prepared['image'], 'Concurrent container replacement'
        assert not exists(FAILED)
        cmd('docker', 'stop', '--time', '10', NAME)
        cmd('docker', 'rename', NAME, FAILED)
    cmd('docker', 'rename', BACKUP, NAME)
    cmd('docker', 'start', NAME)
    save('rolled-back.json', {'at': time.time(), 'restored_id': inspect(NAME)['Id']})


manifest = json.loads((ROOT / (ROLE + '-manifest.json')).read_text())
if MODE == 'prepare':
    assert not (ROOT / 'prepared.json').exists() and not exists(BACKUP) and not exists(FAILED)
    old = inspect(NAME)
    assert old['HostConfig']['NetworkMode'] == 'host'
    save('container-before.json', old)
    assert hashes(manifest['before']) == manifest['before'], 'Live source changed'
    for path, digest in manifest['after'].items():
        assert hashlib.sha256((ROOT / Path(path).name).read_bytes()).hexdigest() == digest
    (ROOT / 'Dockerfile').write_text('FROM ' + old['Image'] + '\n' + ''.join('COPY ' + Path(p).name + ' ' + p + '\n' for p in manifest['after']))
    with (ROOT / 'build.log').open('w') as log:
        subprocess.run(['docker', 'build', '--network=none', '-t', TAG, str(ROOT)], stdout=log, stderr=subprocess.STDOUT, check=True)
    if ROLE == 'monitoring':
        print(cmd('docker', 'run', '--rm', '--network=none', '-v', str(ROOT / 'check_candidate.py') + ':/check_candidate.py:ro', '--entrypoint', 'python', TAG, '-c', "import runpy;runpy.run_path('/check_candidate.py')"), flush=True)
    else:
        check = "from pathlib import Path;s=Path('/app/frontend/static/app.js').read_text();assert 'Prefill · XPU-1 观测于' in s and 'Prefill · XPU-2 观测于' not in s;compile(Path('/app/backend/app/main.py').read_text(),'main.py','exec');print('Web candidate passed')"
        print(cmd('docker', 'run', '--rm', '--network=none', '--entrypoint', 'python', TAG, '-c', check), flush=True)
    assert inspect(NAME)['Id'] == old['Id']
    save('prepared.json', {'old_id': old['Id'], 'image': inspect(TAG)['Id'], 'protected': protected()})
    print('Prepared', ROLE, flush=True)
else:
    old = json.loads((ROOT / 'container-before.json').read_text())
    prepared = json.loads((ROOT / 'prepared.json').read_text())
    if MODE == 'rollback':
        rollback(old, prepared)
        sys.exit(0)
    assert inspect(NAME)['Id'] == prepared['old_id'] and protected() == prepared['protected']
    assert hashes(manifest['before']) == manifest['before']
    assert not exists(BACKUP) and not exists(FAILED) and not (ROOT / 'complete.json').exists()
    fields = ('User', 'ExposedPorts', 'Env', 'Cmd', 'Healthcheck', 'Volumes', 'WorkingDir', 'Entrypoint', 'Labels', 'StopSignal', 'StopTimeout', 'Hostname')
    cfg = {k: old['Config'][k] for k in fields if k in old['Config']}
    cfg.update(Image=prepared['image'], HostConfig=old['HostConfig'])
    class Docker(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect('/var/run/docker.sock')
    try:
        cmd('docker', 'stop', '--time', '30', NAME)
        cmd('docker', 'rename', NAME, BACKUP)
        conn = Docker('localhost', timeout=60)
        conn.request('POST', '/v1.39/containers/create?name=' + NAME, json.dumps(cfg), {'Content-Type': 'application/json'})
        response = conn.getresponse()
        response.read()
        assert response.status == 201, response.status
        conn.close()
        cmd('docker', 'start', NAME)
        for attempt in range(45):
            try:
                health = json.loads(get('/health' if ROLE == 'monitoring' else '/api/health'))
                assert health['status'] == 'ok'
                assert inspect(NAME)['State'].get('Health', {}).get('Status', 'healthy') == 'healthy'
                latest = json.loads(get('/api/monitoring/latest?environment=xpu-pd'))
                assert time.time() - latest['ts'] < 60
                assert latest['nodes']['prefill']['metrics']['data']['cache_60s']['ratio'] is not None
                break
            except Exception:
                if attempt == 44:
                    raise
                time.sleep(1)
        checks = {}
        for env in ('dcu-pd', 'a3-vllm', 'xpu-pd'):
            history = json.loads(get('/api/monitoring/history?hours=3&environment=' + env))
            assert history['environment'] == env and history['points']
            checks[env] = {'points': len(history['points']), 'gateway_status': history['gateway_status']}
            if env == 'xpu-pd':
                values = [p['nodes']['prefill']['cache_60s']['ratio'] for p in history['points']]
                assert any(v is not None for v in values)
                checks[env]['valid_cache_points'] = sum(v is not None for v in values)
        assert hashes(manifest['after']) == manifest['after']
        if ROLE == 'web':
            assert 'Prefill · XPU-1 观测于' in get('/static/app.js')
        assert protected() == prepared['protected']
        result = {'passed': True, 'image': prepared['image'], 'rollback_container': BACKUP, 'other_containers_unchanged': True, 'health': health, 'history': checks, 'hashes': manifest['after']}
        save('complete.json', result)
        print(json.dumps(result), flush=True)
    except BaseException:
        if exists(BACKUP):
            rollback(old, prepared)
        elif inspect(NAME)['Id'] == old['Id']:
            cmd('docker', 'start', NAME)
        raise
