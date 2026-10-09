"""Exact panel-expression fixtures on a disposable loopback VM, also for browser QA."""
import argparse
import copy
import json
import math
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'perses'))
from query_slimming import BATCHES, RESULTS, plugin, prepare
from merge_release import equivalent, sha, save

CASES = ('normal', 'zero', 'absent-result', 'gap-result')


def fixtures(changes, base):
    histograms = {}
    for c in changes:
        if c['kind'] != 'histogram-monotonic':
            continue
        expr = plugin(c['before']['spec']['queries'][0])['query']
        selector = re.search(r'([\w:]+)_bucket\{([^{}]+)\}', expr)
        name, body = selector.groups()
        labels = dict(re.findall(r'(\w+)="([^"\\]*)"', body))
        bounds = sorted(set(re.findall(r'\ble="([^"]+)"', expr)), key=float)
        nodes = ('a3-1', 'a3-2') if name.startswith('vllm:') else ('gateway',)
        for node in nodes:
            histograms[(name, labels['environment'], node)] = (labels, bounds)
    ends = {case: base + i * 1200 + 600 for i, case in enumerate(CASES)}
    for case, end in ends.items():
        for i in range(121):
            tick = end - 600 + i * 5
            seen = set()
            def emit(name, value, labels):
                identity = name + '{' + ','.join(k + '=' + json.dumps(v) for k, v in sorted(labels.items())) + '}'
                if identity in seen:
                    return None
                seen.add(identity)
                return identity + ' ' + str(value) + ' ' + str(tick * 1000)
            for env in ('a3-vllm', 'dcu-pd', 'xpu-pd'):
                labels = dict(environment=env, job='aigate', instance='gateway')
                yield emit('up', 1, labels)
                source = dict(labels, request_scope='all')
                for metric in ('aigate_error_metrics_start_time_seconds', 'aigate_profile_counter_start_time_seconds'):
                    yield emit(metric, base - 8000, source)
                for result, factor in [('completed', 4), ('error', 3), ('client_cancelled', 1), ('client_disconnected', 2), ('unknown', 3)]:
                    if result == 'client_cancelled' and (case == 'absent-result' or (case == 'gap-result' and i == 118)):
                        continue
                    yield emit('aigate_generation_requests_ended_total', 100 if case == 'zero' else (i + 100) * 5 * factor,
                               dict(source, result=result))
            for (name, env, node), (labels, bounds) in histograms.items():
                source = dict(environment=env, job=labels['job'], instance=node)
                if node != 'gateway': source['node'] = node
                yield emit('up', 1, source)
                metric_labels = dict(source, **{k: v for k, v in labels.items() if k not in source})
                if node == 'gateway':
                    metric_labels.update(backend='fixture', model='fixture')
                    yield emit('aigate_profile_group_start_time_seconds', base - 8000,
                               dict(metric_labels, request_scope='all'))
                else:
                    metric_labels['engine'] = '0'
                    yield emit(name + '_created', base - 8000, metric_labels)
                value = 100 if case == 'zero' else i + 100
                for index, bound in enumerate(bounds, 1):
                    yield emit(name + '_bucket', value * index, dict(metric_labels, le=bound))
                yield emit(name + '_count', value * len(bounds), metric_labels)
    return ends


def run(snapshot, root, vm, batch):
    assert vm.startswith('http://127.0.0.1:'), 'Fixtures only belong in a disposable loopback VM'
    before = json.loads(snapshot.read_text())
    after = copy.deepcopy(before)
    changes = []
    for name in (BATCHES if batch == 'all' else (batch,)):
        after, current = prepare(after, name)
        changes.extend(current)
    assert changes
    base = int(time.time() // 60) * 60 - 6000
    ends = {case: base + i * 1200 + 600 for i, case in enumerate(CASES)}
    report = {'passed': False, 'candidate_sha256': sha(after), 'vm': vm, 'synthetic': True,
              'scope': 'exact panel expressions; full anomaly/seven-step suite is test_query_slimming_vm.py', 'checks': []}
    with httpx.Client(base_url=vm, timeout=60, trust_env=False) as client:
        lines = []
        for line in fixtures(changes, base):
            if line is None: continue
            lines.append(line)
            if len(lines) >= 10000:
                client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n').raise_for_status()
                lines.clear()
        if lines: client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n').raise_for_status()
        client.get('/internal/force_flush').raise_for_status()
        def rows(panel, change, end, step):
            result = []
            for q in panel['spec']['queries']:
                spec = plugin(q)
                expression = spec['query'].replace('$__interval', str(step) + 's').replace('$role', '.*').replace('$node', '.*')
                response = client.post('/api/v1/query_range', data={'query': expression, 'start': end, 'end': end,
                                      'step': step, 'nocache': 1, 'latency_offset': '1ms'})
                response.raise_for_status()
                data = response.json()
                assert data['status'] == 'success' and not data.get('warnings') and not data.get('isPartial')
                for row in data['data']['result']:
                    if change['kind'] == 'generation-results':
                        labels = row['metric']
                        labels['perses_series'] = labels.get('perses_series', spec['seriesNameFormat'])
                        labels.pop('perses_order', None)
                    result.append(row)
            return result
        for case, end in ends.items():
            for step in (5, 15, 60):
                for c in changes:
                    old, new = [rows(c[v], c, end, step) for v in ('before', 'after')]
                    assert equivalent(old, new), (c['project'], c['panel'], case, step)
                    if c['kind'] == 'generation-results':
                        want = {'全部结束', *(label for _, label in RESULTS)}
                        if case == 'absent-result': want.remove('客户端取消')
                        if case == 'gap-result': want -= {'全部结束', '客户端取消'}
                        assert {r['metric']['perses_series'] for r in new} == want
                        if case == 'zero': assert all(float(r['values'][0][1]) == 0 for r in new)
                    else:
                        assert bool(new) == (case != 'zero'), (c['panel'], case, new)
                        assert all(math.isfinite(float(r['values'][0][1])) for r in new)
                    report['checks'].append({'project': c['project'], 'dashboard': c['dashboard'], 'panel': c['panel'], 'case': case, 'step': step, 'series': len(new), 'passed': True})
    report['passed'] = True
    for name, data in [('query-before.json', before), ('query-candidate.json', after), ('query-changes.json', changes),
                       ('query-synthetic.json', report), ('query-fixtures.json', {'ends': ends, 'base': base})]:
        save(root, name, data)
    print(json.dumps({'passed': True, 'checks': len(report['checks']), 'candidate_sha256': report['candidate_sha256']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--vm', required=True)
    parser.add_argument('--batch', choices=(*BATCHES, 'all'), required=True)
    args = parser.parse_args()
    assert args.evidence.is_dir()
    run(args.snapshot, args.evidence, args.vm, args.batch)
