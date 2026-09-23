"""Execute on test4 via SSH MCP. Snapshot live resources, then patch six panels."""
import copy
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request
import xpu_hosts_release as release
from xpu_hardware import configure, PANELS

release.ROOT = pathlib.Path('/data2/monitoring/releases/xpu-hardware-20260922')
ROOT = release.ROOT


def prepare():
    assert not (ROOT / 'dashboards-before.json').exists(), 'Snapshot already exists'
    before = {p: release.api('/api/v1/projects/' + p + '/dashboards')
              for p in ('dcu-monitoring', 'a3-monitoring', 'xpu-monitoring')}
    release.save('dashboards-before.json', before)
    old = next(d for d in before['xpu-monitoring'] if d['metadata']['name'] == 'hosts-xpu')
    new = configure(old)
    for key, panel in old['spec']['panels'].items():
        if key not in PANELS:
            assert new['spec']['panels'][key] == panel
    assert new['spec']['layouts'] == old['spec']['layouts']
    release.save('hosts-xpu.json', new)
    current = release.CONFIG.read_text()
    assert current == (ROOT / 'scrape-before.yml').read_text()
    assert 'job_name: xpu-hardware' not in current
    (ROOT / 'scrape-candidate.yml').write_text(current.rstrip() + '\n\n' + (ROOT / 'xpu_hardware_scrape.yml').read_text())
    print('Prepared live snapshot and six-panel patch')


def verify():
    document = release.api(release.PATH)
    proxy = '/proxy/projects/xpu-monitoring/datasources/victoriametrics'
    def query(expression, ranged=False, step=15):
        params = {'query': expression, 'nocache': 1}
        if ranged:
            end = int(time.time()) - 15
            params.update(start=end-60, end=end, step=step)
        else:
            params['time'] = time.time()
        path = '/api/v1/' + ('query_range' if ranged else 'query') + '?' + urllib.parse.urlencode(params)
        proxied = release.api(proxy + path)
        with urllib.request.urlopen('http://127.0.0.1:18428' + path, timeout=30) as response:
            direct = json.load(response)
        assert proxied['status'] == direct['status'] == 'success'
        def normalized(data):
            return sorted(data['data']['result'], key=lambda s: json.dumps(s['metric'], sort_keys=True))
        assert normalized(proxied) == normalized(direct), expression
        return proxied['data']['result']
    health = query('min_over_time(up{job="xpu-hardware"}[1m])')
    assert len(health) == 2 and all(float(s['value'][1]) == 1 for s in health), health
    cases = []
    for key in PANELS:
        template = document['spec']['panels'][key]['spec']['queries'][0]['spec']['plugin']['spec']['query']
        for step in (15, 60):
            for node, device in (('.*', '.*'), ('xpu-1', '.*'), ('xpu-2', '.*'), ('xpu-1', '0'), ('xpu-2', '7')):
                expression = template.replace('$__interval', str(step)+'s').replace('$node', node).replace('$device', device)
                series = query(expression, True, step)
                nodes = {'xpu-1', 'xpu-2'} if node == '.*' else {node}
                cards = {str(i) for i in range(8)} if device == '.*' else {device}
                assert {(s['metric']['node'],s['metric']['devid']) for s in series} == {(n,c) for n in nodes for c in cards}, (key,node,device)
                assert all(len(s['values']) >= (2 if step == 60 else 4) for s in series), key
                cases.append({'panel':key,'step':step,'node':node,'device':device,'series':len(series)})
    release.unchanged(json.loads((ROOT/'dashboards-before.json').read_text()), skip_host=True)
    assert release.CONFIG.read_bytes() == (ROOT/'scrape-candidate.yml').read_bytes()
    candidate = json.loads((ROOT/'hosts-xpu.json').read_text())
    for field in ('panels','layouts','variables','duration','refreshInterval'):
        assert document['spec'][field] == candidate['spec'][field], field
    release.save('hardware-verification.json', {'verified_at':time.time(),'health':health,'cases':cases})
    print('Verified 60 hardware query/filter/step cases against VM and Perses; 16 cards; unrelated dashboards unchanged')


if __name__ == '__main__':
    {'prepare':prepare,'publish':release.publish,'verify':verify}[sys.argv[1]]()
