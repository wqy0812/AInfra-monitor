"""Run on test4 via SSH MCP; publish only XPU host collection and dashboard."""
import json
import pathlib
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path('/data2/monitoring/releases/xpu-hosts-20260922')
CONFIG = pathlib.Path('/data2/monitoring/release/deploy/scrape.yml')
BASE = 'http://122.247.53.162:18431'
PATH = '/api/v1/projects/xpu-monitoring/dashboards/hosts-xpu'
TOKEN = None


def api(path, data=None):
    global TOKEN
    if TOKEN is None:
        credentials = json.loads(pathlib.Path('/data2/monitoring/perses/admin-credentials.json').read_text())
        request = urllib.request.Request(BASE + '/api/auth/providers/native/login',
            data=json.dumps(credentials).encode(), headers={'Content-Type': 'application/json'})
        TOKEN = json.load(urllib.request.urlopen(request, timeout=15))['access_token']
    request = urllib.request.Request(BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        method='PUT' if data is not None else 'GET',
        headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
        return json.loads(content) if content else None


def save(name, data):
    (ROOT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def unchanged(before, skip_host=False):
    for project, dashboards in before.items():
        current = api('/api/v1/projects/' + project + '/dashboards')
        def specs(items):
            return {d['metadata']['name']: d['spec'] for d in items
                    if not (skip_host and project == 'xpu-monitoring' and d['metadata']['name'] == 'hosts-xpu')}
        assert specs(current) == specs(dashboards), 'Concurrent dashboard edit: ' + project


def write_config(content):
    temporary = CONFIG.with_suffix('.xpu-hosts.tmp')
    temporary.write_bytes(content)
    temporary.chmod(CONFIG.stat().st_mode)
    temporary.replace(CONFIG)
    subprocess.check_call(['docker', 'kill', '--signal=HUP', 'monitoring-vmagent'])


def publish():
    before = json.loads((ROOT / 'dashboards-before.json').read_text())
    old_config = (ROOT / 'scrape-before.yml').read_bytes()
    candidate = (ROOT / 'scrape-candidate.yml').read_bytes()
    old = next(d for d in before['xpu-monitoring'] if d['metadata']['name'] == 'hosts-xpu')
    new = json.loads((ROOT / 'hosts-xpu.json').read_text())
    new['metadata'] = old['metadata']
    assert CONFIG.read_bytes() == old_config, 'Concurrent scrape configuration edit'
    unchanged(before)
    container = json.loads(subprocess.check_output(['docker', 'inspect', 'monitoring-vmagent']))[0]
    subprocess.check_call(['docker', 'run', '--rm', '--network=none', '-v',
        str(ROOT / 'scrape-candidate.yml') + ':/candidate.yml:ro', '--entrypoint',
        container['Config']['Entrypoint'][0], container['Image'],
        '-promscrape.config=/candidate.yml', '-promscrape.config.dryRun'])
    assert CONFIG.read_bytes() == old_config
    assert api(PATH)['spec'] == old['spec']
    config_written = False
    dashboard_attempted = False
    try:
        config_written = True
        write_config(candidate)
        dashboard_attempted = True
        api(PATH, new)
        got = api(PATH)
        for field in ('panels', 'layouts', 'variables', 'duration', 'refreshInterval'):
            assert got['spec'][field] == new['spec'][field], field
        unchanged(before, skip_host=True)
        save('published.json', {'published_at': time.time(), 'dashboard': PATH, 'panels': len(new['spec']['panels'])})
        print('Published XPU hosts; unrelated dashboards unchanged')
    except BaseException as failure:
        if dashboard_attempted:
            try:
                current = api(PATH)
                if current['spec'] == new['spec']:
                    old['metadata'] = current['metadata']
                    api(PATH, old)
            except BaseException as rollback_error:
                failure.add_note('Dashboard rollback failed: ' + repr(rollback_error))
        try:
            if config_written and CONFIG.read_bytes() == candidate:
                write_config(old_config)
        except BaseException as rollback_error:
            failure.add_note('Scrape configuration rollback failed: ' + repr(rollback_error))
        raise


def verify():
    assert time.time() - json.loads((ROOT / 'published.json').read_text())['published_at'] >= 120, 'Wait for two full rate windows'
    document = api(PATH)
    sources = api('/api/v1/projects/xpu-monitoring/datasources')
    proxy = '/proxy/projects/xpu-monitoring/datasources/' + sources[0]['metadata']['name']
    def query(expression, ranged=False):
        params = {'query': expression, 'nocache': 1}
        if ranged:
            params.update(start=time.time() - 60, end=time.time(), step=15)
        result = api(proxy + '/api/v1/' + ('query_range' if ranged else 'query') + '?' + urllib.parse.urlencode(params))
        assert result['status'] == 'success'
        return result['data']['result']
    up = query('min_over_time(up{job="node-xpu",environment="xpu-pd"}[2m])')
    assert len(up) == 2 and all(float(s['value'][1]) == 1 for s in up), up
    counts = query('count_over_time(up{job="node-xpu",environment="xpu-pd"}[2m])')
    assert len(counts) == 2 and all(float(s['value'][1]) >= 23 for s in counts), counts
    fresh = query('time() - timestamp(up{job="node-xpu",environment="xpu-pd"})')
    assert len(fresh) == 2 and all(0 <= float(s['value'][1]) < 15 for s in fresh), fresh
    results = []
    for key, panel in document['spec']['panels'].items():
        for index, item in enumerate(panel['spec']['queries']):
            expression = item['spec']['plugin']['spec']['query']
            hardware = expression == 'vector(0) unless on() vector(0)'
            for node in ('.*', 'xpu-1', 'xpu-2'):
                q = expression.replace('$__interval', '15s').replace('$node', node).replace('$device', '.*')
                series = query(q, True)
                assert bool(series) != hardware, (key, index, node, 'unexpected data presence')
                if not hardware:
                    nodes = {s['metric'].get('node') for s in series}
                    assert nodes == ({'xpu-1', 'xpu-2'} if node == '.*' else {node}), (key, nodes)
                results.append({'panel': key, 'query': index, 'node': node, 'series': len(series)})
    unchanged(json.loads((ROOT / 'dashboards-before.json').read_text()), skip_host=True)
    assert CONFIG.read_bytes() == (ROOT / 'scrape-candidate.yml').read_bytes()
    save('verification.json', {'verified_at': time.time(), 'up': up, 'sample_counts': counts, 'freshness': fresh, 'queries': results})
    print('Verified', len(results), 'panel query/filter cases; both targets up for 2m; other dashboards unchanged')


if __name__ == '__main__':
    {'publish': publish, 'verify': verify}[sys.argv[1]]()
