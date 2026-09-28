"""Snapshot-based consolidation rollout. Execute remotely via SSH MCP only."""
import argparse
import concurrent.futures
import copy
import hashlib
import json
import math
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'perses'))
from query_acceleration import prepare, SERIES_LABEL, ORDER_LABEL

BASE = 'http://122.247.53.162:18431'
VM = 'http://127.0.0.1:18428'
SERVICES = ('monitoring-perses', 'monitoring-vm', 'monitoring-vmagent', 'monitoring-api')
ROOT = Path(__file__).resolve().parent
TOKEN = None
AUTH_LOCK = threading.Lock()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, method='GET', data=None):
    global TOKEN
    for attempt in range(2):
        with AUTH_LOCK:
            if TOKEN is None:
                login = urllib.request.Request(BASE + '/api/auth/providers/native/login',
                    data=Path('/data2/monitoring/perses/admin-credentials.json').read_bytes(),
                    headers={'Content-Type': 'application/json'})
                with OPENER.open(login, timeout=20) as response:
                    TOKEN = json.load(response)['access_token']
            token = TOKEN
        request = urllib.request.Request(BASE + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with OPENER.open(request, timeout=45) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code == 401 and attempt == 0:
                with AUTH_LOCK:
                    if TOKEN == token:
                        TOKEN = None
                continue
            raise RuntimeError('Perses API HTTP ' + str(error.code)) from None


def fingerprint():
    records = json.loads(subprocess.check_output(['docker', 'inspect'] + list(SERVICES)))
    return [{'name': c['Name'], 'id': c['Id'], 'image': c['Image'], 'started': c['State']['StartedAt']}
            for c in records]


def snapshot():
    result = {'projects': api('/api/v1/projects'), 'dashboards': [], 'datasources': []}
    for project in result['projects']:
        for kind in ('dashboards', 'datasources'):
            result[kind].extend(api('/api/v1/projects/' + project['metadata']['name'] + '/' + kind))
    return result


def normalized(resources):
    return sorted([(d['kind'], d['metadata'].get('project', ''), d['metadata']['name'], d['spec'])
                   for ds in resources.values() for d in ds], key=lambda x: x[:3])


def save(root, name, data):
    path = root / name
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def expressions(document):
    return [q['spec']['plugin']['spec']['query'] for p in document['spec']['panels'].values()
            for q in p['spec'].get('queries', [])]


def query(expression, end, hours, step, nocache, project=None, datasource='victoriametrics', start=None):
    global TOKEN
    expression = expression.replace('$__interval', str(step) + 's')
    for variable in ('role', 'node', 'device'):
        expression = expression.replace('$' + variable, '.*')
    params = {'query': expression, 'start': end - hours * 3600 if start is None else start, 'end': end,
              'step': step, 'nocache': int(nocache)}
    url = VM + '/api/v1/query_range'
    headers = {}
    if project:
        if TOKEN is None:
            api('/api/v1/projects')
        url = BASE + '/proxy/projects/' + project + '/datasources/' + datasource + '/api/v1/query_range'
        headers = {'Authorization': 'Bearer ' + TOKEN}
    for attempt in range(2):
        token = TOKEN
        if project:
            headers = {'Authorization': 'Bearer ' + token}
        request = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), headers=headers)
        try:
            with OPENER.open(request, timeout=45) as response:
                result = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if project and error.code == 401 and attempt == 0:
                with AUTH_LOCK:
                    if TOKEN == token:
                        TOKEN = None
                api('/api/v1/projects')
                continue
            raise
    assert result['status'] == 'success' and not result.get('isPartial') and not result.get('warnings')
    return result['data']['result']


def canonical(rows):
    return sorted(rows, key=lambda row: json.dumps(row['metric'], sort_keys=True))


def equivalent(left, right):
    left, right = canonical(left), canonical(right)
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if a['metric'] != b['metric'] or len(a['values']) != len(b['values']):
            return False
        for x, y in zip(a['values'], b['values']):
            if x[0] != y[0] or (x[1] != y[1] and not math.isclose(float(x[1]), float(y[1]), rel_tol=1e-9, abs_tol=1e-10)):
                return False
    return True


def check_change(change, end, hours, step):
    old = []
    tagged = change['panel'] not in ('core-ttft', 'core-itl', 'core-e2e')
    for i, q in enumerate(change['before']['spec']['queries']):
        spec = q['spec']['plugin']['spec']
        rows = query(spec['query'], end, hours, step, True)
        if tagged:
            for row in rows:
                row['metric'][SERIES_LABEL] = spec['seriesNameFormat']
                row['metric'][ORDER_LABEL] = '%02d' % i
        else:
            for row in rows:
                row['metric'][ORDER_LABEL] = '%02d' % i
        old.extend(rows)
    expression = change['after']['spec']['queries'][0]['spec']['plugin']['spec']['query']
    new = query(expression, end, hours, step, True)
    proxy = query(expression, end, hours, step, True, change['project'])
    assert equivalent(old, new), (change['project'], change['panel'], 'semantics')
    assert equivalent(new, proxy), (change['project'], change['panel'], 'proxy')
    return {'project': change['project'], 'panel': change['panel'], 'hours': hours, 'step': step,
            'series': len(new), 'passed': True}


def audit(root, samples=41, resume=False):
    assert samples == 41, 'The admission cohort is fixed before measurement'
    if resume:
        before = json.loads((root / 'merge-before.json').read_text())
        assert normalized(snapshot()) == normalized(before), 'Concurrent resource edit'
        assert fingerprint() == json.loads((root / 'merge-services.json').read_text())
    else:
        assert not (root / 'merge-before.json').exists(), 'Use a fresh evidence directory'
        before = snapshot()
    after, changes = prepare(before)
    assert 0 < len(changes) <= 14
    if not resume:
        save(root, 'merge-before.json', before)
        save(root, 'merge-candidate.json', after)
        save(root, 'merge-changes.json', changes)
        save(root, 'merge-services.json', fingerprint())
        end = int(time.time() // 60) * 60 - 180
        checks = [check_change(c, end, hours, step) for c in changes
                  for hours, step in ((1, 5), (1, 15), (1, 60), (24, 60))]
        save(root, 'merge-semantics.json', {'passed': True, 'end': end, 'checks': checks})
        print('Semantic and proxy checks passed:', len(checks), flush=True)
    else:
        end = json.loads((root / 'merge-semantics.json').read_text())['end']
    identities = sorted(set((c['project'], c['dashboard']) for c in changes))
    report = {'passed': False, 'samples': samples, 'end': end, 'candidate_sha256': sha(after),
              'method': 'server-local authenticated Perses proxy; page query cohort; concurrency=3; not browser paint',
              'benchmarks': []}
    if resume:
        report = json.loads((root / 'merge-performance.json').read_text())
        assert report['candidate_sha256'] == sha(after)
    for project, dashboard in identities:
        documents = [next(d for d in resources['dashboards'] if d['metadata']['project'] == project
                          and d['metadata']['name'] == dashboard) for resources in (before, after)]
        groups = [expressions(d) for d in documents]
        for hours, step in ((1, 5), (24, 60)):
            for nocache in (True, False):
                if any((r['project'], r['dashboard'], r['hours'], r['nocache']) == (project, dashboard, hours, nocache)
                       for r in report['benchmarks']):
                    continue
                pairs = []
                for iteration in range(samples):
                    values = [None, None]
                    for version in ((0, 1) if iteration % 2 else (1, 0)):
                        started = time.monotonic()
                        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                            list(pool.map(lambda q: query(q, end, hours, step, nocache, project), groups[version]))
                        values[version] = time.monotonic() - started
                        time.sleep(.1)
                    pairs.append(values)
                old, new = [[pair[i] for pair in pairs] for i in (0, 1)]
                percentile = lambda values: sorted(values)[math.ceil(.95 * len(values)) - 1]
                row = {'project': project, 'dashboard': dashboard, 'hours': hours, 'step': step,
                       'nocache': nocache, 'pairs': pairs, 'before_requests': len(groups[0]),
                       'after_requests': len(groups[1]), 'before_p95': percentile(old),
                       'after_p95': percentile(new), 'before_median': statistics.median(old),
                       'after_median': statistics.median(new)}
                row['passed'] = row['after_p95'] <= row['before_p95'] * 1.05
                report['benchmarks'].append(row)
                save(root, 'merge-performance.json', report)
                print(project, dashboard, hours, nocache, 'P95', round(row['before_p95'], 3),
                      round(row['after_p95'], 3), row['passed'], flush=True)
    report['passed'] = all(row['passed'] for row in report['benchmarks'])
    report['resources_unchanged'] = normalized(snapshot()) == normalized(before)
    report['services_unchanged'] = fingerprint() == json.loads((root / 'merge-services.json').read_text())
    report['passed'] &= report['resources_unchanged'] and report['services_unchanged']
    save(root, 'merge-performance.json', report)


def resume(root):
    audit(root, resume=True)


def path(document):
    return '/api/v1/projects/' + document['metadata']['project'] + '/dashboards/' + document['metadata']['name']


def rollback(root):
    for entry in reversed(json.loads((root / 'merge-journal.json').read_text())):
        current = api(path(entry['after']))
        for key, before_panel in entry['before']['spec']['panels'].items():
            after_panel = entry['after']['spec']['panels'][key]
            if before_panel == after_panel: continue
            actual = current['spec']['panels'][key]
            if actual == before_panel: continue
            assert actual == after_panel, 'Concurrent target panel edit'
            current['spec']['panels'][key] = copy.deepcopy(before_panel)
        api(path(current), 'PUT', current)
        assert api(path(current))['spec'] == current['spec']
    from generator_transaction import rollback as restore_generators
    restore_generators(root)
    save(root, 'merge-rollback.json', {'passed': True, 'time': time.time()})


def apply(root, admitted_only=False, viewport=False):
    prefix = 'viewport' if viewport else 'merge'
    before = json.loads((root / (prefix + '-before.json')).read_text())
    after = json.loads((root / (prefix + '-candidate.json')).read_text())
    performance = json.loads((root / (prefix + '-performance.json')).read_text())
    browser = json.loads((root / 'merge-browser.json').read_text())
    synthetic = json.loads((root / 'merge-synthetic.json').read_text())
    assert performance['samples'] == 41 and performance['candidate_sha256'] == sha(after)
    changes = json.loads((root / 'merge-changes.json').read_text())
    identities = {(c['project'], c['dashboard']) for c in changes}
    assert performance['resources_unchanged'] and performance['services_unchanged']
    assert len(performance['benchmarks']) == 4 * len(identities)
    if viewport:
        assert len(performance['semantics']) == 2 * len(changes) and all(c['passed'] for c in performance['semantics'])
        assert all(c['passed'] for c in performance['health'])
        for row in performance['benchmarks']:
            assert len(row['pairs']) == 41
            passed = row['page']['passed'] and row['probe']['passed']
            if 'target' in row:
                target = row['target']
                assert target['performance_policy'] == 'faster-median-v1'
                old, new = [statistics.median(p[v]['target'] for p in row['pairs']) for v in (0, 1)]
                assert target['passed'] == (new < old)
                passed = passed and target['passed'] and row['target_probe']['passed']
            assert row['passed'] == passed
    else:
        non_target = json.loads((root / 'non-target-performance.json').read_text())
        assert non_target['passed'] and non_target['candidate_sha256'] == sha(after), 'Non-target performance gate failed'
    admitted = {(r['project'], r['dashboard']) for r in performance['benchmarks']}
    if admitted_only:
        admitted = {identity for identity in admitted if all(r['passed'] for r in performance['benchmarks']
                    if (r['project'], r['dashboard']) == identity)}
        assert admitted, 'No complete dashboard cohort passed admission'
    else:
        assert performance['passed']
    assert browser['passed'] and browser['candidate_sha256'] == sha(after)
    assert synthetic['passed'] and synthetic['candidate_sha256'] == sha(after)
    for i, document in enumerate(after['dashboards']):
        if (document['metadata']['project'], document['metadata']['name']) not in admitted:
            after['dashboards'][i] = copy.deepcopy(next(d for d in before['dashboards'] if path(d) == path(document)))
    assert normalized(snapshot()) == normalized(before), 'Concurrent resource edit'
    assert fingerprint() == json.loads((root / (prefix + '-services.json')).read_text())
    assert not (root / 'merge-journal.json').exists()
    selected = [c for c in changes if (c['project'], c['dashboard']) in admitted]
    from generator_transaction import plan as plan_generators, install as install_generators
    plan_generators(root, selected)
    journal = []
    save(root, 'merge-journal.json', journal)
    try:
        for candidate in after['dashboards']:
            old = next(d for d in before['dashboards'] if path(d) == path(candidate))
            if old['spec'] == candidate['spec']:
                continue
            current = api(path(old))
            assert current['spec'] == old['spec'], 'Concurrent dashboard edit'
            new = copy.deepcopy(candidate)
            new['metadata'] = current['metadata']
            journal.append({'before': current, 'after': new})
            save(root, 'merge-journal.json', journal)
            api(path(new), 'PUT', new)
            assert api(path(new))['spec'] == new['spec']
        install_generators(root)
        assert normalized(snapshot()) == normalized(after)
        assert fingerprint() == json.loads((root / (prefix + '-services.json')).read_text())
        save(root, 'merge-publication.json', {'passed': True, 'time': time.time(), 'dashboards': len(journal),
             'panels': len(selected), 'admitted': sorted(admitted), 'queries': sum(len(expressions(d)) for d in after['dashboards'])})
    except BaseException:
        rollback(root)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('audit', 'resume', 'apply', 'rollback'))
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--admitted-only', action='store_true')
    parser.add_argument('--viewport', action='store_true')
    args = parser.parse_args()
    assert args.evidence.is_dir()
    if args.action == 'apply': apply(args.evidence, args.admitted_only, args.viewport)
    else: globals()[args.action](args.evidence)
