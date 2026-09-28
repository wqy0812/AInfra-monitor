"""Finish admitted batches serially, without racing an existing release worker.

Run via SSH MCP. Existing processes and evidence remain authoritative. This
controller never relaxes admission, replaces snapshots, or retries failed batches.
"""
import argparse
import fcntl
import json
import os
import time
from pathlib import Path

import admit_when_ready as admission
import materialized_release as release
from merge_release import fingerprint, save


def active_workers(build, proc=Path('/proc')):
    workers = []
    for item in proc.iterdir():
        if not item.name.isdigit() or int(item.name) == os.getpid(): continue
        try:
            args = [s.decode() for s in (item / 'cmdline').read_bytes().split(b'\0') if s]
        except (OSError, UnicodeError):
            continue
        if not args: continue
        # The existing CPU shell queues admission after its observer exits.
        # Treat the owner as busy even between the two child processes.
        if Path(args[0]).name in ('sh', 'bash', 'dash'):
            command = args[-1]
            if 'cpu-release.log' in command and 'admit_when_ready.py' in command and build.name in command:
                workers.append({'pid': int(item.name), 'script': 'cpu-release-owner'})
            continue
        if not Path(args[0]).name.startswith('python'): continue
        script = next((Path(a).name for a in args[1:] if a.endswith('.py')), '')
        if script not in ('admit_when_ready.py', 'materialized_release.py'): continue
        if any(str(build) in a or build.name in Path(a).parts for a in args[1:]):
            workers.append({'pid': int(item.name), 'script': script})
    return workers


def next_action(build):
    """Return only one serial action; a failure is terminal evidence, not a pass."""
    states = {}
    for group in release.GROUPS:
        root = build / ('batch-' + group)
        terminal = release.batch_terminal(root)
        states[group] = terminal or 'pending'
        if terminal: continue
        if (root / 'batch-publication.json').exists():
            return 'incomplete_observation', group, states
        if (root / 'batch-journal.json').exists():
            return 'incomplete_publication', group, states
        if (root / 'batch-before.json').exists():
            accepted = root / 'batch-admission.json'
            if accepted.exists() and json.loads(accepted.read_text())['passed']:
                return 'publish', group, states
            return 'interrupted_admission', group, states
        return 'admit', group, states
    return 'finished', None, states


def main(build, browser, hours):
    assert build.is_dir() and browser.is_file() and 0 < hours <= 24
    with (build / 'continuation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.time(); deadline = started + hours * 3600
        expected = json.loads((build / 'shadow-started.json').read_text())
        report = {'started_at': started, 'scope': list(release.GROUPS), 'events': []}
        try:
            while time.time() < deadline:
                service = next(s for s in fingerprint() if s['name'] == '/monitoring-api')
                assert service['id'] == expected['container_id'] and service['image'] == expected['image'], 'API changed; stop continuation'
                workers = active_workers(build)
                if workers:
                    report.update(state='waiting_for_existing_worker', at=time.time(), workers=workers)
                    save(build, 'continuation-progress.json', report)
                    time.sleep(10)
                    continue
                action, group, states = next_action(build)
                report.update(state=action, group=group, groups=states, at=time.time(), workers=[])
                save(build, 'continuation-progress.json', report)
                if action == 'finished':
                    report.update(state='observed' if all(v == 'observed' for v in states.values()) else 'needs_followup',
                                  passed=all(v == 'observed' for v in states.values()), ended_at=time.time())
                    save(build, 'continuation-progress.json', report)
                    return
                assert action not in ('incomplete_publication', 'incomplete_observation', 'interrupted_admission'), 'Existing incomplete batch requires forward repair: ' + str(group)
                if action == 'admit':
                    admission.main(build, build, browser, min(8, max(.01, (deadline-time.time())/3600)))
                    continue
                root = build / ('batch-' + group)
                release.require_serial_preparation(root)
                release.health()
                release.apply(root, group)
                report['events'].append({'group': group, 'action': 'published', 'at': time.time()})
                report.update(state='observing', at=time.time())
                save(build, 'continuation-progress.json', report)
                release.observe(root, group)
                report['events'].append({'group': group, 'action': 'observed', 'at': time.time()})
                save(build, 'continuation-progress.json', report)
            raise TimeoutError('Continuation time budget expired')
        except Exception as error:
            report.update(state='stopped', error=type(error).__name__ + ': ' + str(error), ended_at=time.time())
            save(build, 'continuation-progress.json', report)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--hours', type=float, default=12)
    args = parser.parse_args()
    main(args.build, args.browser, args.hours)
