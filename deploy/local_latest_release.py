"""Run on test4 via SSH MCP; preserve failed deployments for forward repair."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.request


def command(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT).decode()


def inspect(name):
    return json.loads(command('docker', 'inspect', name))[0]


def save(name, value):
    p = ROOT / name
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    p.chmod(0o600)


def read(name):
    return json.loads((ROOT / name).read_text())


def get(path):
    with urllib.request.urlopen('http://127.0.0.1:18430' + path, timeout=20) as r:
        return json.load(r)


def protected():
    ids = command('docker', 'ps', '-q').split()
    return {v['Name']: (v['Id'], v['State']['StartedAt'], v['State']['Pid'])
            for v in json.loads(command('docker', 'inspect', *ids)) if v['Name'] != '/monitoring-api'}


def check_protected():
    expected = {v['Name']: (v['Id'], v['State']['StartedAt'], v['State']['Pid'])
                for v in read('containers-before.json') if v['Name'] != '/monitoring-api'}
    assert protected() == expected, 'Other running containers changed'


def hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / 'monitoring').glob('*.py'))}


def prepare():
    assert not (ROOT / 'prepared.json').exists()
    check_protected()
    baseline = next(c for c in read('containers-before.json') if c['Name'] == '/monitoring-api')
    assert inspect('monitoring-api')['Id'] == baseline['Id']
    assert not any(m['Destination'].startswith('/monitoring') for m in baseline['Mounts'])
    save('source-sha256.json', hashes())
    dockerfile = 'FROM ' + baseline['Image'] + '\nCOPY monitoring/ /monitoring/monitoring/\n'
    (ROOT / 'Dockerfile').write_text(dockerfile)
    with (ROOT / 'build.log').open('w') as log:
        subprocess.run(['docker', 'build', '-t', TAG, str(ROOT)], stdout=log, stderr=subprocess.STDOUT, check=True)
    smoke = ('import importlib,pkgutil,monitoring;'
             '[importlib.import_module("monitoring."+m.name) for m in pkgutil.iter_modules(monitoring.__path__)];'
             'from monitoring.request_scope import SCHEMA;assert SCHEMA=="request-streaming-v1";print("imports OK")')
    print(command('docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', TAG, '-c', smoke))
    save('prepared.json', {'image': inspect(TAG)['Id'], 'tag': TAG, 'at': time.time()})
    print(json.dumps(read('prepared.json')), flush=True)


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')


def rollback():
    tx = read('transaction.json')
    ids = command('docker', 'ps', '-aq', '--filter', 'name=^/monitoring-api$').strip()
    if ids and inspect('monitoring-api')['Id'] != tx['old_id']:
        assert inspect('monitoring-api')['Image'] == tx['new_image']
        command('docker', 'rm', '-f', 'monitoring-api')
        ids = ''
    if not ids:
        command('docker', 'rename', tx['backup'], 'monitoring-api')
    if not inspect('monitoring-api')['State']['Running']:
        command('docker', 'start', 'monitoring-api')
    save('api-rollback.json', {'at': time.time(), 'id': inspect('monitoring-api')['Id']})


def switch():
    assert not (ROOT / 'transaction.json').exists()
    assert read('candidate-acceptance.json')['passed']
    check_protected()
    old = next(c for c in read('containers-before.json') if c['Name'] == '/monitoring-api')
    assert inspect('monitoring-api')['Id'] == old['Id']
    new_image = read('prepared.json')['image']
    backup = 'monitoring-api-local-latest-rollback-' + str(int(time.time()))
    save('transaction.json', {'old_id': old['Id'], 'new_image': new_image, 'backup': backup, 'at': time.time()})
    fields = ('User', 'ExposedPorts', 'Env', 'Cmd', 'Healthcheck', 'Volumes', 'WorkingDir',
              'Entrypoint', 'Labels', 'StopSignal', 'StopTimeout', 'Hostname')
    config = {k: old['Config'][k] for k in fields if k in old['Config']}
    config.update(Image=new_image, HostConfig=old['HostConfig'])
    try:
        command('docker', 'stop', '--time', '30', 'monitoring-api')
        save('handoff.json', {p.name: json.loads(p.read_text())
                              for p in Path('/data2/monitoring/state').glob('watermark*.json')})
        command('docker', 'rename', 'monitoring-api', backup)
        c = Connection('localhost', timeout=30)
        c.request('POST', '/v1.39/containers/create?name=monitoring-api', json.dumps(config), {'Content-Type': 'application/json'})
        response = c.getresponse()
        body = response.read()
        c.close()
        assert response.status == 201, body
        command('docker', 'start', 'monitoring-api')
        deadline = time.time() + 45
        while True:
            try:
                health = get('/health')
                assert health['status'] == 'ok', health
                for env in ('dcu-pd', 'a3-vllm'):
                    point = get('/api/monitoring/latest?environment=' + env)
                    assert point['environment'] == env and time.time() - point['ts'] < 20, point['ts']
                    assert all(n['metrics']['status'] == 'ok' for n in point['nodes'].values())
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(2)
        check_protected()
        save('api-switched.json', {'at': time.time(), 'id': inspect('monitoring-api')['Id'],
                                   'image': new_image, 'health': health, 'protected_unchanged': True})
        print(json.dumps(read('api-switched.json')), flush=True)
    except BaseException as error:
        error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
        raise


def finalize():
    assert read('acceptance.json')['passed']
    check_protected()
    current = inspect('monitoring-api')
    assert current['Image'] == read('prepared.json')['image']
    expected = read('source-sha256.json')
    actual = json.loads(command('docker', 'exec', 'monitoring-api', 'python', '-c',
        'import pathlib,hashlib,json;print(json.dumps({"monitoring/"+p.name:hashlib.sha256(p.read_bytes()).hexdigest() '
        'for p in pathlib.Path("/monitoring/monitoring").glob("*.py")}))'))
    assert actual == expected, 'Container source differs from release snapshot'
    for source in (ROOT / 'monitoring').glob('*.py'):
        shutil.copy2(source, Path('/data2/monitoring/release/monitoring') / source.name)
    save('complete.json', {'passed': True, 'at': time.time(), 'image': current['Image'],
                          'source_files': len(expected), 'protected_unchanged': True,
                          'rollback_container': read('transaction.json')['backup']})
    print(json.dumps(read('complete.json')), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('action', choices=('prepare', 'switch', 'rollback', 'finalize'))
    args = parser.parse_args()
    ROOT = args.root
    TAG = 'monitoring-api:local-latest-20260915-2030'
    assert ROOT.is_dir()
    globals()[args.action]()
