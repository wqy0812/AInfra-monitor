"""Publish the explicitly approved 13 merges, retaining failed timing evidence."""
import copy
import fcntl
import json
import time
from pathlib import Path

from merge_release import api, expressions, fingerprint, normalized, path, prepare, record_failure, save, sha, snapshot
from generator_transaction import plan, install


def main():
    build = Path('api-build-v6')
    prior = build / 'merge-recheck'
    root = build / 'merge-user-release-20260927'
    assert root.is_dir() and not (root / 'merge-journal.json').exists()
    with (build / 'admission-work.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = snapshot()
        after, changes = prepare(before)
        services = fingerprint()
        assert len(changes) == 13 and changes == json.loads((prior / 'merge-changes.json').read_text()), 'Target panels changed since verification'
        original = json.loads((prior / 'merge-candidate.json').read_text())
        proofs = {}
        for name in ('merge-browser.json', 'merge-synthetic.json', 'merge-semantics.json'):
            evidence = json.loads((prior / name).read_text())
            assert evidence['passed'] and evidence['candidate_sha256'] == sha(original)
            proofs[name] = sha(evidence)
        assert services == json.loads((prior / 'viewport-services.json').read_text()), 'Runtime changed'
        count = lambda resources: sum(len(d['spec']['panels']) for d in resources['dashboards'])
        assert count(before) == count(after) == 303
        assert sum(len(expressions(d)) for d in after['dashboards']) == 368
        save(root, 'merge-before.json', before)
        save(root, 'merge-candidate.json', after)
        save(root, 'merge-changes.json', changes)
        save(root, 'merge-services.json', services)
        save(root, 'user-release-decision.json', {'at': time.time(), 'user_instruction': '也上线吧',
             'scope': '13 remaining merge panels; performance gates waived; no stability wait',
             'candidate_sha256': sha(after), 'prior_candidate_sha256': sha(original),
             'identical_target_changes': True, 'correctness_proofs': proofs,
             'performance_evidence': str(prior / 'viewport-performance.json')})
        assert normalized(snapshot()) == normalized(before), 'Concurrent resource edit'
        plan(root, changes)
        journal = []
        save(root, 'merge-journal.json', journal)
        try:
            for candidate in after['dashboards']:
                old = next(d for d in before['dashboards'] if path(d) == path(candidate))
                if old['spec'] == candidate['spec']:
                    continue
                current = api(path(old))
                assert current['spec'] == old['spec'], 'Concurrent dashboard edit'
                new = copy.deepcopy(candidate)
                new['metadata'] = current['metadata']
                journal.append({'before': current, 'after': new})
                save(root, 'merge-journal.json', journal)
                api(path(new), 'PUT', new)
                assert api(path(new))['spec'] == new['spec']
            install(root)
            assert normalized(snapshot()) == normalized(after), 'Readback mismatch'
            assert fingerprint() == services
            save(root, 'merge-publication.json', {'published': True, 'at': time.time(),
                 'panels': 13, 'dashboards_changed': len(journal), 'total_panels': 303,
                 'total_queries': 368, 'resources_match': True, 'services_unchanged': True,
                 'performance': 'waived_by_user', 'observation': 'waived_by_user'})
        except BaseException as error:
            record_failure(root, 'merge-apply-failure.json', error)
            raise
    print('Published 13 merge panels; 303 panels / 368 queries; readback verified', flush=True)


if __name__ == '__main__':
    main()
