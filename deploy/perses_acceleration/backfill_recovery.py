"""Supervise temporary parallel backfill, restore normal scheduling, then admit observation.

This is not a stability or performance pass. All server execution uses SSH MCP.
"""
import argparse
import json
import time
from pathlib import Path

from admin import update
from merge_release import save
from release_api import health, inspect, protected

STATE = Path('/data2/monitoring/state/perses-acceleration-admin.json')


def main(root, hours=8, recovery_seconds=3600):
    expected = json.loads((root / 'shadow-started.json').read_text())
    catalog = json.loads((root / 'perses_acceleration_catalog.json').read_text())
    groups = {p['id']: p['group'] for p in catalog['panels']}
    expected_jobs = {p['id'] + ':' + p['revision'] + ':' + str(s)
                     for p in catalog['panels'] for s in catalog['steps']}
    started = time.time(); deadline = started + hours * 3600
    recovery_at = None
    bad = {g: 0 for g in catalog['groups']}
    transport_bad = 0
    owned = False
    try:
        while time.time() < deadline:
            current = inspect('monitoring-api')
            owned = current['Id'] == expected['container_id'] and current['Image'] == expected['image']
            assert owned, 'API changed; stop stale supervisor without changing its state'
            try:
                result = health(); transport_bad = 0
            except (OSError, ValueError) as error:
                transport_bad += 1
                if transport_bad >= 3: raise RuntimeError('Health unavailable repeatedly') from error
                time.sleep(30)
                continue
            acceleration = result['perses_acceleration']; jobs = acceleration['jobs']
            assert {j['job'] for j in jobs} == expected_jobs, 'Unexpected worker catalog'
            assert not acceleration['disabled_groups'], 'Worker group was stopped'
            for group in catalog['groups']:
                selected = [j for j in jobs if groups[j['job'].rsplit(':', 2)[0]] == group]
                fault = acceleration['state_error'] or any(j['error'] or (j.get('backfill') or {}).get('error') for j in selected)
                bad[group] = bad[group] + 1 if fault else 0
                if bad[group] >= 3:
                    update(STATE, 'disable', group=group)
                    raise RuntimeError('Persistent materialization failure: ' + group)
            complete = all(j.get('backfill') and j['backfill']['watermark'] == j['backfill']['end'] for j in jobs)
            lags = {k: time.time() - v['processed_at'] for k, v in result['environments'].items()}
            if complete and recovery_at is None:
                update(STATE, 'parallel-a3-off')
                recovery_at = time.time()
            normal = (complete and not acceleration.get('parallel_a3_backfill')
                      and not acceleration.get('backfill_priority_active')
                      and result['status'] == 'ok' and all(0 <= v < 20 for v in lags.values())
                      and all(v['error'] is None for v in result['environments'].values())
                      and not any(bad.values())
                      and all(j['lag_seconds'] <= max(300, int(j['job'].rsplit(':', 1)[1])) for j in jobs))
            record = {'at': time.time(), 'started_at': started, 'backfill_complete': complete,
                      'state': 'recovering' if complete else 'backfilling', 'recovery_started_at': recovery_at,
                      'priority_active': acceleration.get('backfill_priority_active'),
                      'parallel_a3': acceleration.get('parallel_a3_backfill'), 'model_lags': lags,
                      'max_live_lag': max(j['lag_seconds'] for j in jobs),
                      'completed_jobs': sum(bool(j.get('backfill')) and j['backfill']['watermark'] == j['backfill']['end'] for j in jobs),
                      'total_jobs': len(jobs), 'consecutive_errors': dict(bad)}
            save(root, 'backfill-recovery.json', record)
            with (root / 'backfill-recovery.jsonl').open('a') as log:
                log.write(json.dumps(record) + '\n')
            if normal:
                save(root, 'recovery-ready.json', dict(record, state='ready_for_acceptance'))
                return
            if recovery_at is not None:
                assert time.time() - recovery_at < recovery_seconds, 'Normal processing did not recover within the bounded window'
            time.sleep(30)
        raise RuntimeError('Bounded backfill supervision expired')
    except BaseException as error:
        # Never mutate a replacement container's shared state.
        if owned:
            update(STATE, 'parallel-a3-off')
        save(root, 'backfill-recovery-failure.json', {'at': time.time(), 'type': type(error).__name__, 'error': str(error)[:500]})
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--hours', type=float, default=8)
    args = parser.parse_args()
    assert args.build.is_dir() and 0 < args.hours <= 8
    main(args.build, args.hours)
