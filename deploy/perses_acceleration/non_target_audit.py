"""41 paired non-target probes under old/new admitted query-cohort load."""
import argparse
import concurrent.futures
import json
import math
import statistics
import time
import urllib.request
from pathlib import Path

from merge_release import query, snapshot, normalized, fingerprint, sha, save
from dashboard_columns import section_panels


def main(root):
    before = json.loads((root / 'merge-before.json').read_text())
    after = json.loads((root / 'merge-candidate.json').read_text())
    performance = json.loads((root / 'merge-performance.json').read_text())
    changes = json.loads((root / 'merge-changes.json').read_text())
    assert performance['resources_unchanged'] and performance['services_unchanged']
    assert normalized(snapshot()) == normalized(before)
    assert fingerprint() == json.loads((root / 'merge-services.json').read_text())
    identities = {(r['project'], r['dashboard']) for r in performance['benchmarks']}
    admitted = {identity for identity in identities if all(r['passed'] for r in performance['benchmarks']
        if (r['project'], r['dashboard']) == identity)}
    selected = [c for c in changes if (c['project'], c['dashboard']) in admitted]
    assert selected
    end = int(time.time() // 60) * 60 - 180
    report = {'passed': False, 'samples': 41, 'candidate_sha256': sha(after), 'end': end,
              'method': 'non-target probes under admitted old/new target-cohort load; concurrency=3; alternating pairs', 'cohorts': [], 'health': []}
    for project in sorted({c['project'] for c in selected}):
        probes = []
        for dashboard in ('backend-performance', 'accelerator-resources', 'monitoring-health'):
            doc, key = next((doc, key) for doc, key in section_panels(before, project, dashboard)
                if not any(c['project'] == project and c['dashboard'] == doc['metadata']['name'] and c['panel'] == key for c in changes))
            panel = doc['spec']['panels'][key]
            probes.append((doc['metadata']['name'] + '/' + key, panel['spec']['queries'][0]['spec']['plugin']['spec']['query']))
        groups = [[q['spec']['plugin']['spec']['query'] for c in selected if c['project'] == project
                   for q in c[version]['spec']['queries']] for version in ('before', 'after')]
        for hours, step in ((1, 5), (24, 60)):
            for nocache in (True, False):
                results = {name: [] for name, _ in probes}
                for iteration in range(41):
                    measured = [{}, {}]
                    for version in ((0, 1) if iteration % 2 else (1, 0)):
                        def work(item):
                            name, expression = item
                            started = time.monotonic()
                            query(expression, end, hours, step, nocache, project)
                            return name, time.monotonic() - started
                        items = [(None, q) for q in groups[version]] + probes
                        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                            for name, elapsed in pool.map(work, items):
                                if name is not None: measured[version][name] = elapsed
                        time.sleep(.1)
                    for name, _ in probes:
                        results[name].append([measured[0][name], measured[1][name]])
                for name, pairs in results.items():
                    old, new = [[row[i] for row in pairs] for i in (0, 1)]
                    percentile = lambda values: sorted(values)[math.ceil(len(values) * .95) - 1]
                    row = {'project': project, 'probe': name, 'hours': hours, 'step': step, 'nocache': nocache,
                           'pairs': pairs, 'before_p95': percentile(old), 'after_p95': percentile(new),
                           'before_median': statistics.median(old), 'after_median': statistics.median(new)}
                    row['passed'] = row['after_p95'] <= row['before_p95'] * 1.05
                    report['cohorts'].append(row)
                with urllib.request.urlopen('http://127.0.0.1:18430/health', timeout=10) as response:
                    health = json.load(response)
                lags = {env: time.time() - data['processed_at'] for env, data in health['environments'].items()}
                report['health'].append({'at': time.time(), 'status': health['status'], 'model_lags': lags,
                    'passed': health['status'] == 'ok' and max(lags.values()) < 20 and all(v['error'] is None for v in health['environments'].values())})
                save(root, 'non-target-performance.json', report)
                print(project, hours, nocache, 'probes passed', sum(r['passed'] for r in report['cohorts'][-3:]), '/ 3', flush=True)
    report['resources_unchanged'] = normalized(snapshot()) == normalized(before)
    report['services_unchanged'] = fingerprint() == json.loads((root / 'merge-services.json').read_text())
    report['passed'] = all(r['passed'] for r in report['cohorts'] + report['health']) and report['resources_unchanged'] and report['services_unchanged']
    save(root, 'non-target-performance.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--evidence', type=Path, required=True)
    main(parser.parse_args().evidence)
