"""Publish only the reviewed CPU materialization and two panels via SSH MCP."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.parse
import urllib.request

import local_latest_release as release
from host_cpu_panel import extend

release.ROOT = Path('/data2/monitoring/releases/host-cpu-20260916-v1')
release.TAG = 'monitoring-api:host-cpu-20260916-v1'
ROOT = release.ROOT
BASE = 'http://122.247.53.162:18431'
IDENTITY = 'a3-monitoring/dashboards/a3-hosts'
ENDPOINT = '/api/v1/projects/' + IDENTITY
FILES = {'api.py', 'host_cpu.py'}
PANELS = ('p0', 'extra-iowait')
TOKEN = None


def http(path, method='GET', obj=None):
    global TOKEN
    if TOKEN is None:
        if os.environ.get('PERSES_PASSWORD'):
            login = urllib.request.Request(BASE + '/api/auth/providers/native/login',
                data=json.dumps({'login': 'admin', 'password': os.environ['PERSES_PASSWORD']}).encode(),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(login, timeout=15) as response:
                TOKEN = json.load(response)['access_token']
        else:
            TOKEN = (ROOT / 'perses-token').read_text()
    headers = {'Authorization': 'Bearer ' + TOKEN}
    if obj is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(BASE + path, data=json.dumps(obj).encode() if obj is not None else None,
                                     headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def sources(container='monitoring-api'):
    code = ('import pathlib,hashlib,json;print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() '
            'for p in pathlib.Path("/monitoring/monitoring").glob("*.py")}))')
    return json.loads(release.command('docker', 'exec', container, 'python', '-c', code))


def check_resources(after=False):
    before = release.read('resources-before.json')
    expected = extend(before[IDENTITY]) if after else before[IDENTITY]
    current = {}
    for project in ('a3-monitoring', 'dcu-monitoring'):
        for kind in ('dashboards', 'datasources'):
            for document in http('/api/v1/projects/' + project + '/' + kind):
                current[project + '/' + kind + '/' + document['metadata']['name']] = document
    assert set(current) == set(before)
    for key, old in before.items():
        assert current[key]['spec'] == (expected['spec'] if key == IDENTITY else old['spec']), key
    actual = {str(p.relative_to('/data2/monitoring/perses/data')): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in Path('/data2/monitoring/perses/data').rglob('*.json')}
    old_files = release.read('perses-files-before.json')
    assert set(actual) == set(old_files)
    changed = [p for p in old_files if actual[p] != old_files[p]]
    assert changed == (['dashboards/a3-monitoring/a3-hosts.json'] if after else []), changed
    assert hashlib.sha256(Path('/data2/monitoring/perses/config.yaml').read_bytes()).hexdigest() == release.read('perses-config-sha256.json')['sha256']
    return len(current)


def prepare():
    assert not (ROOT / 'prepared.json').exists()
    release.check_protected()
    manifest = release.read('manifest.json')
    assert sources() == release.read('baseline-source.json')
    assert sources()['api.py'] == manifest['before_api_sha256']
    assert 'host_cpu.py' not in sources()
    assert {p.name for p in (ROOT / 'monitoring').glob('*.py')} == FILES
    assert release.hashes() == {'monitoring/' + k: v for k, v in manifest['after'].items()}
    old = release.inspect('monitoring-api')
    assert not any(m['Destination'].startswith('/monitoring') for m in old['Mounts'])
    assert not any(line[2:].startswith('/monitoring/monitoring/') and line.endswith('.py')
                   for line in release.command('docker', 'diff', 'monitoring-api').splitlines())
    check_resources()
    (ROOT / 'Dockerfile').write_text('FROM ' + old['Image'] + '\nCOPY monitoring/ /monitoring/monitoring/\n')
    import subprocess
    with (ROOT / 'build.log').open('w') as log:
        subprocess.run(['docker', 'build', '-t', release.TAG, str(ROOT)], stdout=log, stderr=subprocess.STDOUT, check=True)
    output = release.command('docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', release.TAG,
        '-c', 'import monitoring.api,monitoring.host_cpu;assert monitoring.host_cpu.SCHEMA=="host-cpu-v1";print("imports OK")')
    release.save('prepared.json', {'image': release.inspect(release.TAG)['Id'], 'tag': release.TAG, 'imports': output})
    print(json.dumps(release.read('prepared.json')))


def candidate():
    output = release.command('docker', 'run', '--rm', '--network', 'host', '--cpus', '1', '--memory', '512m',
        '-e', 'VM_URL=http://127.0.0.1:18428', '-e', 'STATE_DIR=/tmp/host-cpu-check-state', '-e', 'PYTHONPATH=/monitoring',
        '-v', str(ROOT) + ':/release-checks:ro', '--entrypoint', 'python', release.TAG,
        '/release-checks/check_host_cpu_candidate.py')
    result = json.loads(output)
    assert result['passed'] and result['production_imports'] == 0
    release.save('candidate-acceptance.json', result)
    print(output)


def api_check():
    release.check_protected()
    assert sources() == {**release.read('baseline-source.json'), **release.read('manifest.json')['after']}
    health = release.get('/health')
    assert health['status'] == 'ok'
    assert health['environments']['a3-vllm']['host_cpu']['query_status'] == 'ok'
    assert set(health['environments']['a3-vllm']['host_cpu']['sources'].values()) == {'ok'}
    point = release.get('/api/monitoring/latest?environment=a3-vllm')
    assert time.time() - point['ts'] < 20
    assert all(n['host_cpu']['status'] == 'ok' for n in point['nodes'].values())
    return health, point


def publish_panels():
    api_check()
    check_resources()
    old = http(ENDPOINT)
    assert old == release.read('resources-before.json')[IDENTITY], 'Concurrent dashboard edit'
    candidate = extend(old)
    release.save('panel-transaction.json', {'before': old, 'candidate': candidate, 'at': time.time()})
    try:
        http(ENDPOINT, 'PUT', candidate)
        count = check_resources(after=True)
        release.save('panels-published.json', {'at': time.time(), 'resources_checked': count, 'panels': PANELS})
        print(json.dumps(release.read('panels-published.json')))
    except BaseException as error:
        error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
        raise


def rollback_panels():
    if not (ROOT / 'panel-transaction.json').exists():
        return
    tx = release.read('panel-transaction.json')
    current = http(ENDPOINT)
    if current['spec'] == tx['before']['spec']:
        return
    for panel in PANELS:
        assert current['spec']['panels'][panel] == tx['candidate']['spec']['panels'][panel], 'Concurrent CPU panel edit'
    payload = copy.deepcopy(current)
    for panel in PANELS:
        payload['spec']['panels'][panel] = tx['before']['spec']['panels'][panel]
    http(ENDPOINT, 'PUT', payload)
    assert http(ENDPOINT)['spec'] == payload['spec']
    release.save('panels-rollback.json', {'at': time.time()})


def finalize():
    api_check()
    check_resources(after=True)
    assert release.read('acceptance.json')['passed']
    target = Path('/data2/monitoring/release/monitoring')
    for name in FILES:
        if (target / name).exists():
            shutil.copy2(target / name, ROOT / ('release-before-' + name))
        shutil.copy2(ROOT / 'monitoring' / name, target / name)
    # Patch the current server generator, without replacing it with a newer
    # local snapshot that may contain unrelated unpublished changes.
    generator = Path('/data2/monitoring/perses/release/project_split.py')
    builder = generator.parent / 'host_cpu_panel.py'
    old = '            elif name in ("hosts-dcu", "a3-hosts"):\n                host_additions(d)\n'
    new = old + '                if name == "a3-hosts":\n                    from host_cpu_panel import extend as materialized_cpu\n                    d = materialized_cpu(d)\n'
    original = generator.read_text()
    assert original.count(old) == 1 and 'materialized_cpu' not in original
    assert not builder.exists()
    shutil.copy2(generator, ROOT / 'project_split-before.py')
    shutil.copy2(ROOT / 'host_cpu_panel.py', builder)
    patched = original.replace(old, new)
    compile(patched, str(generator), 'exec')
    tmp = generator.with_suffix('.cpu.tmp')
    tmp.write_text(patched)
    shutil.copymode(generator, tmp)
    tmp.replace(generator)
    release.save('generator-publication.json', {'generator_sha256': hashlib.sha256(generator.read_bytes()).hexdigest(),
        'builder_sha256': hashlib.sha256(builder.read_bytes()).hexdigest()})
    release.save('complete.json', {'passed': True, 'at': time.time(), 'image': release.inspect('monitoring-api')['Image'],
        'rollback_container': release.read('transaction.json')['backup'], 'panels': PANELS, 'protected_unchanged': True})
    print(json.dumps(release.read('complete.json')))


def rollback():
    rollback_panels()
    release.rollback()
    if (ROOT / 'complete.json').exists():
        target = Path('/data2/monitoring/release/monitoring')
        for name in FILES:
            backup = ROOT / ('release-before-' + name)
            if backup.exists():
                shutil.copy2(backup, target / name)
            elif name not in release.read('baseline-source.json'):
                (target / name).unlink()
    if (ROOT / 'generator-publication.json').exists():
        publication = release.read('generator-publication.json')
        generator = Path('/data2/monitoring/perses/release/project_split.py')
        builder = generator.parent / 'host_cpu_panel.py'
        assert hashlib.sha256(generator.read_bytes()).hexdigest() == publication['generator_sha256']
        assert hashlib.sha256(builder.read_bytes()).hexdigest() == publication['builder_sha256']
        shutil.copy2(ROOT / 'project_split-before.py', generator)
        builder.unlink()


if __name__ == '__main__':
    {'prepare': prepare, 'candidate': candidate, 'switch': release.switch, 'publish': publish_panels,
     'check': api_check, 'finalize': finalize, 'rollback': rollback}[sys.argv[1]]()
