"""Run on test4 through SSH MCP. Transactionally switch API and latency queries."""
import copy
import hashlib
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(sys.argv[1])
ACTION = sys.argv[2]
NAME = 'monitoring-api'
TAG = 'monitoring-api:latency-v2-20260914-r3'
DASH = 'http://122.247.53.162:18431/api/v1/projects/dcu-monitoring/dashboards/overview'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cmd(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT).decode()


def inspect(name):
    return json.loads(cmd('docker', 'inspect', name))[0]


def save(name, value):
    path = ROOT / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def get(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'Content-Type': 'application/json'}, method='PUT' if data is not None else 'GET')
    with OPENER.open(req, timeout=15) as response:
        return json.load(response)


def protected():
    values = json.loads(cmd('docker', 'inspect', *cmd('docker', 'ps', '-q').split()))
    return {v['Name']: dict({k: v['State'][k] for k in ('Pid', 'StartedAt')}, Id=v['Id'])
            for v in values if v['Name'] != '/' + NAME and not v['Name'].startswith('/monitoring-latency-')}


def check_protected():
    before = json.loads((ROOT / 'baseline.json').read_text())
    expected = {v['Name']: dict({k: v['State'][k] for k in ('Pid', 'StartedAt')}, Id=v['Id'])
                for v in before if v['Name'] != '/' + NAME}
    assert protected() == expected, 'Protected containers changed'


def update_spec(spec):
    spec = copy.deepcopy(spec)
    count = 0
    for panel in spec['panels'].values():
        for query in panel['spec'].get('queries', []):
            target = query['spec']['plugin']['spec']
            text = target.get('query', '')
            if '.percentiles.' in text and 'environment="dcu-pd"' in text:
                assert 'schema="v1"' in text or 'schema="latency-v2"' in text
                target['query'] = text.replace('schema="v1"', 'schema="latency-v2"')
                count += 1
    assert count == 9, 'Unexpected dashboard latency query layout'
    return spec


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')


def rollback():
    tx = json.loads((ROOT / 'transaction.json').read_text())
    if (ROOT / 'dashboard-before.json').exists():
        old = json.loads((ROOT / 'dashboard-before.json').read_text())
        live = get(DASH)
        assert live['spec'] in (old['spec'], update_spec(old['spec'])), 'Dashboard independently edited; cannot replace its spec'
        live['spec'] = old['spec']
        get(DASH, live)
        assert get(DASH)['spec'] == old['spec']
    if cmd('docker', 'ps', '-aq', '--filter', 'name=^/' + NAME + '$').strip():
        v = inspect(NAME)
        if v['Id'] != tx['old_id']:
            assert v['Image'] == tx['new_image']
            cmd('docker', 'rm', '-f', NAME)
            cmd('docker', 'rename', tx['backup'], NAME)
    else:
        cmd('docker', 'rename', tx['backup'], NAME)
    if not inspect(NAME)['State']['Running']:
        cmd('docker', 'start', NAME)
    save('rollback.json', {'at': time.time(), 'id': inspect(NAME)['Id']})


def switch():
    assert json.loads((ROOT / 'candidate-acceptance.json').read_text())['passed']
    history = json.loads((ROOT / 'historical.json').read_text())
    assert history['watermark'] == history['end']
    assert not (ROOT / 'transaction.json').exists()
    check_protected()
    v = inspect(NAME)
    baseline = next(x for x in json.loads((ROOT / 'baseline.json').read_text()) if x['Name'] == '/' + NAME)
    assert v['Id'] == baseline['Id']
    new_image = inspect(TAG)['Id']
    backup = NAME + '-latency-rollback-' + str(int(time.time()))
    save('transaction.json', {'old_id': v['Id'], 'backup': backup, 'new_image': new_image, 'at': time.time()})
    save('dashboard-before.json', get(DASH))
    fields = ('User', 'ExposedPorts', 'Env', 'Cmd', 'Healthcheck', 'Volumes', 'WorkingDir', 'Entrypoint', 'Labels', 'StopSignal', 'StopTimeout', 'Hostname')
    config = {k: v['Config'][k] for k in fields if k in v['Config']}
    config.update(Image=new_image, HostConfig=v['HostConfig'])
    try:
        cmd('docker', 'stop', '--time', '30', NAME)
        # Capture the precise original producer cutoff without changing its cursor.
        save('handoff.json', json.loads(Path('/data2/monitoring/state/watermark.json').read_text()))
        cmd('docker', 'rename', NAME, backup)
        c = Connection('localhost', timeout=30)
        c.request('POST', '/v1.39/containers/create?name=' + NAME, json.dumps(config), {'Content-Type': 'application/json'})
        r = c.getresponse(); body = r.read(); c.close()
        assert r.status == 201, body
        cmd('docker', 'start', NAME)
        deadline = time.time() + 60
        while True:
            try:
                h = get('http://127.0.0.1:18430/health')
                assert h['status'] == 'ok', h
                for env in ('dcu-pd', 'a3-vllm'):
                    data = get('http://127.0.0.1:18430/api/monitoring/latest?environment=' + env)
                    assert data['environment'] == env and time.time() - data['ts'] < 20
                    assert all(n['metrics']['status'] == 'ok' for n in data['nodes'].values())
                break
            except Exception:
                if time.time() > deadline: raise
                time.sleep(2)
        old = json.loads((ROOT / 'dashboard-before.json').read_text())
        live = get(DASH)
        assert live['spec'] == old['spec'], 'Dashboard changed during API replacement'
        live['spec'] = update_spec(old['spec'])
        save('dashboard-after.json', get(DASH, live))
        assert get(DASH)['spec'] == live['spec']
        check_protected()
        save('switched.json', {'at': time.time(), 'id': inspect(NAME)['Id'], 'image': new_image, 'protected_unchanged': True})
    except BaseException:
        rollback()
        raise


if __name__ == '__main__':
    {'switch': switch, 'rollback': rollback, 'check': check_protected}[ACTION]()
