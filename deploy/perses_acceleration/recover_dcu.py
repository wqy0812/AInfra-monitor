"""One bounded fresh DCU admission after the A3 observation completes."""
import fcntl
import json
import shutil
import time
import traceback
from pathlib import Path

import materialized_release as release
from merge_release import fingerprint, save


def main():
    build = Path('api-build-v6')
    root = build / 'batch-dcu-recovery-20260927'
    assert root.is_dir()
    expected = json.loads((build / 'shadow-started.json').read_text())
    deadline = time.time() + 8 * 3600
    report = {'started_at': time.time(), 'passed': False}
    original_health = release.health

    def checked_health():
        try:
            return original_health()
        except Exception:
            with release.urllib.request.urlopen('http://127.0.0.1:18430/health', timeout=10) as response:
                evidence = json.load(response)
            save(root, 'health-failure-' + str(time.time_ns()) + '.json',
                 {'at': time.time(), 'traceback': traceback.format_exc(), 'health': evidence})
            raise

    release.health = checked_health
    try:
        while True:
            assert time.time() < deadline, 'A3 observation wait expired'
            state = release.batch_terminal(build / 'batch-a3')
            if state:
                assert state == 'observed', 'A3 did not complete observation: ' + state
                break
            report.update(state='waiting_for_a3_observation', at=time.time())
            save(root, 'recovery-progress.json', report)
            time.sleep(10)
        with (build / 'admission-work.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            service = next(s for s in fingerprint() if s['name'] == '/monitoring-api')
            assert service['id'] == expected['container_id'] and service['image'] == expected['image'], 'API changed'
            release.prepare(root, 'dcu')
            shutil.copyfile(build / 'materialized-browser.json', root / 'materialized-browser.json')
            for name in ('audit', 'impact', 'apply', 'observe'):
                report.update(state=name, at=time.time())
                save(root, 'recovery-progress.json', report)
                getattr(release, name)(root, 'dcu')
                if name in ('audit', 'impact'):
                    filename = 'batch-performance.json' if name == 'audit' else 'non-target-performance.json'
                    assert json.loads((root / filename).read_text())['passed'], name + ' did not pass'
                if name == 'impact':
                    save(root, 'batch-admission.json', {'group': 'dcu', 'passed': True, 'at': time.time()})
            report.update(state='observed', passed=True, ended_at=time.time())
    except BaseException:
        report.update(state='stopped', ended_at=time.time(), traceback=traceback.format_exc())
        save(root, 'recovery-progress.json', report)
        if not (root / 'batch-publication.json').exists():
            save(root, 'batch-admission.json', {'group': 'dcu', 'passed': False, **report})
        raise
    save(root, 'recovery-progress.json', report)


if __name__ == '__main__':
    main()
