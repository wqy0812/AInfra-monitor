"""Replay actual 1080p initial queries through the environment's Perses proxy.

An external unchanged probe has its own executor and begins with the page.
It never waits behind a version-dependent target queue. Report service latency
and page completion separately; these are data timings, not browser paint.
"""
import argparse
import concurrent.futures
import json
import math
import statistics
import threading
import time
import urllib.request
from pathlib import Path

from merge_release import api, query, prepare, save, sha, snapshot, normalized, fingerprint, check_change


def percentile(values):
    return sorted(values)[math.ceil(len(values) * .95) - 1]


def run_page(requests, project, end, hours, nocache, probe):
    start_gate = threading.Barrier(2)
    def page():
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            start_gate.wait()
            begin = time.monotonic()
            def work(request):
                query(request['query'], end, hours, int(request['step']), nocache, project)
            list(pool.map(work, requests))
            return time.monotonic() - begin
    def external():
        start_gate.wait()
        begin = time.monotonic()
        query(probe, end, hours, int(requests[0]['step']), nocache, project)
        return time.monotonic() - begin
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(page), pool.submit(external)
        return {'page': first.result(), 'probe': second.result()}


def main(root, browser, windows=(1, 24), target_timing=False):
    capture = json.loads(browser.read_text())
    assert capture['passed'] and capture['viewport'] == {'width': 1920, 'height': 1080}
    assert not (root / 'viewport-performance.json').exists(), 'Preserve prior measurements'
    with urllib.request.urlopen('http://127.0.0.1:18430/health', timeout=10) as response: current = json.load(response)
    accelerator = current['perses_acceleration']
    assert current['status'] == 'ok' and not accelerator['disabled_groups'] and not accelerator['state_error']
    assert all(j['processed_at'] >= j['started_at'] and not j['error'] for j in accelerator['jobs']), 'Wait for verified initial coverage'
    before = snapshot(); after, changes = prepare(before); services = fingerprint()
    assert 0 < len(changes) <= 14
    save(root, 'viewport-before.json', before); save(root, 'viewport-candidate.json', after)
    save(root, 'viewport-services.json', services)
    # Map captures back to exact frozen resources; they must not introduce queries.
    for c in capture['cohorts']:
        resource = before if c['version'] == 'before' else after
        document = next(d for d in resource['dashboards'] if (d['metadata']['project'], d['metadata']['name']) == (c['project'], c['dashboard']))
        for request in c['requests']:
            allowed = []
            for panel in document['spec']['panels'].values():
                for q in panel['spec'].get('queries', []):
                    expression = q['spec']['plugin']['spec']['query']
                    for name in ('role', 'node', 'device'): expression = expression.replace('$' + name, '.*')
                    step = int(request['step'])
                    intervals = [str(step) + 's'] + ([str(step // 60) + 'm'] if step % 60 == 0 else [])
                    allowed.extend(expression.replace('$__interval', interval) for interval in intervals)
            assert request['query'] in allowed, 'Browser expression differs from fresh environment snapshot'
    end = int(time.time() // 3600) * 3600 - 3600
    report = {'passed': False, 'samples': 41, 'end': end, 'candidate_sha256': sha(after),
              'browser_capture_sha256': sha(capture), 'method': 'actual 1080p initial query order; server-side authenticated proxy; page concurrency 3; independent unchanged probe starts concurrently; data completion, not paint',
              'benchmarks': [], 'health': []}
    report['semantics'] = [check_change(c, end, hours, step) for c in changes for hours, step in ((1, 5), (24, 120))]
    identities = sorted({(c['project'], c['dashboard']) for c in changes})
    api('/api/v1/projects')
    for project, dashboard in identities:
        health_doc = next(d for d in before['dashboards'] if d['metadata']['project'] == project and d['metadata']['name'] == 'monitoring-health')
        probe = next(iter(health_doc['spec']['panels'].values()))['spec']['queries'][0]['spec']['plugin']['spec']['query']
        selected = [c for c in changes if (c['project'], c['dashboard']) == (project, dashboard)]
        for hours in windows:
            groups = [next(c['requests'] for c in capture['cohorts'] if (c['project'], c['dashboard'], c['hours'], c['version']) == (project, dashboard, hours, version)) for version in ('before', 'after')]
            target_groups = [[{'query': q['spec']['plugin']['spec']['query'], 'step': groups[0][0]['step']}
                              for c in selected for q in c[version]['spec']['queries']] for version in ('before', 'after')]
            for nocache in (True, False):
                pairs = []
                for iteration in range(41):
                    measured = [None, None]
                    for version in ((0, 1) if iteration % 2 else (1, 0)):
                        measured[version] = run_page(groups[version], project, end, hours, nocache, probe)
                        if target_timing:
                            target = run_page(target_groups[version], project, end, hours, nocache, probe)
                            measured[version].update(target=target['page'], target_probe=target['probe'])
                        time.sleep(.1)
                    pairs.append(measured)
                row = {'project': project, 'dashboard': dashboard, 'hours': hours, 'step': int(groups[0][0]['step']), 'nocache': nocache,
                       'before_requests': len(groups[0]), 'after_requests': len(groups[1]), 'pairs': pairs,
                       'identical_initial_queries': [r['query'] for r in groups[0]] == [r['query'] for r in groups[1]]}
                for kind in (('page', 'probe', 'target_probe') if target_timing else ('page', 'probe')):
                    old, new = [percentile([p[v][kind] for p in pairs]) for v in (0, 1)]
                    row[kind] = {'before_p95': old, 'after_p95': new, 'passed': new <= old * 1.05}
                row['passed'] = row['page']['passed'] and row['probe']['passed']
                if target_timing:
                    old, new = [statistics.median(p[v]['target'] for p in pairs) for v in (0, 1)]
                    row['target'] = {'before_median': old, 'after_median': new, 'passed': new < old,
                                     'before_requests': len(target_groups[0]), 'after_requests': len(target_groups[1]),
                                     'performance_policy': 'faster-median-v1'}
                    row['passed'] &= row['target']['passed'] and row['target_probe']['passed']
                report['benchmarks'].append(row)
                with urllib.request.urlopen('http://127.0.0.1:18430/health', timeout=10) as response: health = json.load(response)
                lags = {k: time.time() - v['processed_at'] for k, v in health['environments'].items()}
                report['health'].append({'at': time.time(), 'model_lags': lags, 'passed': health['status'] == 'ok' and max(lags.values()) < 20})
                save(root, 'viewport-performance.json', report)
                print(project, dashboard, hours, nocache, 'page', row['page'], 'probe', row['probe'], 'target', row.get('target'), flush=True)
    report['resources_unchanged'] = normalized(snapshot()) == normalized(before)
    report['services_unchanged'] = fingerprint() == services
    report['passed'] = all(r['passed'] for r in report['benchmarks'] + report['health']) and report['resources_unchanged'] and report['services_unchanged']
    save(root, 'viewport-performance.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--evidence', type=Path, required=True); parser.add_argument('--browser', type=Path, required=True)
    args = parser.parse_args(); main(args.evidence, args.browser)
