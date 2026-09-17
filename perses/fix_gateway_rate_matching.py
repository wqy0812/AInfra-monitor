# Project-aware CLI routing; legacy helpers below remain importable.
if __name__ == "__main__":
    raise SystemExit("此历史发布入口已退役。使用 project_release.py prepare/audit/apply --evidence DIR；资源按 project/name 定位。")
    raise SystemExit(0)

"""Repair only the three basic gateway rate queries; require measured results."""
import copy
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from publish_gateway_scope import scope_query
from publish_gateway_live import API, SOURCES, get, save, protected


def main():
    root = Path(sys.argv[1])
    before = get(API)
    old = next(d for d in before if d['metadata']['name'] == 'gateway')
    services = protected()
    proposed = copy.deepcopy(old)
    keys = ('p1', 'p5', 'p6')
    checks = []
    end = int(time.time()) - 90
    for key in keys:
        spec = proposed['spec']['panels'][key]['spec']['queries'][0]['spec']['plugin']['spec']
        spec['query'] = scope_query(spec['query'])
        assert spec['query'] != old['spec']['panels'][key]['spec']['queries'][0]['spec']['plugin']['spec']['query'], 'Already fixed or unexpected query'
        for step in (15, 60):
            params = {'query': spec['query'].replace('$__interval', str(step) + 's'), 'start': end - 600, 'end': end, 'step': step, 'nocache': '1'}
            data = []
            for source in SOURCES:
                req = urllib.request.Request(source + '/api/v1/query_range', data=urllib.parse.urlencode(params).encode())
                response = json.load(urllib.request.urlopen(req, timeout=30))
                assert response['status'] == 'success', response
                data.append(response['data'])
            assert data[0] == data[1]
            rows = data[0]['result']
            assert len(rows) == 1 and rows[0]['metric']['environment'] == 'dcu-pd', (key, step, rows)
            assert len(rows[0]['values']) >= 3, (key, step, rows)
            checks.append({'panel': key, 'step': step, 'points': len(rows[0]['values']), 'last': rows[0]['values'][-1]})
    assert not (root / 'rate-matching-before.json').exists()
    save(root, 'rate-matching-before.json', old)
    save(root, 'rate-matching-checks.json', checks)
    assert get(API) == before, 'Concurrent dashboard edit'
    req = urllib.request.Request(API + '/gateway', data=json.dumps(proposed).encode(), headers={'Content-Type': 'application/json'}, method='PUT')
    urllib.request.urlopen(req, timeout=30).close()
    after = get(API)
    actual = next(d for d in after if d['metadata']['name'] == 'gateway')
    assert actual['spec'] == proposed['spec']
    assert len(after) == len(before)
    for d in before:
        if d['metadata']['name'] != 'gateway':
            assert d == next(x for x in after if x['metadata']['name'] == d['metadata']['name'])
    assert protected() == services
    save(root, 'rate-matching-after.json', actual)
    report = {'passed': True, 'checks': checks, 'other_dashboards_preserved': True, 'services_preserved': True}
    save(root, 'rate-matching-publication.json', report)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
