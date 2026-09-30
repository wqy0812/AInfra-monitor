"""Guarded monitoring-api repair; run on test4 through SSH MCP, no rollback."""
import argparse
import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
NAME = 'monitoring-api'
BACKUP = 'monitoring-api-before-review-fixes-20260930'
TAG = 'monitoring-api:review-fixes-20260930'
RELEASE = Path('/data2/monitoring/release')
PEERS = '127.0.0.1,::1,122.247.53.162,122.247.53.250'
DEST = '/monitoring/monitoring/'


def command(*args, timeout=30):
    return subprocess.check_output(args, text=True, timeout=timeout)


def inspect(name):
    return json.loads(command('docker', 'inspect', name))[0]


def save(name, value):
    target = ROOT/name
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(target)


def read(name):
    return json.loads((ROOT/name).read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def live_hashes():
    names = ('api.py', 'query_client.py', 'gateway_live.py', 'xpu_cache.py')
    code = 'import hashlib,json;from pathlib import Path;p=Path('+repr(DEST)+');print(json.dumps({n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in '+repr(names)+'}))'
    return json.loads(command('docker', 'exec', NAME, 'python', '-c', code))


def protected():
    return {name: [inspect(name)['Id'], inspect(name)['State']['StartedAt']]
            for name in ('monitoring-vm', 'monitoring-vmagent', 'monitoring-perses')}


def get(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:18430'+path, timeout=12) as response:
        return json.load(response)


def prepare():
    assert not (ROOT/'prepared.json').exists(), 'Preserve previous preparation'
    manifest = read('manifest.json')
    old = inspect(NAME)
    assert old['Id'] == manifest['before_id'] and old['Image'] == manifest['before_image']
    assert old['State']['Running'] and old['HostConfig']['NetworkMode'] == 'host'
    assert old['Config']['Labels'].get('monitoring.owner') == 'independent'
    assert live_hashes() == manifest['before_hashes'], 'Live code changed'
    assert not command('docker', 'ps', '-aq', '--filter', 'name=^/'+BACKUP+'$').strip()
    assert all(not DEST.startswith(m['Destination'].rstrip('/')+'/') for m in old['Mounts'])
    for name, expected in manifest['files'].items():
        assert sha(ROOT/name) == expected, ('Upload differs', name)
    save('container-before.json', old)
    baseline = get('/health')
    save('health-before.json', baseline)
    save('protected-before.json', protected())
    command('docker', 'cp', NAME+':'+DEST+'api.py', str(ROOT/'api-before.py'))
    plan = []
    for source, target in [('api.py', RELEASE/'monitoring/api.py'),
                           ('access.py', RELEASE/'monitoring/access.py'),
                           ('query_client.py', RELEASE/'monitoring/query_client.py'),
                           ('start.py', RELEASE/'deploy/start.py'),
                           ('start_test4.py', RELEASE/'deploy/start_test4.py')]:
        assert target.parent.is_dir()
        plan.append({'source': source, 'path': str(target), 'before_sha256': sha(target),
                     'before_content': base64.b64encode(target.read_bytes()).decode() if target.exists() else None})
    save('runtime-plan.json', plan)
    build = ROOT/'build'
    build.mkdir(mode=0o700)
    for name in ('api.py', 'access.py'): shutil.copy2(ROOT/name, build/name)
    (build/'Dockerfile').write_text('FROM '+old['Image']+'\nCOPY api.py access.py '+DEST+'\n')
    with (ROOT/'build.log').open('w') as log:
        subprocess.run(['docker', 'build', '--network=none', '-t', TAG, str(build)],
            stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    image = json.loads(command('docker', 'image', 'inspect', TAG))[0]['Id']
    candidate = command('docker', 'run', '--rm', '--network=host', '--cpus=2', '--memory=512m',
        '-e', 'ALLOWED_CLIENTS='+PEERS, '-e', 'STATE_DIR=/tmp/review-candidate-state',
        '-v', str(ROOT/'candidate.py')+':/candidate.py:ro', '--entrypoint=python', image,
        '/candidate.py', timeout=90)
    result = json.loads(candidate)
    assert result['passed'] and result['production_writes'] == 0
    save('candidate.json', result)
    save('prepared.json', {'old_id': old['Id'], 'image': image, 'protected': protected()})
    print(json.dumps({'prepared': True, 'image': image, 'candidate': result}), flush=True)


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def create(config):
    connection = Connection('localhost', timeout=30)
    try:
        connection.request('POST', '/v1.39/containers/create?name='+NAME,
                           json.dumps(config), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 201, ('Container creation failed', response.status)
        return json.loads(body)['Id']
    finally:
        connection.close()


def sync_runtime():
    manifest = read('manifest.json')
    for item in read('runtime-plan.json'):
        path = Path(item['path'])
        after = manifest['files'][item['source']]
        assert sha(path) in (item['before_sha256'], after), ('Concurrent runtime edit', str(path))
        if sha(path) == after: continue
        temporary = path.with_name(path.name+'.review-fixes.tmp')
        with temporary.open('xb') as output: output.write((ROOT/item['source']).read_bytes())
        temporary.chmod(path.stat().st_mode if path.exists() else 0o644)
        temporary.replace(path)
    save('runtime-synced.json', {'passed': True, 'at': time.time()})


def switch():
    assert not (ROOT/'transaction.json').exists(), 'Inspect existing transaction; do not restart it'
    manifest, prepared, old = read('manifest.json'), read('prepared.json'), read('container-before.json')
    assert inspect(NAME)['Id'] == old['Id'] and live_hashes() == manifest['before_hashes']
    assert protected() == prepared['protected']
    for item in read('runtime-plan.json'):
        assert sha(Path(item['path'])) == item['before_sha256'], 'Concurrent runtime edit'
    baseline = get('/health')
    assert all(v['error'] is None and 0 <= time.time()-v['processed_at'] < 20 for v in baseline['environments'].values())
    save('switch-baseline.json', baseline)
    fields = ('User', 'ExposedPorts', 'Env', 'Cmd', 'Healthcheck', 'Volumes', 'WorkingDir',
              'Entrypoint', 'Labels', 'StopSignal', 'StopTimeout', 'Hostname')
    config = copy.deepcopy({k: old['Config'][k] for k in fields if k in old['Config']})
    config.update(Image=prepared['image'], HostConfig=copy.deepcopy(old['HostConfig']))
    config['Env'] = [v for v in config.get('Env', []) if not v.startswith('ALLOWED_CLIENTS=')]+['ALLOWED_CLIENTS='+PEERS]
    config.setdefault('Labels', {})['monitoring.transaction'] = BACKUP
    assert '--no-proxy-headers' in config['Cmd']
    save('transaction.json', {'old_id': old['Id'], 'image': prepared['image'], 'started_at': time.time(), 'backup': BACKUP})
    command('docker', 'stop', old['Id'])
    command('docker', 'rename', old['Id'], BACKUP)
    identity = create(config)
    command('docker', 'start', identity)
    save('started.json', {'id': identity, 'image': prepared['image'], 'at': time.time()})
    sync_runtime()
    print(json.dumps({'started': True, 'id': identity, 'image': prepared['image'], 'allowed_clients': PEERS}), flush=True)


def verify():
    prepared, started, baseline = read('prepared.json'), read('started.json'), read('switch-baseline.json')
    current = inspect(NAME)
    assert current['Id'] == started['id'] and current['Image'] == prepared['image']
    assert current['State']['Running'] and current['RestartCount'] == 0
    assert protected() == prepared['protected']
    assert 'ALLOWED_CLIENTS='+PEERS in current['Config']['Env']
    hashes = live_hashes()
    assert hashes['api.py'] == read('manifest.json')['files']['api.py']
    health = get('/health')
    for environment, value in health['environments'].items():
        assert value['error'] is None and 0 <= time.time()-value['processed_at'] < 20
        for role, before in baseline['environments'][environment]['sources'].items():
            if before == 'ok': assert value['sources'][role] == 'ok', (environment, role)
        assert value['status'] == ('ok' if all(v == 'ok' for v in value['sources'].values()) else 'degraded')
    assert health['status'] == ('ok' if all(v['status'] == 'ok' for v in health['environments'].values()) else 'degraded')
    assert health['perses_acceleration']['state_error'] is None
    results = {}
    end = int((time.time()-120)//5)*5
    for environment in ('dcu-pd', 'a3-vllm', 'xpu-pd'):
        latest = get('/api/monitoring/latest?environment='+environment)
        latest_age = time.time()-latest['ts']
        assert 0 <= latest_age < 20
        queries = {}
        for hours in (1, 24):
            path = '/api/monitoring/history?'+urllib.parse.urlencode({
                'environment': environment, 'hours': hours, 'start': end-hours*3600, 'end': end, 'view': 'summary'})
            beginning = time.monotonic()
            data = get(path)
            assert data['points'] and data['environment'] == environment
            queries[str(hours)] = {'points': len(data['points']), 'seconds': time.monotonic()-beginning}
        results[environment] = {'latest_age': latest_age, 'sources': health['environments'][environment]['sources'], 'history': queries}
    save('verified.json', {'passed': True, 'at': time.time(), 'health': health, 'results': results,
                          'protected_unchanged': True, 'allowed_clients': PEERS, 'image': prepared['image']})
    print(json.dumps({'passed': True, 'health': health['status'], 'results': results}, ensure_ascii=False), flush=True)


def cleanup():
    assert read('verified.json')['passed']
    old, started = read('container-before.json'), read('started.json')
    assert inspect(NAME)['Id'] == started['id']
    assert inspect(BACKUP)['Id'] == old['Id'] and not inspect(BACKUP)['State']['Running']
    command('docker', 'rm', old['Id'])
    save('complete.json', {'passed': True, 'at': time.time(), 'removed_old_container': old['Id'], 'rollback': False})
    print('Old stopped container removed after acceptance; no rollback', flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'switch', 'verify', 'sync_runtime', 'cleanup'])
    action = parser.parse_args().action
    try:
        globals()[action]()
    except BaseException as error:
        save('failure-'+action+'.json', {'at': time.time(), 'error': str(error), 'type': type(error).__name__, 'recovery': 'fix_forward', 'rollback': False})
        if hasattr(error, 'add_note'):
            error.add_note('Preserve failed deployment state and fix forward; rollback is disabled.')
        raise
