"""Patch only the real-time region of the existing gateway-generation dashboard."""
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from gateway_live import extend_dashboard, build_panels

BASE = 'http://122.247.53.162:18431'
API = BASE + '/api/v1/projects/dcu-monitoring/dashboards'
NAME = 'gateway-generation'
SOURCES = ['http://127.0.0.1:18428', BASE + '/proxy/projects/dcu-monitoring/datasources/victoriametrics']


def get(url):
    return json.load(urllib.request.urlopen(url, timeout=30))


def save(root, name, value):
    p = root / name
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    p.chmod(0o600)


def protected():
    data = json.loads(subprocess.check_output(['docker', 'inspect', 'monitoring-perses', 'monitoring-vm', 'monitoring-vmagent', 'monitoring-api']))
    return {x['Name']: {'id': x['Id'], 'started': x['State']['StartedAt']} for x in data}


def main():
    root = Path(sys.argv[1])
    assert json.loads((root / 'live-semantics.json').read_text())['passed']
    old = get(API + '/' + NAME)
    proposed = extend_dashboard(old)
    all_before = get(API)
    services = protected()
    if not (root / 'dashboard-before.json').exists():
        save(root, 'dashboard-before.json', old)
        save(root, 'dashboards-before.json', all_before)
        save(root, 'services-before.json', services)
    # Check the actual rendered expressions against both data paths at fixed times.
    end = int(time.time() // 5) * 5 - 90
    checks = []
    for key, panel in proposed['spec']['panels'].items():
        for index, query in enumerate(panel['spec']['queries']):
            for step in (15, 60):
                params = {'query': query['spec']['plugin']['spec']['query'].replace('$__interval', f'{step}s'), 'start': end - 600, 'end': end, 'step': step, 'nocache': '1'}
                answers = []
                for base in SOURCES:
                    req = urllib.request.Request(base + '/api/v1/query_range', data=urllib.parse.urlencode(params).encode())
                    result = json.load(urllib.request.urlopen(req, timeout=30))
                    assert result['status'] == 'success'
                    answers.append(result['data'])
                assert answers[0] == answers[1], (key, index, step, answers)
                checks.append({'panel': key, 'query': index, 'step': step, 'series': len(answers[0]['result'])})
    # Do not publish if either gateway has not exposed the new metric families yet.
    for environment in ('dcu-pd', 'a3-vllm'):
        q = f'aigate_live_backend_groups{{job="aigate",environment="{environment}"}} and (time() - timestamp(aigate_live_backend_groups{{job="aigate",environment="{environment}"}}) < 15)'
        d = get(SOURCES[0] + '/api/v1/query?' + urllib.parse.urlencode({'query': q}))
        assert d['status'] == 'success' and len(d['data']['result']) == 1, environment
    assert get(API + '/' + NAME) == old, 'Dashboard changed during validation; refusing to overwrite'
    if proposed != old:
        req = urllib.request.Request(API + '/' + NAME, data=json.dumps(proposed).encode(), headers={'Content-Type': 'application/json'}, method='PUT')
        urllib.request.urlopen(req, timeout=30).close()
    actual = get(API + '/' + NAME)
    assert actual['spec'] == proposed['spec'], 'Readback differs'
    for key, p in old['spec']['panels'].items():
        if key not in build_panels():
            assert actual['spec']['panels'][key] == p
    after = get(API)
    assert len(after) == len(all_before)
    for x in all_before:
        if x['metadata']['name'] != NAME:
            assert next(y for y in after if y['metadata']['name'] == x['metadata']['name']) == x
    assert protected() == services
    save(root, 'dashboard-after.json', actual)
    save(root, 'publication.json', {'passed': True, 'panels': len(actual['spec']['panels']), 'new_panels': len(build_panels()), 'query_checks': checks, 'other_dashboards_preserved': True, 'services_preserved': True, 'time': time.time()})
    print(json.dumps({'passed': True, 'panels': len(actual['spec']['panels']), 'query_checks': len(checks), 'url': BASE + '/projects/dcu-monitoring/dashboards/' + NAME}))


if __name__ == '__main__':
    main()
