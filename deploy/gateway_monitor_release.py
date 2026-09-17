"""Patch only gateway query/API files; run on test4 through SSH MCP."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import local_latest_release as release

release.ROOT = Path('/data2/monitoring/releases/gateway-monitor-20260916-v1')
release.TAG = 'monitoring-api:gateway-monitor-20260916-v1'
FILES = {'api.py', 'gateway_live.py'}


def source_hashes(container):
    code = ('import pathlib,hashlib,json;print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() '
            'for p in pathlib.Path("/monitoring/monitoring").glob("*.py")}))')
    return json.loads(release.command('docker', 'exec', container, 'python', '-c', code))


def prepare():
    assert not (release.ROOT / 'prepared.json').exists()
    assert (release.ROOT / 'check_gateway_monitor_candidate.py').is_file(), 'Missing API candidate checker in release payload'
    assert {p.name for p in (release.ROOT / 'monitoring').glob('*.py')} == FILES
    expected = release.read('manifest.json')
    actual = source_hashes('monitoring-api')
    assert actual['api.py'] == expected['before']['api.py'] and 'gateway_live.py' not in actual
    assert release.hashes() == {'monitoring/' + k: v for k, v in expected['after'].items()}
    if (release.ROOT / 'containers-before.json').exists():
        baseline = next(c for c in release.read('containers-before.json') if c['Name'] == '/monitoring-api')
        assert release.inspect('monitoring-api')['Id'] == baseline['Id']
        assert actual == release.read('baseline-source.json')
    else:
        ids = release.command('docker', 'ps', '-q').split()
        release.save('containers-before.json', json.loads(release.command('docker', 'inspect', *ids)))
        release.save('baseline-source.json', actual)
    release.check_protected()
    baseline = release.inspect('monitoring-api')
    assert not any(m['Destination'].startswith('/monitoring') for m in baseline['Mounts'])
    assert not any(line[2:].startswith('/monitoring/monitoring/') and line.endswith('.py')
                   for line in release.command('docker', 'diff', 'monitoring-api').splitlines())
    release.save('source-sha256.json', release.hashes())
    (release.ROOT / 'Dockerfile').write_text('FROM ' + baseline['Image'] + '\nCOPY monitoring/ /monitoring/monitoring/\n')
    with (release.ROOT / 'build.log').open('w') as log:
        subprocess.run(['docker', 'build', '-t', release.TAG, str(release.ROOT)], stdout=log, stderr=subprocess.STDOUT, check=True)
    smoke = ('import importlib,pkgutil,monitoring;'
             '[importlib.import_module("monitoring."+m.name) for m in pkgutil.iter_modules(monitoring.__path__)];'
             'from monitoring.request_scope import SCHEMA;assert SCHEMA=="request-metrics-v2";print("imports OK")')
    result = release.command('docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', release.TAG, '-c', smoke)
    release.save('prepared.json', {'image': release.inspect(release.TAG)['Id'], 'tag': release.TAG, 'candidate_imports': result})
    print(json.dumps(release.read('prepared.json')))


def candidate():
    output = release.command('docker', 'run', '--rm', '--network', 'host',
        '-e', 'VM_URL=http://127.0.0.1:18428', '-e', 'STATE_DIR=/tmp/gateway-readonly-state', '-e', 'PYTHONPATH=/monitoring',
        '-v', str(release.ROOT) + ':/release-checks:ro', '--entrypoint', 'python', release.TAG,
        '/release-checks/check_gateway_monitor_candidate.py', 'api')
    result = json.loads(output)
    assert result['passed']
    release.save('candidate-acceptance.json', result)
    print(output)


def verify():
    release.check_protected()
    expected = {**release.read('baseline-source.json'), **release.read('manifest.json')['after']}
    assert source_hashes('monitoring-api') == expected, 'Unrelated runtime source changed'
    health = release.get('/health')
    assert health['status'] == 'ok'
    ranges = {}
    for env in ('dcu-pd', 'a3-vllm'):
        for hours in (1, 6, 24, 168, 720):
            start = time.monotonic()
            data = release.get('/api/monitoring/history?hours=' + str(hours) + '&environment=' + env)
            assert data['environment'] == env and 0 < len(data['points']) <= 721
            assert set(data['gateway_status'].values()) == {'ok'}
            counts = {k: sum(p['gateway'][k] is not None for p in data['points'])
                      for k in ('stream_idle_max_seconds', 'oldest_age_seconds')}
            assert all(counts.values()), (env, hours, counts)
            ranges[env + '/' + str(hours)] = {'points': len(data['points']), 'gateway_valid_points': counts,
                                            'seconds': round(time.monotonic() - start, 3)}
    release.save('acceptance.json', {'passed': True, 'at': time.time(), 'health': health, 'ranges': ranges,
                                   'protected_unchanged': True, 'unrelated_source_unchanged': True})
    print(json.dumps(release.read('acceptance.json')))


def finalize():
    assert release.read('acceptance.json')['passed']
    release.check_protected()
    expected = {**release.read('baseline-source.json'), **release.read('manifest.json')['after']}
    assert source_hashes('monitoring-api') == expected
    backup = release.ROOT / 'baseline-release-files'
    backup.mkdir()
    target = Path('/data2/monitoring/release/monitoring')
    for name in FILES:
        path = target / name
        if path.exists():
            shutil.copy2(path, backup / name)
        shutil.copy2(release.ROOT / 'monitoring' / name, path)
    release.save('complete.json', {'passed': True, 'at': time.time(), 'image': release.inspect('monitoring-api')['Image'],
                                  'files': sorted(FILES), 'protected_unchanged': True,
                                  'rollback': release.read('transaction.json')['backup']})
    print(json.dumps(release.read('complete.json')))


def rollback():
    release.rollback()
    if (release.ROOT / 'complete.json').exists():
        backup = release.ROOT / 'baseline-release-files'
        target = Path('/data2/monitoring/release/monitoring')
        for name in FILES:
            if (backup / name).exists():
                shutil.copy2(backup / name, target / name)
            elif name not in release.read('baseline-source.json'):
                if (target / name).exists():
                    (target / name).unlink()


if __name__ == '__main__':
    {'prepare': prepare, 'candidate': candidate, 'switch': release.switch, 'verify': verify,
     'finalize': finalize, 'rollback': rollback}[sys.argv[1]]()
