"""Read-only real-environment equivalence probe for available shadow coverage."""
import argparse
import itertools
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from merge_release import equivalent, save


def get_health():
    with urllib.request.urlopen('http://127.0.0.1:18430/health', timeout=10) as response: return json.load(response)


def query(base, expr, start, end, step):
    params = urllib.parse.urlencode({'query': expr, 'start': start, 'end': end, 'step': step, 'nocache': '1'}).encode()
    with urllib.request.urlopen(base + '/api/v1/query_range', data=params, timeout=45) as response: result = json.load(response)
    assert result['status'] == 'success' and not result.get('isPartial') and not result.get('warnings')
    return result['data']['result']


def main(root, catalog_path):
    catalog = json.loads(catalog_path.read_text())
    health = get_health(); jobs = {j['job']: j for j in health['perses_acceleration']['jobs']}
    assert not health['perses_acceleration']['disabled_groups']
    report = {'passed': False, 'production': True, 'performance_admission': False, 'started_at': time.time(), 'checks': [], 'unstable_source': []}
    for panel in catalog['panels']:
        for step in catalog['steps']:
            job = jobs[panel['id'] + ':' + panel['revision'] + ':' + str(step)]
            assert job['processed_at'] >= job['started_at']
            end = int(job['processed_at']); start = max(int(job['started_at']), end - 60 * step)
            keys = sorted(panel['variables'])
            for values in itertools.product(*(panel['variables'][key] for key in keys)):
                expr = panel['expression'].replace('$__interval', str(step) + 's')
                for key, value in zip(keys, values): expr = expr.replace('$' + key, value)
                scenarios = [('covered', start, end), ('cross-history', int(job['started_at']) - 2 * step, end),
                             ('fractional', start + .123, end + .123)]
                if step == 5: scenarios.append(('realtime-tail', start, int(time.time() // 5) * 5))
                for mode, left, right in scenarios:
                    old = query('http://127.0.0.1:18428', expr, left, right, step)
                    fast = query('http://127.0.0.1:18430/internal/perses', expr, left, right, step)
                    if not equivalent(old, fast):
                        again = query('http://127.0.0.1:18428', expr, left, right, step)
                        if mode == 'realtime-tail' and not equivalent(old, again):
                            report['unstable_source'].append({'panel': panel['id'], 'step': step, 'mode': mode})
                            continue
                        save(root, 'online-mismatch.json', {'panel': panel['id'], 'step': step, 'filters': list(values), 'mode': mode, 'old': old, 'fast': fast, 'again': again})
                        raise AssertionError(('Environment mismatch', panel['id'], step, mode, values))
                    report['checks'].append({'panel': panel['id'], 'step': step, 'filters': list(values), 'mode': mode, 'series': len(fast)})
        save(root, 'online-comparison.json', report)
        print(panel['id'], 'checked', len(report['checks']), flush=True)
    after = get_health()
    assert after['perses_acceleration']['counters']['fast_requests'] > health['perses_acceleration']['counters']['fast_requests']
    report.update(passed=True, ended_at=time.time(), fast_requests=after['perses_acceleration']['counters']['fast_requests']-health['perses_acceleration']['counters']['fast_requests'])
    save(root, 'online-comparison.json', report)


def tail(root, catalog_path):
    catalog = json.loads(catalog_path.read_text())
    checks = []
    for panel in catalog['panels']:
        for values in itertools.product(*(panel['variables'][key] for key in sorted(panel['variables']))):
            expr = panel['expression'].replace('$__interval', '5s')
            for key, value in zip(sorted(panel['variables']), values): expr = expr.replace('$' + key, value)
            # Still inside the forced-raw last 60s, but beyond VM's moving
            # latency offset so a boundary advancing mid-comparison is avoided.
            end = int(time.time() // 5) * 5 - 40; start = end - 300
            old = query('http://127.0.0.1:18428', expr, start, end, 5)
            fast = query('http://127.0.0.1:18430/internal/perses', expr, start, end, 5)
            again = query('http://127.0.0.1:18428', expr, start, end, 5)
            assert equivalent(old, again), ('Source changed during tail control', panel['id'])
            assert equivalent(old, fast), ('Stable tail mismatch', panel['id'], values)
            checks.append({'panel': panel['id'], 'filters': list(values), 'start': start, 'end': end, 'series': len(fast)})
    save(root, 'tail-comparison.json', {'passed': True, 'checks': checks, 'production': True, 'performance_admission': False})
    print('Stable realtime-tail controls passed:', len(checks), flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--evidence', type=Path, required=True); parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--tail-only', action='store_true')
    args=parser.parse_args(); (tail if args.tail_only else main)(args.evidence,args.catalog)
