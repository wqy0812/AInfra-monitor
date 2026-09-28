"""Read-only A/A timing control; never authorizes publication."""
import fcntl
import json
import time
import traceback
from pathlib import Path

from merge_release import save, snapshot, normalized, fingerprint
from viewport_audit import run_page, percentile


def main():
    build = Path('api-build-v6')
    root = build / 'merge-recheck'
    name = 'noise-control-20260927.json'
    assert not (root / name).exists(), 'Preserve previous control'
    report = {'state': 'waiting_for_dcu_recovery', 'started_at': time.time(),
              'method': 'Identical original queries in both arms; 41 alternating pairs; diagnostic only',
              'benchmarks': []}
    save(root, name, report)
    try:
        deadline = time.time() + 9 * 3600
        while True:
            assert time.time() < deadline, 'Wait expired'
            p = build / 'batch-dcu-recovery-20260927/recovery-progress.json'
            if p.exists() and json.loads(p.read_text())['state'] in ('observed', 'stopped'):
                break
            time.sleep(10)
        with (build / 'admission-work.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            before, services = snapshot(), fingerprint()
            capture = json.loads((root / 'browser-cohorts.json').read_text())
            assert capture['passed']
            end = int(time.time() // 3600) * 3600 - 3600
            report.update(state='testing', end=end)
            for cohort in capture['cohorts']:
                if cohort['version'] != 'before':
                    continue
                project, hours = cohort['project'], cohort['hours']
                doc = next(d for d in before['dashboards'] if d['metadata']['project'] == project and d['metadata']['name'] == 'monitoring-health')
                probe = next(iter(doc['spec']['panels'].values()))['spec']['queries'][0]['spec']['plugin']['spec']['query']
                for nocache in (True, False):
                    pairs = []
                    for iteration in range(41):
                        measured = [None, None]
                        for arm in ((0, 1) if iteration % 2 else (1, 0)):
                            measured[arm] = run_page(cohort['requests'], project, end, hours, nocache, probe)
                            time.sleep(.1)
                        pairs.append(measured)
                    row = {'project': project, 'dashboard': cohort['dashboard'], 'hours': hours, 'nocache': nocache, 'pairs': pairs}
                    for kind in ('page', 'probe'):
                        a, b = [percentile([p[i][kind] for p in pairs]) for i in (0, 1)]
                        row[kind] = {'arm_a_p95': a, 'arm_b_p95': b, 'ratio': b/a}
                    report['benchmarks'].append(row)
                    save(root, name, report)
            report.update(state='complete', ended_at=time.time(), resources_unchanged=normalized(snapshot()) == normalized(before), services_unchanged=fingerprint() == services)
    except BaseException:
        report.update(state='stopped', traceback=traceback.format_exc(), ended_at=time.time())
        save(root, name, report)
        raise
    save(root, name, report)


if __name__ == '__main__':
    main()
