"""Wait for verified coverage, then gather real-environment batch evidence.

No datasource bindings are published. Every batch still needs an explicit
publisher invocation against its fresh snapshot after all gates pass. Stop at
the first publishable batch; resume after its observation finishes.
"""
import argparse
import fcntl
import json
import shutil
import time
from pathlib import Path
from api_readiness import publication, accepted

import materialized_release as release
from merge_release import fingerprint, save


def _main(build, batches, browser, hours, observation=None, resume_prepared=None):
    catalog = json.loads(release.CATALOG.read_text())
    expected = publication(build)
    observed_build = observation or build
    observed_release = publication(observed_build)
    assert observed_release['image'] == expected['image'], 'Observation belongs to another API image'
    deadline = time.time() + hours * 3600
    summary = {'started_at': time.time(), 'published_panels': 0, 'groups': {}}
    for group in ('cpu', 'dcu', 'a3'):
        root = batches / ('batch-' + group)
        assert root.is_dir(), 'Use disclosed evidence directories'
        terminal = release.batch_terminal(root)
        if terminal:
            summary['groups'][group] = {'state': terminal, 'at': time.time()}
            save(build, 'admission-progress.json', summary)
            continue
        resuming = resume_prepared == group
        if resuming:
            assert (root / 'batch-performance.json').exists() and not (root / 'batch-publication.json').exists(), 'Only interrupted unpublished audits can resume'
            release.require_serial_preparation(root)
        if (root / 'batch-before.json').exists() and not resuming:
            summary['groups'][group] = {'state': 'waiting_for_publication_or_recovery', 'at': time.time()}
            summary['next'] = 'Repair and finish this prepared batch before resuming later batches'
            save(build, 'admission-progress.json', summary)
            return
        summary['groups'][group] = {'state': 'waiting_for_verified_coverage'}
        save(build, 'admission-progress.json', summary)
        passed = False
        try:
            while True:
                assert time.time() < deadline, 'Bounded admission wait expired'
                service = next(s for s in fingerprint() if s['name'] == '/monitoring-api')
                assert service['id'] == expected['container_id'] and service['image'] == expected['image'], 'API changed; stop stale admission'
                accelerator = release.health()['perses_acceleration']
                assert group not in accelerator['disabled_groups'] and not accelerator['state_error'], 'Shadow group stopped'
                panel_ids = {p['id'] for p in catalog['panels'] if p['group'] == group}
                selected = [j for j in accelerator['jobs'] if j['job'].rsplit(':', 2)[0] in panel_ids]
                assert all(not j['error'] and not (j.get('backfill') or {}).get('error') for j in selected), 'Materialization failure'
                try:
                    release.readiness(catalog, group)
                    assert accepted(observed_build), 'Startup acceptance still pending'
                    break
                except AssertionError:
                    time.sleep(30)
            summary['groups'][group] = {'state': 'testing', 'started_at': time.time()}
            save(build, 'admission-progress.json', summary)
            if not resuming: release.prepare(root, group)
            shutil.copyfile(browser, root / 'materialized-browser.json')
            if resuming: release.audit(root, group, resume=True)
            else: release.audit(root, group)
            performance = json.loads((root / 'batch-performance.json').read_text())
            if performance['passed']:
                release.impact(root, group)
                impact = json.loads((root / 'non-target-performance.json').read_text())
                passed = impact['passed']
            else:
                passed = False
            summary['groups'][group] = {'state': 'ready_for_publication' if passed else 'admission_failed', 'at': time.time()}
        except (OSError, ValueError, KeyError, AssertionError, RuntimeError) as error:
            summary['groups'][group] = {'state': 'stopped', 'at': time.time(), 'error': type(error).__name__ + ': ' + str(error)[:300]}
        save(root, 'batch-admission.json', {'group': group, 'passed': passed, **summary['groups'][group]})
        save(build, 'admission-progress.json', summary)
        print(group, summary['groups'][group], flush=True)
        if passed:
            summary['next'] = 'Apply and observe this batch before resuming admission for the next group'
            save(build, 'admission-progress.json', summary)
            return
    summary['ended_at'] = time.time()
    save(build, 'admission-progress.json', summary)


def main(build, batches, browser, hours, observation=None, resume_prepared=None):
    # Keep independent production benchmarks from contaminating each other.
    # Passive stability observation does not acquire this lock.
    with (build / 'admission-work.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _main(build, batches, browser, hours, observation, resume_prepared)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--batches', type=Path, required=True)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--hours', type=float, default=8)
    parser.add_argument('--observation', type=Path, help='Prior successful observation of the exact same API image')
    parser.add_argument('--resume-prepared', choices=release.GROUPS, help='Resume one interrupted unpublished audit without preparing a new snapshot')
    args = parser.parse_args()
    assert 0 < args.hours <= 24 and args.build.is_dir() and args.batches.is_dir()
    main(args.build, args.batches, args.browser, args.hours, args.observation, args.resume_prepared)
