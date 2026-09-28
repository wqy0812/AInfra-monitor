"""Bounded shadow-only watch; stops faulty groups, never publishes dashboards."""
import argparse
import json
import time
import urllib.request
from pathlib import Path

from admin import update
from merge_release import save

STATE = Path('/data2/monitoring/state/perses-acceleration-admin.json')
PUBLICATION = Path('/data2/monitoring/perses/release/acceleration_state.json')


def main(root, hours, catchup_seconds=0):
    catalog = json.loads((root / 'perses_acceleration_catalog.json').read_text())
    groups = {p['id']: p['group'] for p in catalog['panels']}
    bad = {group: 0 for group in catalog['groups']}
    start = time.time(); deadline = start + hours * 3600
    while time.time() < deadline:
        if PUBLICATION.exists() and json.loads(PUBLICATION.read_text())['groups']:
            save(root, 'shadow-watch.json', {'state': 'handed_over_to_batch_observation', 'at': time.time()})
            return
        admin = json.loads(STATE.read_text()) if STATE.exists() else {'disabled_groups': []}
        active = set(catalog['groups']) - set(admin['disabled_groups'])
        if not active:
            save(root, 'shadow-watch.json', {'state': 'stopped_groups_disabled', 'at': time.time()})
            return
        try:
            with urllib.request.urlopen('http://127.0.0.1:18430/health', timeout=10) as response: health = json.load(response)
            acceleration = health['perses_acceleration']; jobs = acceleration['jobs']
            maintenance = acceleration.get('backfill_priority_active', False)
            model_lags = {k: time.time() - v['processed_at'] for k, v in health['environments'].items()}
            model_ok = health['status'] == 'ok' and max(model_lags.values()) < 20 and all(v['error'] is None for v in health['environments'].values())
            summary = {}
            for group in sorted(active):
                selected = [j for j in jobs if groups[j['job'].rsplit(':', 2)[0]] == group]
                fault = (not model_ok and not maintenance) or acceleration['state_error'] or any(j['error'] or (j.get('backfill') or {}).get('error') for j in selected)
                # Allow the normal batching/visibility delay. Two due long-step
                # samples or a ten-minute short-step backlog require intervention.
                catching_up = maintenance or time.time() - start < catchup_seconds
                fault = fault or (not catching_up and any(j['lag_seconds'] > max(600, 2 * int(j['job'].rsplit(':', 1)[1])) for j in selected))
                bad[group] = bad[group] + 1 if fault else 0
                summary[group] = {'min_coverage_seconds': min(j['processed_at']-j.get('coverage_start', j['started_at']) for j in selected),
                                  'backfill_completed_jobs': sum(bool(j.get('backfill')) and j['backfill']['watermark'] == j['backfill']['end'] for j in selected),
                                  'backfill_total_jobs': len(selected),
                                  'catching_up': catching_up,
                                  'max_lag_seconds': max(j['lag_seconds'] for j in selected), 'consecutive_bad': bad[group]}
            record = {'at': time.time(), 'backfill_priority_active': maintenance, 'model_lags': model_lags, 'groups': summary, 'counters': acceleration['counters']}
        except (OSError, ValueError, KeyError) as error:
            for group in active: bad[group] += 1
            record = {'at': time.time(), 'error': type(error).__name__ + ': ' + str(error)[:160], 'groups': {g: {'consecutive_bad': bad[g]} for g in active}}
        with (root / 'shadow-watch.jsonl').open('a') as log: log.write(json.dumps(record) + '\n')
        stopped = []
        for group in sorted(active):
            if bad[group] >= 3:
                update(STATE, 'disable', group=group)
                stopped.append(group)
        save(root, 'shadow-watch.json', {'state': 'groups_stopped' if stopped else 'building_coverage', 'started_at': start, 'stopped_groups': stopped, **record})
        ready = [g for g, v in record['groups'].items() if v.get('min_coverage_seconds', -1) >= 43200 and not bad[g]]
        if active == set(ready):
            save(root, 'shadow-ready.json', {'at': time.time(), 'groups': ready, 'next': '12h performance and 24h mixed-history correctness; no automatic datasource switch'})
            print('12-hour watermarks ready for admission:', ', '.join(ready), flush=True)
            return
        time.sleep(30)
    save(root, 'shadow-watch.json', {'state': 'watch_window_ended', 'at': time.time(), 'started_at': start})


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--evidence', type=Path, required=True); parser.add_argument('--hours', type=float, default=26)
    parser.add_argument('--catchup-seconds', type=int, default=0, help='Bounded live-watermark recovery after an intentional pause; health/errors are still guarded')
    args=parser.parse_args(); assert args.evidence.is_dir() and 0 < args.hours <= 48
    assert 0 <= args.catchup_seconds <= 600
    main(args.evidence,args.hours,args.catchup_seconds)
