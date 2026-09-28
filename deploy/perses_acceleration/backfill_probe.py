"""Read-only comparison of newly backfilled history; not performance admission."""
import argparse
import itertools
import json
import time
from pathlib import Path

from environment_probe import get_health, query
from merge_release import equivalent, save


def main(root, catalog_path):
    catalog = json.loads(catalog_path.read_text())
    before = get_health()['perses_acceleration']
    jobs = {j['job']: j for j in before['jobs']}
    report = {'passed': False, 'production': True, 'performance_admission': False,
              'started_at': time.time(), 'checks': [], 'skipped_jobs': []}
    for panel in catalog['panels']:
        for step in catalog['steps']:
            key = panel['id'] + ':' + panel['revision'] + ':' + str(step)
            job = jobs[key]; history = job.get('backfill')
            if not history or history['watermark'] < history['start']:
                report['skipped_jobs'].append(key)
                continue
            assert panel['group'] not in before['disabled_groups']
            assert not history['error'] and not job['error']
            start = history['start']; end = min(history['watermark'], start + 59 * step)
            for values in itertools.product(*(panel['variables'][k] for k in sorted(panel['variables']))):
                expr = panel['expression'].replace('$__interval', str(step) + 's')
                for key, value in zip(sorted(panel['variables']), values): expr = expr.replace('$' + key, value)
                for mode, left in (('backfilled', start), ('old-history-boundary', start - 2 * step)):
                    raw = query('http://127.0.0.1:18428', expr, left, end, step)
                    accelerated = query('http://127.0.0.1:18430/internal/perses', expr, left, end, step)
                    if not equivalent(raw, accelerated):
                        save(root, 'backfill-mismatch.json', {'panel': panel['id'], 'step': step,
                             'filters': values, 'mode': mode, 'raw': raw, 'accelerated': accelerated})
                        raise AssertionError(('Backfilled history differs', panel['id'], step, mode))
                    report['checks'].append({'panel': panel['id'], 'step': step, 'filters': values,
                                            'mode': mode, 'start': left, 'end': end, 'series': len(raw)})
    after = get_health()['perses_acceleration']
    report['fast_hits'] = after['counters']['fast_requests'] - before['counters']['fast_requests']
    assert report['checks'] and report['fast_hits'] > 0
    report.update(passed=True, ended_at=time.time())
    assert not (root / 'backfill-comparison.json').exists(), 'Preserve previous evidence'
    save(root, 'backfill-comparison.json', report)
    print('Backfilled-history comparisons passed:', len(report['checks']), 'fast hits:', report['fast_hits'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    args = parser.parse_args()
    main(args.evidence, args.catalog)
