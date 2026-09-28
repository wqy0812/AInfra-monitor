"""Bounded monitoring-api rollout. Execute on test4 through SSH MCP only."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from replace import cmd, create, find, inspect, restore

ROOT = Path(__file__).resolve().parent
NAME = 'monitoring-api'
TAG = 'monitoring-api:time-navigation-20260925'
BACKUP = 'monitoring-api-before-time-navigation-20260925'
DEST = '/monitoring/monitoring/api.py'
ENVS = ('dcu-pd', 'a3-vllm', 'xpu-pd')


def save(name, data):
    (ROOT / name).write_text(json.dumps(data, indent=2) + '\n')


def read(name):
    return json.loads((ROOT / name).read_text())


def source_hash(name):
    return cmd('docker', 'exec', name, 'python', '-c',
               'import hashlib;from pathlib import Path;print(hashlib.sha256(Path(' + repr(DEST) + ').read_bytes()).hexdigest())').strip()


def protected():
    return {n: [inspect(n)['Id'], inspect(n)['State']['StartedAt']]
            for n in ('monitoring-vm', 'monitoring-vmagent', 'monitoring-perses')}


def get(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:18430' + path, timeout=15) as response:
        return json.load(response)


def prepare():
    manifest = read('manifest.json')
    assert not (ROOT / 'prepared.json').exists(), 'Already prepared'
    assert find(BACKUP) is None, 'Backup already exists'
    old = inspect(NAME)
    assert old['Image'] == manifest['before_image'] and old['State']['Running']
    assert source_hash(NAME) == manifest['before_sha256'], 'Live source changed'
    assert hashlib.sha256((ROOT / 'api.py').read_bytes()).hexdigest() == manifest['after_sha256']
    assert old['HostConfig']['NetworkMode'] == 'host'
    assert not any(DEST == m['Destination'] or DEST.startswith(m['Destination'].rstrip('/') + '/') for m in old['Mounts'])
    assert not any(line[2:] == DEST for line in cmd('docker', 'diff', NAME).splitlines())
    save('container-before.json', old)
    save('health-before.json', get('/health'))
    (ROOT / 'Dockerfile').write_text('FROM ' + old['Image'] + '\nCOPY api.py ' + DEST + '\n')
    with (ROOT / 'build.log').open('w') as log:
        subprocess.run(['docker', 'build', '--network=none', '-t', TAG, str(ROOT)], stdout=log, stderr=subprocess.STDOUT, check=True)
    cmd('docker', 'run', '--rm', '--network=none', '--entrypoint=python', TAG, '-c', 'import monitoring.api')
    image = json.loads(cmd('docker', 'image', 'inspect', TAG))[0]['Id']
    save('prepared.json', {'old_id': old['Id'], 'image': image, 'protected': protected()})
    print('API image prepared', flush=True)


def summary(value):
    value = copy.deepcopy(value)
    for point in value['points']:
        for obj in [*point['nodes'].values(), point.get('mooncake', {})]:
            obj.pop('resources', None)
            if 'gap_before' in obj:
                obj['gap_before'] = [k for k in obj['gap_before'] if not k.startswith('resources.')]
    return value


def verify():
    prepared, manifest = read('prepared.json'), read('manifest.json')
    assert source_hash(NAME) == manifest['after_sha256']
    assert protected() == prepared['protected'], 'Unrelated services changed'
    current = inspect(NAME)
    assert current['Image'] == prepared['image'] and current['State']['Running'] and current['RestartCount'] == 0
    end = int((time.time() - 120) // 5) * 5
    results = {}
    for env in ENVS:
        path = '/api/monitoring/history?' + urllib.parse.urlencode({'environment': env, 'hours': 1, 'start': end - 3600, 'end': end})
        full = get(path)
        started = time.monotonic()
        reduced = get(path + '&view=summary')
        cold = time.monotonic() - started
        started = time.monotonic()
        warm = get(path + '&view=summary')
        warm_seconds = time.monotonic() - started
        assert reduced['points'] and reduced == summary(full) and warm == reduced, env
        results[env] = {'points': len(reduced['points']), 'cold_seconds': cold, 'warm_seconds': warm_seconds, 'full_summary_match': True}
    return results


def switch():
    old, prepared, manifest = read('container-before.json'), read('prepared.json'), read('manifest.json')
    candidate = read('candidate-api.json')
    assert candidate['passed'] and candidate['image'] == prepared['image']
    assert inspect(NAME)['Id'] == old['Id'] and source_hash(NAME) == manifest['before_sha256']
    assert find(BACKUP) is None and protected() == prepared['protected']
    baseline = get('/health')
    assert baseline['status'] == 'ok'
    fields = ('User', 'ExposedPorts', 'Env', 'Cmd', 'Healthcheck', 'Volumes', 'WorkingDir', 'Entrypoint', 'Labels', 'StopSignal', 'StopTimeout', 'Hostname')
    config = copy.deepcopy({k: old['Config'][k] for k in fields if k in old['Config']})
    config.update(Image=prepared['image'], HostConfig=copy.deepcopy(old['HostConfig']))
    config.setdefault('Labels', {})['monitoring.transaction'] = BACKUP
    save('transaction.json', {'old_id': old['Id'], 'new_image': prepared['image'], 'backup': BACKUP})
    try:
        cmd('docker', 'stop', old['Id'])
        cmd('docker', 'rename', old['Id'], BACKUP)
        identity = create(NAME, config)
        cmd('docker', 'start', identity)
        deadline = time.monotonic() + 90
        stable = None
        while True:
            try:
                state, health = inspect(NAME), get('/health')
                assert state['Id'] == identity and state['State']['Running'] and state['RestartCount'] == 0
                assert health['status'] == 'ok'
                for env in ENVS:
                    actual = health['environments'][env]
                    assert actual['error'] is None and 0 <= time.time() - actual['processed_at'] < 30
                    for source, status in baseline['environments'][env]['sources'].items():
                        if status == 'ok':
                            assert actual['sources'][source] == 'ok', (env, source)
                stable = time.monotonic() if stable is None else stable
                if time.monotonic() - stable >= 10:
                    break
            except (OSError, ValueError, KeyError, AssertionError):
                stable = None
            if time.monotonic() >= deadline:
                raise RuntimeError('API did not reach stable health')
            time.sleep(1)
        results = verify()
        save('complete.json', {'passed': True, 'image': prepared['image'], 'results': results, 'rollback_container': BACKUP, 'other_containers_unchanged': True})
        print(json.dumps(results), flush=True)
    except BaseException as error:
        error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
        raise


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'switch', 'verify', 'rollback'])
    action = parser.parse_args().action
    if action == 'rollback':
        restore(NAME, read('container-before.json'), BACKUP, read('prepared.json')['image'])
    elif action == 'verify':
        print(json.dumps(verify()))
    else:
        {'prepare': prepare, 'switch': switch}[action]()
