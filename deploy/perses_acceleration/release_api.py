"""Shadow-worker image transaction. All server execution is through SSH MCP."""
import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from replace import cmd, create, find, inspect, restore

ROOT = Path(__file__).resolve().parents[2] / 'api-build'
NAME = 'monitoring-api'
BACKUP = 'monitoring-api-before-perses-acceleration-20260926'
TAG = 'monitoring-api:perses-acceleration-20260926'
DEST = '/monitoring/monitoring/'
FILES = ('api.py', 'perses_acceleration.py', 'perses_acceleration_catalog.json')


def save(name, value):
    path = ROOT / name
    temporary = path.with_suffix('.tmp'); temporary.write_text(json.dumps(value, indent=2) + '\n'); temporary.replace(path)


def read(name):
    return json.loads((ROOT / name).read_text())


def notice(message):
    # Completion is committed to disk. A disconnected caller must never turn a
    # successful deployment into a rollback, or prevent later independent work.
    try:
        print(message, flush=True)
    except BrokenPipeError:
        pass


def record_failure(name, error):
    try:
        save(name, {'at': time.time(), 'type': type(error).__name__, 'error': str(error)[:500]})
    except OSError:
        pass  # Failure reporting must not prevent the actual rollback.


def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:18430/health', timeout=10) as response:
        return json.load(response)


def protected():
    return {name: [inspect(name)['Id'], inspect(name)['State']['StartedAt']]
            for name in ('monitoring-vm', 'monitoring-vmagent', 'monitoring-perses')}


def source_hash(name, file='api.py'):
    return cmd('docker', 'exec', name, 'python', '-c', 'from pathlib import Path;import hashlib;print(hashlib.sha256(Path('
               + repr(DEST + file) + ').read_bytes()).hexdigest())').strip()


def prepare():
    manifest = read('manifest.json')
    assert not (ROOT / 'prepared.json').exists() and find(BACKUP) is None
    old = inspect(NAME)
    assert old['Image'] == manifest['before_image'] and old['State']['Running']
    assert source_hash(NAME) == manifest['before_sha256']
    assert old['HostConfig']['NetworkMode'] == 'host'
    assert all(not DEST.startswith(m['Destination'].rstrip('/') + '/') for m in old['Mounts'])
    for name in FILES:
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == manifest['files'][name]
    save('container-before.json', old)
    save('health-before.json', health())
    (ROOT / 'Dockerfile').write_text('FROM ' + old['Image'] + '\n' + ''.join('COPY ' + name + ' ' + DEST + name + '\n' for name in FILES))
    with (ROOT / 'build.log').open('w') as log:
        subprocess.run(['docker', 'build', '--network=none', '-t', TAG, str(ROOT)], stdout=log, stderr=subprocess.STDOUT, check=True)
    cmd('docker', 'run', '--rm', '--network=none', '--entrypoint=python', TAG, '-c', 'import monitoring.api')
    image = json.loads(cmd('docker', 'image', 'inspect', TAG))[0]['Id']
    output = cmd('docker', 'run', '--rm', '--network=host', '--cpus=1', '--memory=512m',
                 '-v', str(ROOT / 'candidate_api.py') + ':/candidate.py:ro', '--entrypoint=python', image, '/candidate.py', timeout=300)
    candidate = json.loads(output)
    assert candidate['passed'] and candidate['production_imports'] == 0
    save('candidate-api.json', dict(candidate, image=image))
    save('prepared.json', {'old_id': old['Id'], 'image': image, 'protected': protected()})
    notice('Read-only candidate checks passed; image prepared')


def rollback():
    # Once panels use the new datasource, they must be restored first.
    publication = Path('/data2/monitoring/perses/release/acceleration_state.json')
    assert not publication.exists() or not json.loads(publication.read_text())['groups'], 'Restore datasource bindings before API rollback'
    from admin import update
    for group in ('cpu', 'dcu', 'a3'):
        update(Path('/data2/monitoring/state/perses-acceleration-admin.json'), 'disable', group=group)
    restore(NAME, read('container-before.json'), BACKUP, read('prepared.json')['image'])
    save('rollback.json', {'at': time.time(), 'restored_id': inspect(NAME)['Id']})


def switch():
    old, prepared, manifest = read('container-before.json'), read('prepared.json'), read('manifest.json')
    assert inspect(NAME)['Id'] == old['Id'] and source_hash(NAME) == manifest['before_sha256']
    assert find(BACKUP) is None and protected() == prepared['protected']
    assert read('candidate-api.json')['image'] == prepared['image']
    baseline = health()
    assert baseline['status'] == 'ok'
    fields = ('User', 'ExposedPorts', 'Env', 'Cmd', 'Healthcheck', 'Volumes', 'WorkingDir', 'Entrypoint', 'Labels', 'StopSignal', 'StopTimeout', 'Hostname')
    config = copy.deepcopy({key: old['Config'][key] for key in fields if key in old['Config']})
    config.update(Image=prepared['image'], HostConfig=copy.deepcopy(old['HostConfig']))
    config.setdefault('Labels', {})['monitoring.transaction'] = BACKUP
    if '--no-proxy-headers' not in config['Cmd']:
        config['Cmd'].append('--no-proxy-headers')
    config['Env'] = [v for v in config['Env'] if not v.startswith('PERSES_ACCELERATION_CATALOG=')]
    config['Env'].append('PERSES_ACCELERATION_CATALOG=' + DEST + 'perses_acceleration_catalog.json')
    save('transaction.json', {'old_id': old['Id'], 'new_image': prepared['image'], 'started_at': time.time(), 'backup': BACKUP})
    try:
        cmd('docker', 'stop', old['Id']); cmd('docker', 'rename', old['Id'], BACKUP)
        identity = create(NAME, config); cmd('docker', 'start', identity)
        deadline = time.monotonic() + 120
        stable = None
        while time.monotonic() < deadline:
            try:
                current, result = inspect(NAME), health()
                assert current['Id'] == identity and current['State']['Running'] and current['RestartCount'] == 0
                assert result['status'] == 'ok' and result['perses_acceleration']['enabled']
                assert result['perses_acceleration']['state_error'] is None
                for env, value in result['environments'].items():
                    assert value['error'] is None and 0 <= time.time() - value['processed_at'] < 20
                    for source, status in baseline['environments'][env]['sources'].items():
                        if status == 'ok': assert value['sources'][source] == 'ok'
                stable = time.monotonic() if stable is None else stable
                if time.monotonic() - stable >= 15: break
            except (OSError, ValueError, KeyError, AssertionError):
                stable = None
            time.sleep(1)
        else:
            raise RuntimeError('Shadow API did not reach stable health')
        assert protected() == prepared['protected']
        assert all(source_hash(NAME, name) == manifest['files'][name] for name in FILES)
        save('shadow-started.json', {'passed': True, 'at': time.time(), 'image': prepared['image'],
             'container_id': identity, 'panels_switched': 0, 'backup': BACKUP})
    except BaseException as error:
        record_failure('switch-failure.json', error)
        rollback()
        raise
    notice('Shadow worker started; all dashboards retain original datasources')


def observe():
    started = read('shadow-started.json')
    observed_at = time.time()
    bad = 0
    deadline = time.monotonic() + 1800
    records = []
    initial_failures = None
    try:
        while time.monotonic() < deadline:
            try:
                result = health()
                lags = {env: time.time() - value['processed_at'] for env, value in result['environments'].items()}
                assert all(value['error'] is None for value in result['environments'].values())
                assert result['status'] == 'ok' and max(lags.values()) < 20
                acceleration = result['perses_acceleration']
                assert not acceleration['state_error']
                assert not acceleration.get('backfill_priority_active') and not acceleration.get('parallel_a3_backfill'), 'Temporary scheduling is not normal-load evidence'
                assert not acceleration.get('disabled_groups')
                assert all(not j['error'] and not (j.get('backfill') or {}).get('error')
                           and j['lag_seconds'] <= max(600, 2 * int(j['job'].rsplit(':', 1)[1]))
                           for j in acceleration.get('jobs', []))
                failures = acceleration.get('counters', {}).get('worker_failures', 0)
                if initial_failures is None: initial_failures = failures
                assert failures == initial_failures, 'New worker failure during normal-load observation'
                bad = 0
                records.append({'at': time.time(), 'model_lags': lags, 'acceleration': result['perses_acceleration']})
            except (OSError, ValueError, KeyError, AssertionError):
                bad += 1
                if bad >= 3: raise RuntimeError('Sustained health or model-processing regression')
            save('shadow-observation.json', {'passed': False, 'started_at': observed_at, 'release_started_at': started['at'], 'records': records})
            time.sleep(5)
        assert inspect(NAME)['Id'] == started['container_id'] and protected() == read('prepared.json')['protected']
        save('shadow-observation.json', {'passed': True, 'started_at': observed_at, 'release_started_at': started['at'], 'ended_at': time.time(), 'records': records})
    except BaseException as error:
        record_failure('observation-failure.json', error)
        rollback()
        raise
    notice('30-minute shadow observation passed; 12-hour performance admission is still required')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('prepare', 'switch', 'observe', 'rollback'))
    parser.add_argument('--build-dir', type=Path, default=ROOT)
    parser.add_argument('--backup', default=BACKUP)
    parser.add_argument('--tag', default=TAG)
    args = parser.parse_args()
    ROOT, BACKUP, TAG = args.build_dir.resolve(), args.backup, args.tag
    assert ROOT.is_dir(), 'Create and disclose the release directory before running'
    globals()[args.action]()
