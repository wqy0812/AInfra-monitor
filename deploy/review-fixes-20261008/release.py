"""Execute this captured release on test4 via SSH MCP; failures never roll back."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'deploy'), str(ROOT / 'perses')]
import project_release as perses
from replace import replace

NAME = 'monitoring-api'
TAG = 'monitoring-api:review-fixes-20261008'


def run(*args):
    return subprocess.check_output(args, text=True, timeout=45)


def read(name):
    return json.loads((ROOT / name).read_text())


def save(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def inspect():
    return json.loads(run('docker', 'inspect', NAME))[0]


def api_hashes():
    code = ("import pathlib,json,hashlib; p=pathlib.Path('/monitoring/monitoring'); "
            "print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() "
            "for f in p.rglob('*.py') if not f.name.startswith('._')}))")
    return json.loads(run('docker', 'exec', NAME, 'python', '-c', code))


def payload():
    manifest = read('manifest.json')
    for name, expected in manifest['files'].items():
        assert sha(ROOT / name) == expected, ('Payload changed', name)
    return manifest


def resource_specs():
    resources = perses.snapshot()
    return {'/'.join(perses.key(d)): digest(perses.spec(d))
            for group in resources.values() for d in group}


def check_before(manifest):
    current = inspect()
    before = manifest['api_before']
    assert current['Id'] == before['id'] and current['Image'] == before['image'], 'API changed'
    assert current['State']['Running'], 'API is not running'
    assert current['Config']['Labels'].get('monitoring.owner') == 'independent'
    assert api_hashes() == before['files'], 'Live API source changed'
    assert digest(current['HostConfig']) == before['host_config_sha256'], 'API settings changed'
    for item in manifest['runtime_plan']:
        path = Path(item['target'])
        assert path.parent.is_dir(), ('Missing runtime parent', str(path))
        assert sha(path) == item['before_sha256'], ('Concurrent runtime edit', str(path))
    assert sha(Path(manifest['scrape_path'])) == manifest['scrape_sha256'], 'Scrape config changed'
    observed = resource_specs()
    assert observed == manifest['resource_specs'], ('Live Perses resources differ',
        sorted(k for k in observed.keys() | manifest['resource_specs'].keys()
               if observed.get(k) != manifest['resource_specs'].get(k)))
    return current


def prepare():
    assert not (ROOT / 'prepared.json').exists(), 'Preparation already exists'
    manifest = payload()
    before = check_before(manifest)
    state = Path('/data2/monitoring/state')
    watermarks = {p.name: json.loads(p.read_text()) for p in state.glob('watermark-request-metrics-v2-*.json')}
    save('state-before.json', {'watermarks': watermarks,
         'activation': json.loads((state / 'a3-prefill-effective-start.json').read_text())})
    save('container-before.json', before)
    with (ROOT / 'build.log').open('w') as output:
        subprocess.run(['docker', 'build', '--network=none', '--build-arg', 'BASE=' + before['Image'],
                        '-f', str(ROOT / 'Dockerfile.api'), '-t', TAG, str(ROOT)],
                       stdout=output, stderr=subprocess.STDOUT, check=True, timeout=180)
    image = json.loads(run('docker', 'image', 'inspect', TAG))[0]['Id']
    save('prepared.json', {'image': image, 'prepared_at': time.time()})
    print(json.dumps({'prepared': True, 'image': image}), flush=True)


def apply():
    assert not (ROOT / 'apply-started.json').exists(), 'Inspect prior attempt before any retry'
    manifest = payload()
    check_before(manifest)
    image = read('prepared.json')['image']
    save('apply-started.json', {'image': image, 'at': time.time()})
    result = replace(NAME, image)
    save('replace-result.json', result)
    journal = []
    for item in manifest['runtime_plan']:
        path = Path(item['target'])
        expected = manifest['files'][item['source']]
        assert sha(path) == item['before_sha256'], ('Concurrent runtime edit', str(path))
        if sha(path) == expected:
            continue
        temporary = path.with_name(path.name + '.review-fixes-20261008.tmp')
        with temporary.open('xb') as stream:
            stream.write((ROOT / item['source']).read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
        assert sha(path) == item['before_sha256'], ('Concurrent runtime edit', str(path))
        temporary.replace(path)
        assert sha(path) == expected
        journal.append(item['target'])
        save('runtime-written.json', journal)
    save('apply-complete.json', {'image': image, 'runtime_files_updated': len(journal), 'at': time.time()})
    print(json.dumps({'applied': True, 'image': image, 'runtime_files_updated': len(journal)}), flush=True)


def verify():
    manifest = payload()
    current = inspect()
    assert current['Image'] == read('prepared.json')['image'] and current['State']['Running']
    assert digest(current['HostConfig']) == manifest['api_before']['host_config_sha256']
    assert current['Mounts'] == manifest['api_before']['mounts']
    assert current['Config']['Cmd'] == manifest['api_before']['command']
    env = dict(item.split('=', 1) for item in current['Config']['Env'] if '=' in item)
    assert env['ALLOWED_CLIENTS'] == manifest['api_before']['allowed_clients']
    expected = {name[len('monitoring/'):]: value for name, value in manifest['files'].items()
                if name.startswith('monitoring/') and name.endswith('.py')}
    assert api_hashes() == expected, 'Deployed API source differs from workspace'
    for item in manifest['runtime_plan']:
        assert sha(Path(item['target'])) == manifest['files'][item['source']], item['target']
    assert sha(Path(manifest['scrape_path'])) == manifest['scrape_sha256']
    assert resource_specs() == manifest['resource_specs']
    # Import and regenerate from the installed directory, independently of this payload.
    code = ("from pathlib import Path; from project_split import read_resources,build,validate; "
            "from project_release import flattened; "
            "from a3_mooncake import configure; import reorg_runtime; "
            "assert 'a3_mooncake.py' in reorg_runtime.MODULES; "
            "r=read_resources(Path('projects')); validate(r); "
            "assert flattened(build(r))==flattened(r); print('installed generator verified')")
    completed = subprocess.run([sys.executable, '-c', code], cwd=manifest['perses_runtime'],
                               env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'},
                               text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    state = Path('/data2/monitoring/state')
    before = read('state-before.json')
    assert json.loads((state / 'a3-prefill-effective-start.json').read_text()) == before['activation']
    for name, value in before['watermarks'].items():
        assert json.loads((state / name).read_text())['ts'] >= value['ts'], name
    with urllib.request.urlopen('http://127.0.0.1:18430/health', timeout=10) as response:
        health = json.load(response)
    assert health['status'] == 'ok'
    result = {'passed': True, 'image': current['Image'], 'api_files': len(expected),
              'runtime_files': len(manifest['runtime_plan']), 'resource_count': len(manifest['resource_specs']),
              'environments': health['environments'], 'activation': before['activation'],
              'at': time.time()}
    save('verified.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'apply', 'verify'])
    globals()[parser.parse_args().action]()
