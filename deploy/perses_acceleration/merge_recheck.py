"""Read-only validation now, serialized benchmarks and guarded publication later."""
import argparse
import fcntl
import json
import time
from pathlib import Path

import merge_release as release
import viewport_audit


def main(root, build, hours):
    assert root.is_dir() and build.is_dir()
    deadline = time.time() + hours * 3600
    progress = {'started_at': time.time(), 'state': 'waiting_for_evidence', 'published': False}
    with (build / 'admission-work.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            while True:
                assert time.time() < deadline, 'Browser evidence wait expired'
                names = ('merge-browser.json', 'merge-synthetic.json', 'browser-cohorts.json')
                if all((root / name).exists() for name in names): break
                progress['at'] = time.time(); release.save(root, 'recheck-progress.json', progress)
                time.sleep(5)
            before = json.loads((root / 'merge-before.json').read_text())
            candidate = json.loads((root / 'merge-candidate.json').read_text())
            assert release.normalized(release.snapshot()) == release.normalized(before), 'Concurrent resource edit'
            assert release.fingerprint() == json.loads((root / 'merge-services.json').read_text())
            for name in names:
                proof = json.loads((root / name).read_text()); assert proof['passed'], name
                if name != 'browser-cohorts.json': assert proof['candidate_sha256'] == release.sha(candidate), name
            progress.update(state='performance', at=time.time()); release.save(root, 'recheck-progress.json', progress)
            viewport_audit.main(root, root / 'browser-cohorts.json', windows=(1, 12), target_timing=True)
            report = json.loads((root / 'viewport-performance.json').read_text())
            identities = {(b['project'], b['dashboard']) for b in report['benchmarks']}
            admitted = [identity for identity in identities if all(b['passed'] for b in report['benchmarks']
                         if (b['project'], b['dashboard']) == identity)]
            progress.update(admitted=sorted(admitted), state='waiting_for_cpu_observation' if admitted else 'no_admitted_dashboards', at=time.time())
            release.save(root, 'recheck-progress.json', progress)
            if not admitted: return
            while True:
                assert time.time() < deadline, 'CPU observation wait expired'
                cpu = build / 'batch-cpu'
                assert not (cpu / 'batch-rollback.json').exists(), 'CPU batch rolled back; capture a new snapshot'
                if json.loads((cpu / 'batch-observation.json').read_text())['passed']: break
                time.sleep(5)
            release.apply(root, admitted_only=True, viewport=True)
            progress.update(state='published_admitted_dashboards', published=True, ended_at=time.time())
            release.save(root, 'recheck-progress.json', progress)
        except Exception as error:
            progress.update(state='stopped', error=type(error).__name__ + ': ' + str(error), ended_at=time.time())
            release.save(root, 'recheck-progress.json', progress)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--hours', type=float, default=8)
    args = parser.parse_args(); assert 0 < args.hours <= 24
    main(args.evidence, args.build, args.hours)
