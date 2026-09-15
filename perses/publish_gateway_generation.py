"""Validate and create only gateway-generation; never overwrite other dashboards."""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'http://122.247.53.162:18431'
API = BASE + '/api/v1/projects/dcu-monitoring/dashboards'
VM = 'http://127.0.0.1:18428'
PROXY = BASE + '/proxy/projects/dcu-monitoring/datasources/victoriametrics'
NAME = 'gateway-generation'


def get(url):
    return json.load(urllib.request.urlopen(url, timeout=30))


def normalized(spec):
    value = json.loads(json.dumps(spec))
    if not value.get('variables'):
        value.pop('variables', None)
    return value


def protected():
    data = json.loads(subprocess.check_output(['docker', 'inspect', 'monitoring-perses', 'monitoring-vm', 'monitoring-vmagent', 'monitoring-api']))
    return {x['Name']: {'id': x['Id'], 'started': x['State']['StartedAt']} for x in data}


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    document = json.loads((root / (NAME + '.json')).read_text())
    assert document['metadata'] == {'name': NAME, 'project': 'dcu-monitoring'}
    assert json.loads((root / 'semantics.json').read_text())['passed']
    before = get(API)
    services = protected()
    evidence = root / ('publish-' + str(time.time_ns()))
    evidence.mkdir()
    (evidence / 'dashboards-before.json').write_text(json.dumps(before, ensure_ascii=False, indent=2))
    (evidence / 'services-before.json').write_text(json.dumps(services, indent=2))
    end = int(time.time() // 5) * 5 - 15
    checks = []
    for key, panel in document['spec']['panels'].items():
        assert len(panel['spec']['queries']) == 2
        assert not panel['spec']['plugin']['spec']['visual']['connectNulls']
        for index, query in enumerate(panel['spec']['queries']):
            expected_env = ['dcu-pd', 'a3-vllm'][index]
            for step in (15, 60):
                params = {'query': query['spec']['plugin']['spec']['query'].replace('$__interval', f'{step}s'),
                          'start': end - 3600, 'end': end, 'step': step}
                suffix = '/api/v1/query_range?' + urllib.parse.urlencode(params)
                direct = get(VM + suffix)
                proxy = get(PROXY + suffix)
                assert direct['status'] == proxy['status'] == 'success'
                assert direct['data'] == proxy['data'], (key, index, step)
                rows = direct['data']['result']
                assert len(rows) <= 1 and all(r['metric']['environment'] == expected_env for r in rows)
                checks.append({'panel': key, 'environment': expected_env, 'step': step,
                               'series': len(rows), 'points': sum(len(r['values']) for r in rows)})
    old = next((x for x in before if x['metadata']['name'] == NAME), None)
    if old is not None:
        assert normalized(old['spec']) == normalized(document['spec']), 'Existing dashboard differs; edits preserved'
        created = False
    else:
        request = urllib.request.Request(API, data=json.dumps(document).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                assert response.status in (200, 201)
        except urllib.error.HTTPError as error:
            print(error.read().decode(), file=sys.stderr)
            raise
        created = True
    actual = get(API + '/' + NAME)
    assert normalized(actual['spec']) == normalized(document['spec']), 'Readback differs'
    after = get(API)
    for item in before:
        assert next(x for x in after if x['metadata']['name'] == item['metadata']['name']) == item
    assert protected() == services
    assert len(after) == len(before) + int(created)
    result = {'passed': True, 'created': created, 'panels': len(document['spec']['panels']), 'queries_per_panel': 2, 'range_checks': checks,
              'existing_dashboards_unchanged': True, 'services_unchanged': True,
              'url': BASE + '/projects/dcu-monitoring/dashboards/' + NAME}
    (evidence / 'dashboard-after.json').write_text(json.dumps(actual, ensure_ascii=False, indent=2))
    (root / 'publication.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != 'range_checks'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
