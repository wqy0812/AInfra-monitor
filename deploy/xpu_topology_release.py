"""Run locally on test4 via SSH MCP; guarded XPU mapping repair, no rollback."""
import argparse
import base64
import concurrent.futures
import copy
import json
import subprocess
import sys
import time
from pathlib import Path

RUNTIME = Path('/data2/monitoring/perses/release')
CONFIG = Path('/data2/monitoring/release/deploy/scrape.yml')
sys.path.append(str(RUNTIME))
import project_release as r
from xpu_topology import configure, scrape, NODES, IPS


def encoded(path):
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else None


def atomic(path, data):
    temp = path.with_name(path.name + '.xpu-topology.tmp')
    with temp.open('xb') as f:
        f.write(data)
    temp.chmod(path.stat().st_mode if path.exists() else 0o644)
    temp.replace(path)


def prepare(root):
    assert not (root/'before.json').exists(), 'Preserve existing evidence'
    before = r.snapshot()
    candidate = copy.deepcopy(before)
    candidate['dashboards'] = [configure(d) for d in candidate['dashboards']]
    r.validate(candidate)
    r.save(root, 'before.json', before)
    r.save(root, 'candidate.json', candidate)
    r.save(root, 'services-before.json', r.fingerprint())
    (root/'scrape-before.yml').write_bytes(CONFIG.read_bytes())
    (root/'scrape-candidate.yml').write_text(scrape(CONFIG.read_text()))
    patches = json.loads((root/'runtime-patches.json').read_text())
    entries = []
    for name, pairs in patches.items():
        path = RUNTIME/name
        original = path.read_text()
        updated = original
        for old, new in pairs:
            assert updated.count(old) == 1, ('Unexpected runtime source', name, old)
            updated = updated.replace(old, new)
        entries.append({'path': str(path), 'before': encoded(path), 'after': base64.b64encode(updated.encode()).decode()})
    module = RUNTIME/'xpu_topology.py'
    assert not module.exists()
    entries.insert(0, {'path': str(module), 'before': None, 'after': encoded(root/'xpu_topology.py')})
    for path in (RUNTIME/'projects/xpu-monitoring/dashboards').glob('*.json'):
        source = json.loads(path.read_text())
        updated = configure(source)
        if updated != source:
            entries.append({'path': str(path), 'before': encoded(path), 'after': base64.b64encode((json.dumps(updated, ensure_ascii=False, indent=2)+'\n').encode()).decode()})
    r.save(root, 'runtime-plan.json', entries)
    changed = [d['metadata']['name'] for d in candidate['dashboards'] if d != r.flattened(before)[r.key(d)]]
    print(json.dumps({'prepared': True, 'dashboards': changed, 'runtime_files': len(entries)}))


def apply(root):
    before = json.loads((root/'before.json').read_text())
    candidate = json.loads((root/'candidate.json').read_text())
    entries = json.loads((root/'runtime-plan.json').read_text())
    assert not (root/'apply-journal.json').exists(), 'Inspect partial application before continuing'
    assert r.snapshot() == before, 'Concurrent dashboard edit'
    assert r.fingerprint() == json.loads((root/'services-before.json').read_text()), 'Concurrent service change'
    assert CONFIG.read_bytes() == (root/'scrape-before.yml').read_bytes(), 'Concurrent scrape edit'
    for item in entries:
        assert encoded(Path(item['path'])) == item['before'], ('Concurrent runtime edit', item['path'])
    container = json.loads(subprocess.check_output(['docker', 'inspect', 'monitoring-vmagent']))[0]
    subprocess.check_call(['docker', 'run', '--rm', '--network=none', '-v', str(root/'scrape-candidate.yml')+':/candidate.yml:ro', '--entrypoint', container['Config']['Entrypoint'][0], container['Image'], '-promscrape.config=/candidate.yml', '-promscrape.config.dryRun'])
    journal = {'started_at': time.time(), 'steps': []}
    def mark(value):
        journal['steps'].append(value)
        r.save(root, 'apply-journal.json', journal)
    mark('scrape write starting')
    assert CONFIG.read_bytes() == (root/'scrape-before.yml').read_bytes()
    atomic(CONFIG, (root/'scrape-candidate.yml').read_bytes())
    subprocess.check_call(['docker', 'kill', '--signal=HUP', 'monitoring-vmagent'])
    mark({'scrape_reloaded_at': time.time()})
    originals = r.flattened(before)
    for document in candidate['dashboards']:
        original = originals[r.key(document)]
        if original == document:
            continue
        url = r.endpoint('Dashboard', document)+'/'+document['metadata']['name']
        assert r.http(url) == original, 'Concurrent dashboard edit'
        mark({'publishing': document['metadata']['name']})
        r.http(url, 'PUT', document)
        assert r.spec(r.http(url)) == r.spec(document), 'Readback differs'
    for item in entries:
        path = Path(item['path'])
        assert encoded(path) == item['before'], 'Concurrent runtime edit'
        mark({'writing': str(path)})
        atomic(path, base64.b64decode(item['after']))
    after = r.snapshot()
    for key, document in r.flattened(candidate).items():
        assert r.spec(r.flattened(after)[key]) == r.spec(document), key
    assert r.fingerprint() == json.loads((root/'services-before.json').read_text())
    r.save(root, 'after.json', after)
    mark({'completed_at': time.time()})
    print('Applied XPU mapping and read back all resources; services unchanged')


def instant(expression):
    return r.http(r.VM+'/api/v1/query?'+r.urllib.parse.urlencode({'query': expression, 'nocache': 1}))['data']['result']


def collection(root):
    checks = []
    for role in NODES:
        for job, port in [('sglang-'+role, 8501), ('node-xpu', 9110), ('xpu-hardware', 9507)]:
            selector = 'environment="xpu-pd",role="'+role+'",node="'+NODES[role]+'",job="'+job+'",instance="'+IPS[role]+':'+str(port)+'"'
            up = instant('min_over_time(up{'+selector+'}[2m])')
            counts = instant('count_over_time(up{'+selector+'}[2m])')
            passed = len(up) == len(counts) == 1 and float(up[0]['value'][1]) == 1 and float(counts[0]['value'][1]) >= 23
            checks.append({'role': role, 'job': job, 'passed': passed, 'counts': counts})
    report = {'passed': all(c['passed'] for c in checks), 'at': time.time(), 'checks': checks}
    r.save(root, 'collection.json', report)
    print(json.dumps(report))
    return report['passed']


def verify(root):
    assert collection(root), 'Wait for two minutes of corrected samples'
    after = r.snapshot()
    for document in after['dashboards']:
        assert configure(document) == document, document['metadata']
    end = int(time.time()//15)*15-20
    start = end-120
    work = []
    for d in after['dashboards']:
        if d['metadata']['project'] != 'xpu-monitoring':
            continue
        for key, p in d['spec']['panels'].items():
            for item in p['spec']['queries']:
                spec = item['spec']['plugin']['spec']
                for role in (['all', 'prefill', 'decode'] if '$role' in spec['query'] else ['all']):
                    q = spec['query'].replace('$role', '.*' if role == 'all' else '('+role+'|'+NODES[role]+')')
                    datasource = spec.get('datasource', {}).get('name', 'victoriametrics')
                    work.append((d['metadata']['name'], key, role, q, datasource))
    def check(item):
        name, key, role, q, datasource = item
        a = r.query(r.VM, q, start, end, 15)
        b = r.query(r.BASE+'/proxy/projects/xpu-monitoring/datasources/'+datasource, q, start, end, 15)
        assert r.equivalent(a, b), (name, key, role, 'VM/proxy mismatch')
        return {'dashboard': name, 'panel': key, 'role': role, 'series': len(a), 'points': sum(len(x['values']) for x in a), 'nodes': sorted(set(x['metric'].get('node', '') for x in a))}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        checks = list(pool.map(check, work))
    output = instant('sum by (role,node) (rate(sglang:generation_tokens_total{environment="xpu-pd",job="sglang-decode"}[1m]))')
    assert len(output) == 1 and output[0]['metric']['node'] == NODES['decode'] and float(output[0]['value'][1]) > 0, output
    cache = instant('sglang:cache_hit_rate{environment="xpu-pd",job="sglang-prefill",instance="'+IPS['prefill']+':8501",tp_rank="0",pp_rank="0"}')
    assert cache and all(x['metric']['node'] == NODES['prefill'] for x in cache)
    latest = r.http('http://127.0.0.1:18430/api/monitoring/latest?environment=xpu-pd')
    r.save(root, 'latest.json', latest)
    report = {'passed': True, 'at': time.time(), 'queries': len(checks), 'checks': checks, 'decode_output': output, 'prefill_cache': cache}
    r.save(root, 'verification.json', report)
    print(json.dumps({k: v for k, v in report.items() if k != 'checks'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'apply', 'collection', 'verify'])
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    try:
        globals()[args.action](args.evidence)
    except Exception as error:
        r.save(args.evidence, 'failure-'+args.action+'.json', {'at': time.time(), 'error': str(error), 'action': args.action, 'rollback': False})
        raise
