"""Run locally on test4 via SSH MCP. Only NPU scrape and A3 host dashboard change."""
import argparse
import concurrent.futures
import copy
import json
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request

BASE = 'http://122.247.53.162:18431'
URL = BASE + '/api/v1/projects/a3-monitoring/dashboards/a3-hosts'
CONFIG = Path('/data2/monitoring/release/deploy/scrape.yml')
SERVICES = ['monitoring-perses', 'monitoring-vm', 'monitoring-vmagent', 'monitoring-api']


def http(url, data=None, method=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None


def save(root, name, data):
    (root / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def fingerprint(items):
    return {c['Name']: (c['Id'], c['Image'], c['State']['StartedAt']) for c in items}


def protected(root):
    before = json.loads((root / 'containers.before.json').read_text())
    current = json.loads(subprocess.check_output(['docker', 'inspect'] + SERVICES))
    assert fingerprint(before) == fingerprint(current), 'Monitoring container changed'
    snapshots = json.loads((root / 'before.json').read_text())
    for project, dashboards in snapshots.items():
        actual = http(BASE + '/api/v1/projects/' + project + '/dashboards')
        expected = {d['metadata']['name']: d['spec'] for d in dashboards
                    if (project, d['metadata']['name']) != ('a3-monitoring', 'a3-hosts')}
        got = {d['metadata']['name']: d['spec'] for d in actual
               if (project, d['metadata']['name']) != ('a3-monitoring', 'a3-hosts')}
        assert expected == got, 'Unrelated dashboard changed: ' + project


def canonical(document):
    d = copy.deepcopy(document)
    for var in d['spec'].get('variables', []):
        var['spec'].setdefault('allowAllValue', False)
        var['spec'].setdefault('allowMultiple', False)
        var['spec'].setdefault('display', {}).setdefault('hidden', False)
    return d['spec']


def scrape(root):
    protected(root)
    assert CONFIG.read_bytes() == (root / 'scrape.before.yml').read_bytes(), 'Concurrent config change'
    candidate = root / 'scrape.candidate.yml'
    containers = json.loads((root / 'containers.before.json').read_text())
    image = next(c['Image'] for c in containers if c['Name'] == '/monitoring-vmagent')
    cmd = ['docker', 'run', '--rm', '--network', 'none', '--entrypoint', '/vmagent', '-v', str(root) + ':/candidate:ro',
           '-v', str(CONFIG.parent) + ':/config:ro', image,
           '-promscrape.config=/candidate/scrape.candidate.yml', '-promscrape.config.dryRun']
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (root / 'config-validation.log').write_bytes(result.stdout)
    assert result.returncode == 0, result.stdout.decode()
    # Atomic replacement is visible through vmagent's directory bind mount.
    tmp = CONFIG.with_name('scrape.npu.tmp')
    tmp.write_bytes(candidate.read_bytes())
    tmp.chmod(CONFIG.stat().st_mode & 0o777)
    tmp.replace(CONFIG)
    subprocess.check_call(['docker', 'kill', '--signal=SIGHUP', 'monitoring-vmagent'])
    save(root, 'scrape-applied.json', {'at': time.time()})


def query(expression, proxy=False):
    base = BASE + '/proxy/projects/a3-monitoring/datasources/victoriametrics' if proxy else 'http://127.0.0.1:18428'
    result = http(base + '/api/v1/query?' + urllib.parse.urlencode({'query': expression}))
    assert result['status'] == 'success', result
    return result['data']['result']


def evaluate(root):
    candidate = json.loads((root / 'dashboard.candidate.json').read_text())
    up = query('up{environment="a3-vllm",job="npu-a3"}')
    assert len(up) == 2 and all(x['value'][1] == '1' for x in up), up
    expected = {(node, str(i)) for node in ('a3-1', 'a3-2') for i in range(16)}
    panels = [(k, p) for k, p in candidate['spec']['panels'].items() if k.startswith('npu-')]
    def check(item):
        key, panel = item
        expr = panel['spec']['queries'][0]['spec']['plugin']['spec']['query']
        expr = expr.replace('$__interval', '15s')
        values = query(expr.replace('$node', '.*').replace('$device', '.*'), proxy=True)
        identities = {(x['metric']['node'], x['metric']['id']) for x in values}
        assert identities == expected and len(values) == 32, (key, len(values), expected - identities)
        filtered = query(expr.replace('$node', 'a3-1').replace('$device', '0'), proxy=True)
        assert len(filtered) == 1 and filtered[0]['metric']['node'] == 'a3-1' and filtered[0]['metric']['id'] == '0', key
        return key, {'series': len(values), 'filtered_series': len(filtered), 'values': values}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = dict(pool.map(check, panels))
    assert len(results) == 6
    save(root, 'query-acceptance.json', {'passed': True, 'at': time.time(), 'up': up, 'panels': results})
    return candidate


def dashboard(root):
    protected(root)
    candidate = evaluate(root)
    original = next(d for d in json.loads((root / 'before.json').read_text())['a3-monitoring']
                    if d['metadata']['name'] == 'a3-hosts')
    current = http(URL)
    previous = root / 'dashboard-after.json'
    expected = json.loads(previous.read_text()) if previous.exists() else original
    assert current == expected, 'Concurrent A3 dashboard change'
    current['spec'] = candidate['spec']
    save(root, 'dashboard-journal.json', {'before': original, 'candidate': current})
    http(URL, current, 'PUT')
    actual = http(URL)
    assert canonical(actual) == canonical(candidate), 'Dashboard readback differs'
    save(root, 'dashboard-after.json', actual)
    protected(root)
    save(root, 'complete.json', {'passed': True, 'at': time.time(), 'npu_targets': 2,
                               'chips_per_node': 16, 'panels_added': 6, 'protected_unchanged': True})


def rollback(root):
    journal = root / 'dashboard-journal.json'
    if journal.exists():
        tx = json.loads(journal.read_text())
        current = http(URL)
        assert canonical(current) in (canonical(tx['before']), canonical(tx['candidate'])), 'Concurrent dashboard edit'
        current['spec'] = tx['before']['spec']
        http(URL, current, 'PUT')
    assert CONFIG.read_bytes() in ((root / 'scrape.candidate.yml').read_bytes(), (root / 'scrape.before.yml').read_bytes()), 'Concurrent config edit'
    tmp = CONFIG.with_name('scrape.npu.tmp')
    tmp.write_bytes((root / 'scrape.before.yml').read_bytes())
    tmp.chmod(CONFIG.stat().st_mode & 0o777)
    tmp.replace(CONFIG)
    subprocess.check_call(['docker', 'kill', '--signal=SIGHUP', 'monitoring-vmagent'])
    save(root, 'rollback.json', {'at': time.time(), 'passed': True})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['scrape', 'dashboard', 'evaluate', 'rollback'])
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    globals()[args.action](args.root)
    print(args.action + ' passed', flush=True)
