"""Incremental A3 collection repair. Run only through SSH MCP on test4; no rollback."""
import copy
import datetime
import hashlib
import http.client
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = 'monitoring-api'
TAG = 'monitoring-api:a3-coverage-20260928'
BACKUP = 'monitoring-api-before-a3-coverage-20260928'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(*args):
    return subprocess.check_output(args).decode().strip()


def save(name, data):
    path = ROOT / name
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def read(name):
    return json.loads((ROOT / name).read_text())


def get(url):
    with OPENER.open(url, timeout=30) as response:
        return json.load(response)


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def protected():
    return {n: [inspect(n)['Id'], inspect(n)['State']['StartedAt']]
            for n in ('monitoring-vm', 'monitoring-vmagent', 'monitoring-perses')}


def prepare():
    assert not (ROOT / 'prepared.json').exists()
    old = inspect(NAME)
    assert old['State']['Running'] and old['HostConfig']['NetworkMode'] == 'host'
    before = {'a3.py': 'f1477f68ec98a2e92cf420d66e0d5a3bfb7172da9b37f35694c811cd2ad371bb',
              'api.py': 'fff3f5e8d74458afd3f77717a9809781a68f52cc7d53e8323f7ebdb1068d48af'}
    for name, digest in before.items():
        assert run('docker', 'exec', NAME, 'sha256sum', '/monitoring/monitoring/' + name).split()[0] == digest
    save('container-before.json', old)
    save('health-before.json', get('http://127.0.0.1:18430/health'))
    (ROOT / 'Dockerfile').write_text('FROM ' + old['Image'] + '\nCOPY a3.py api.py /monitoring/monitoring/\n')
    with (ROOT / 'build.log').open('w') as log:
        subprocess.run(['docker', 'build', '--network=none', '-t', TAG, str(ROOT)], stdout=log, stderr=subprocess.STDOUT, check=True)
    run('docker', 'run', '--rm', '--network=none', '--entrypoint=python', TAG, '-c',
        'from monitoring import a3,api; assert a3.INSTANCE_COUNTS == {"prefill":4,"decode":16}')
    save('prepared.json', {'old_id': old['Id'], 'image': inspect(TAG)['Id'], 'protected': protected(),
         'files': {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in before}})


def apply_api():
    prepared = read('prepared.json')
    old = inspect(NAME)
    assert old['Id'] == prepared['old_id'] and protected() == prepared['protected']
    assert not run('docker', 'ps', '-aq', '--filter', 'name=^/' + BACKUP + '$')
    assert inspect(TAG)['Id'] == prepared['image']
    fields = ('User', 'ExposedPorts', 'Env', 'Cmd', 'Healthcheck', 'Volumes', 'WorkingDir', 'Entrypoint', 'Labels', 'StopSignal', 'StopTimeout')
    config = copy.deepcopy({k: old['Config'][k] for k in fields if k in old['Config']})
    config.update(Image=prepared['image'], HostConfig=copy.deepcopy(old['HostConfig']))
    config.setdefault('Labels', {})['monitoring.transaction'] = BACKUP
    run('docker', 'stop', old['Id'])
    run('docker', 'rename', old['Id'], BACKUP)
    import socket
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect('/var/run/docker.sock')
    connection = Connection('localhost', timeout=30)
    connection.request('POST', '/v1.39/containers/create?name=' + NAME, json.dumps(config), {'Content-Type': 'application/json'})
    response = connection.getresponse(); body = response.read()
    assert response.status == 201, body
    identity = json.loads(body)['Id']; connection.close()
    run('docker', 'start', identity)
    save('api-applied.json', {'at': time.time(), 'id': identity, 'image': prepared['image']})


def apply_scrape():
    assert inspect(NAME)['Id'] == read('api-applied.json')['id']
    latest = get('http://127.0.0.1:18430/api/monitoring/latest?environment=a3-vllm')
    assert latest['nodes']['decode']['metrics']['data']['expected_instances'] == 16
    path = Path('/data2/monitoring/release/deploy/scrape.yml')
    old = path.read_text()
    before = "['122.209.21.25:7100', '122.209.21.25:7101', '122.209.21.25:7102', '122.209.21.25:7103']"
    after = '[' + ', '.join(repr('122.209.21.25:' + str(p)) for p in range(7100, 7116)) + ']'
    assert old.count(before) == 1
    new = old.replace(before, after)
    (ROOT / 'scrape-before.yml').write_text(old)
    (ROOT / 'scrape-candidate.yml').write_text(new)
    agent = inspect('monitoring-vmagent')
    run('docker', 'run', '--rm', '--network=none', '--entrypoint', agent['Path'], '-v', str(ROOT / 'scrape-candidate.yml') + ':/candidate.yml:ro',
        '-v', '/data2/monitoring/release/deploy:/config:ro',
        agent['Image'], '-promscrape.config=/candidate.yml', '-promscrape.config.dryRun')
    assert path.read_text() == old
    temp = path.with_suffix('.a3.tmp'); temp.write_text(new); temp.replace(path)
    # The running agent retains all unrelated targets and credentials.
    with OPENER.open('http://127.0.0.1:18429/-/reload', timeout=15) as response: assert response.status == 200
    save('scrape-applied.json', {'at': time.time(), 'targets': 20})


def mark_ready():
    latest = get('http://127.0.0.1:18430/api/monitoring/latest?environment=a3-vllm')
    assert time.time() - latest['ts'] < 20
    for role, count in [('prefill', 4), ('decode', 16)]:
        m = latest['nodes'][role]['metrics']; data = m['data']
        assert m['status'] == 'ok' and data['expected_instances'] == data['available_instances'] == count
        assert data['rates']['decode_tokens'] is not None
        assert len(data['resources']['queue']) == count * 2
    marker = {'incomplete_confirmed_at': 1790575414, 'repair_complete_at': latest['ts'],
              'scrape_reloaded_at': read('scrape-applied.json')['at']}
    path = Path('/data2/monitoring/state/a3-collection-coverage.json')
    assert not path.exists(), 'Do not change a previously recorded boundary'
    path.write_text(json.dumps(marker) + '\n')
    save('ready.json', {'at': time.time(), 'marker': marker, 'latest': latest})


def dashboards():
    from a3_coverage import annotate
    base = 'http://122.247.53.162:18431'
    login = urllib.request.Request(base + '/api/auth/providers/native/login', data=Path('/data2/monitoring/perses/admin-credentials.json').read_bytes(), headers={'Content-Type': 'application/json'})
    with OPENER.open(login, timeout=20) as response: token = json.load(response)['access_token']
    def api(path, data=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
              method='PUT' if data is not None else 'GET', headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with OPENER.open(req, timeout=30) as response:return json.load(response)
    path = '/api/v1/projects/a3-monitoring/dashboards'
    before = api(path); save('dashboards-before.json', before)
    ts = read('ready.json')['marker']['repair_complete_at']
    stamp = datetime.datetime.fromtimestamp(ts, datetime.timezone(datetime.timedelta(hours=8))).isoformat()
    changed = []
    for old in before:
        new = annotate(old, stamp)
        if old == new:continue
        endpoint = path + '/' + old['metadata']['name']
        assert api(endpoint)['spec'] == old['spec'], 'Concurrent dashboard edit'
        api(endpoint, new)
        assert api(endpoint)['spec'] == new['spec']
        changed.append(new['metadata']['name'])
    save('dashboards-after.json', api(path))
    save('dashboards-applied.json', {'at': time.time(), 'changed': changed, 'repair_time': stamp})


def runtime():
    from a3_coverage import annotate
    root = Path('/data2/monitoring/perses/release')
    if (ROOT / 'generator-journal.json').exists():
        journal = read('generator-journal.json')
        assert all(Path(e['path']).read_text() == e['after'] for e in journal), 'Concurrent generator change'
        save('runtime-applied.json', {'files': [e['path'] for e in journal], 'at': time.time()})
        return
    stamp = read('dashboards-applied.json')['repair_time']
    files = {root / 'a3_coverage.py': (ROOT / 'a3_coverage.py').read_text()}
    path = root / 'project_split.py'; old = path.read_text()
    needle = '    resources = build(source)\n    validate(resources)'
    assert old.count(needle) == 1
    files[path] = old.replace(needle, '    resources = build(source)\n    from a3_coverage import annotate\n    resources["dashboards"] = [annotate(d) for d in resources["dashboards"]]\n    validate(resources)')
    for path in (root / 'projects/a3-monitoring/dashboards').glob('*.json'):
        before = json.loads(path.read_text()); after = annotate(before, stamp)
        if before != after:files[path] = json.dumps(after, ensure_ascii=False, indent=2) + '\n'
    journal = [{'path': str(path), 'before': path.read_text() if path.exists() else None, 'after': content} for path, content in files.items()]
    save('generator-journal.json', journal)
    for entry in journal:
        path = Path(entry['path'])
        assert (path.read_text() if path.exists() else None) == entry['before'], 'Concurrent generator change'
        path.write_text(entry['after'])
    save('runtime-applied.json', {'files': [str(p) for p in files], 'at': time.time()})


if __name__ == '__main__':
    try:
        {'prepare': prepare, 'api': apply_api, 'scrape': apply_scrape, 'ready': mark_ready, 'dashboards': dashboards, 'runtime': runtime}[sys.argv[1]]()
        print(json.dumps({'step': sys.argv[1], 'ok': True}))
    except BaseException as error:
        save('failure-' + sys.argv[1] + '.json', {'at': time.time(), 'error': str(error), 'recovery': 'fix_forward', 'automatic_rollback': False})
        raise
