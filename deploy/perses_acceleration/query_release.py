"""Frozen, four-batch query rewrite audit and publication. Run via SSH MCP."""
import argparse
import concurrent.futures
import copy
import hashlib
import json
import math
import statistics
import threading
import time
import uuid
from pathlib import Path

from merge_release import (api, equivalent, fingerprint, normalized, path, query, record_failure,
                           save, sha, snapshot, OPENER)
from release_support import readback
from generator_transaction import plan as generator_plan, install as generator_install
import generator_transaction as transaction
from query_slimming import BATCHES, batch_targets, prepare, plugin

SAMPLES = 41
WINDOWS = ((1, 5), (24, 60))
SEMANTIC_WINDOWS = ((1, 5), (1, 15), (1, 60), (24, 60))


def tool_fingerprint():
    root = Path(__file__).resolve().parents[2]
    paths = {'deploy/perses_acceleration/query_release.py', 'deploy/perses_acceleration/merge_release.py',
             'deploy/perses_acceleration/generator_transaction.py', 'perses/release_support.py', 'perses/connection.py'}
    paths.update(transaction.source_paths())
    return {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sorted(paths)}


def require_predecessors(batch):
    state = json.loads((transaction.RUNTIME / 'acceleration_state.json').read_text())
    admitted = {tuple(e[:3]) for e in state.get('rewrites', [])}
    required = set().union(*(batch_targets(b) for b in BATCHES[:BATCHES.index(batch)]))
    assert required <= admitted, 'Publish prior batches before preparing this batch'


def load(root, name):
    return json.loads((root / name).read_text())


def runtime_snapshot(changes):
    names = set(transaction.MODULES) | set(transaction.DOCUMENTS) | {'acceleration_state.json', 'METRICS_GUIDE.md', 'README.md'}
    names.update('projects/{project}/dashboards/{dashboard}.json'.format(**c) for c in changes)
    return {n: transaction.encoded(transaction.RUNTIME / n) for n in sorted(names)}


def rows_for(panel, change, end, hours, step, *, proxy=False):
    rows = []
    for q in panel['spec']['queries']:
        spec = plugin(q)
        part = query(spec['query'], end, hours, step, True, change['project'] if proxy else None)
        if change['kind'] == 'generation-results':
            # Compare curve identity explicitly; no union that could hide duplicate curves.
            for row in part:
                labels = row['metric']
                label = labels.pop('perses_series', spec['seriesNameFormat'])
                labels.pop('perses_order', None)
                labels['perses_series'] = label
        rows.extend(part)
    keys = [json.dumps(r['metric'], sort_keys=True) for r in rows]
    assert len(keys) == len(set(keys)), 'Duplicate output curve'
    return rows


def check_change(change, end, hours, step):
    old = rows_for(change['before'], change, end, hours, step)
    new = rows_for(change['after'], change, end, hours, step)
    proxy = rows_for(change['after'], change, end, hours, step, proxy=True)
    assert equivalent(old, new) and equivalent(new, proxy), ('Query semantics differ', change['project'], change['panel'], step)
    return {'project': change['project'], 'dashboard': change['dashboard'], 'panel': change['panel'], 'hours': hours, 'step': step,
            'series': len(new), 'passed': True}


def cohort(expressions, probe, project, end, hours, step, nocache):
    gate = threading.Barrier(2)
    def run_target():
        gate.wait()
        start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda e: query(e, end, hours, step, nocache, project), expressions))
        return time.monotonic() - start
    def run_probe():
        gate.wait()
        start = time.monotonic()
        query(probe, end, hours, step, nocache, project)
        return time.monotonic() - start
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        target, external = pool.submit(run_target), pool.submit(run_probe)
        return {'target': target.result(), 'probe': external.result()}


def evaluate(pairs, *, target):
    assert len(pairs) == SAMPLES and all(len(p) == 2 and all(
        set(v) == {'target', 'probe'} and all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in v.values())
        for v in p) for p in pairs), 'Need 41 complete finite timing pairs'
    percentile = lambda xs: sorted(xs)[math.ceil(.95 * len(xs)) - 1]
    medians = [statistics.median(p[i]['target'] for p in pairs) for i in (0, 1)]
    page = [percentile([p[i]['target'] for p in pairs]) for i in (0, 1)]
    probes = [percentile([p[i]['probe'] for p in pairs]) for i in (0, 1)]
    return {'before_median': medians[0], 'after_median': medians[1],
            'before_p95': page[0], 'after_p95': page[1],
            'probe_before_p95': probes[0], 'probe_after_p95': probes[1],
            'passed': (medians[1] < medians[0] if target else page[1] <= page[0] * 1.05)
                      and probes[1] <= probes[0] * 1.05}


def health_record():
    with OPENER.open('http://127.0.0.1:18430/health', timeout=10) as response:
        health = json.load(response)
    lags = {key: time.time() - value['processed_at'] for key, value in health['environments'].items()}
    accelerator = health['perses_acceleration']
    passed = (health['status'] == 'ok' and set(lags) == {'a3-vllm', 'dcu-pd', 'xpu-pd'}
              and all(0 <= lag < 20 for lag in lags.values())
              and all(value['error'] is None for value in health['environments'].values())
              and not accelerator['state_error'] and not accelerator['disabled_groups']
              and all(not job['error'] for job in accelerator['jobs']))
    return {'at': time.time(), 'model_lags': lags, 'passed': passed}


def audit(root, batch, resume=False):
    attempt = uuid.uuid4().hex
    if resume:
        before, after, changes = [load(root, 'query-' + n + '.json') for n in ('before', 'candidate', 'changes')]
        meta = load(root, 'query-meta.json')
        assert meta['batch'] == batch
        assert prepare(before, batch) == (after, changes), 'Candidate code changed; use fresh evidence'
        assert meta.get('tool_sha256') == tool_fingerprint(), 'Audit implementation changed; use fresh evidence'
        assert normalized(snapshot()) == normalized(before), 'Concurrent resource edit'
        assert fingerprint() == meta['services'], 'Measurement version changed'
        assert runtime_snapshot(changes) == load(root, 'query-runtime-before.json'), 'Concurrent runtime edit'
    else:
        assert not (root / 'query-meta.json').exists(), 'Use fresh evidence or resume'
        require_predecessors(batch)
        before = snapshot()
        after, changes = prepare(before, batch)
        assert changes, 'Batch already applied'
        meta = {'batch': batch, 'candidate_sha256': sha(after), 'services': fingerprint(),
                'end': int(time.time() // 3600) * 3600 - 3600, 'samples': SAMPLES, 'tool_sha256': tool_fingerprint()}
        for n, v in [('before', before), ('candidate', after), ('changes', changes), ('meta', meta),
                     ('runtime-before', runtime_snapshot(changes))]:
            save(root, 'query-' + n + '.json', v)
    report_file = root / 'query-performance.json'
    if resume and report_file.exists():
        report = load(root, 'query-performance.json')
        assert report['candidate_sha256'] == sha(after) and report['end'] == meta['end']
    else:
        report = {'passed': False, 'candidate_sha256': sha(after), 'end': meta['end'], 'batch': batch,
                  'samples': SAMPLES, 'semantics': [], 'benchmarks': [], 'health': [health_record()],
                  'method': 'authenticated Perses proxy, concurrency 3, independent concurrent probe; data completion, not browser paint'}
    end = meta['end']
    if not report['semantics']:
        report['semantics'] = [check_change(c, end, hours, step) for c in changes
                               for hours, step in SEMANTIC_WINDOWS]
        save(root, report_file.name, report)
    documents = lambda r, p, d: next(x for x in r['dashboards'] if (x['metadata']['project'], x['metadata']['name']) == (p, d))
    subjects = [(c['project'], c['dashboard'], c['panel'], [c['before'], c['after']]) for c in changes]
    for p, d in sorted({(c['project'], c['dashboard']) for c in changes}):
        subjects.append((p, d, '__page__', [documents(r, p, d) for r in (before, after)]))
    api('/api/v1/projects')
    for project, dashboard, panel, versions in subjects:
        health = documents(before, project, 'monitoring-health')
        probe = plugin(next(iter(health['spec']['panels'].values()))['spec']['queries'][0])['query']
        if panel == '__page__':
            groups = [[plugin(q)['query'] for p in d['spec']['panels'].values() for q in p['spec']['queries']] for d in versions]
        else:
            groups = [[plugin(q)['query'] for q in p['spec']['queries']] for p in versions]
        for hours, step in WINDOWS:
            for nocache in (True, False):
                key = (project, dashboard, panel, hours, step, nocache)
                if any(tuple(r[k] for k in ('project', 'dashboard', 'panel', 'hours', 'step', 'nocache')) == key for r in report['benchmarks']):
                    continue
                pairs = []
                for iteration in range(SAMPLES):
                    values = [None, None]
                    for version in ((0, 1) if iteration % 2 else (1, 0)):
                        values[version] = cohort(groups[version], probe, project, end, hours, step, nocache)
                        # Retain individual measurements even if the process
                        # fails before completing this 41-pair cohort. Resume
                        # reuses complete cohorts only; incomplete attempts stay
                        # in this append-only log and never masquerade as a pass.
                        with (root / 'query-samples.jsonl').open('a') as stream:
                            stream.write(json.dumps({'attempt': attempt, 'cohort': key, 'pair': iteration,
                                                     'version': version, 'measurement': values[version]}) + '\n')
                    pairs.append(values)
                row = dict(zip(('project', 'dashboard', 'panel', 'hours', 'step', 'nocache'), key))
                row.update(pairs=pairs, **evaluate(pairs, target=panel != '__page__'))
                report['benchmarks'].append(row)
                report['health'].append(health_record())
                save(root, report_file.name, report)
                print(project, panel, hours, nocache, json.dumps({k: v for k, v in row.items() if k != 'pairs'}), flush=True)
    report['resources_unchanged'] = normalized(snapshot()) == normalized(before)
    report['services_unchanged'] = fingerprint() == meta['services']
    report['passed'] = all(r['passed'] for r in report['benchmarks'] + report['semantics'] + report['health']) and report['resources_unchanged'] and report['services_unchanged']
    save(root, report_file.name, report)


def validate_admission(root, batch, *, allow_revision=False):
    before, after, changes = [load(root, 'query-' + n + '.json') for n in ('before', 'candidate', 'changes')]
    meta, report = load(root, 'query-meta.json'), load(root, 'query-performance.json')
    assert batch != 'a3-histograms' or allow_revision, 'A3 publication requires the revision replacement acceleration path'
    assert meta['batch'] == report['batch'] == batch and prepare(before, batch) == (after, changes)
    assert meta['candidate_sha256'] == report['candidate_sha256'] == sha(after)
    assert meta.get('tool_sha256') == tool_fingerprint(), 'Audit implementation changed; use fresh evidence'
    require_predecessors(batch)
    assert report['passed'] and report['samples'] == SAMPLES and report['end'] == meta['end']
    subjects = {(c['project'], c['dashboard'], c['panel']) for c in changes}
    subjects |= {(c['project'], c['dashboard'], '__page__') for c in changes}
    expected = {s + (hours, step, nocache) for s in subjects for hours, step in WINDOWS for nocache in (True, False)}
    actual = [tuple(r[k] for k in ('project', 'dashboard', 'panel', 'hours', 'step', 'nocache')) for r in report['benchmarks']]
    assert set(actual) == expected and len(actual) == len(expected), 'Incomplete measurement matrix'
    assert len(report['health']) == len(expected) + 1 and all(r['passed'] for r in report['health']), 'Missing or failed health evidence'
    assert all(evaluate(r['pairs'], target=r['panel'] != '__page__')['passed'] for r in report['benchmarks'])
    semantic_keys = [(r['project'], r['dashboard'], r['panel'], r['hours'], r['step']) for r in report['semantics']]
    semantic_expected = {(c['project'], c['dashboard'], c['panel'], hours, step)
                         for c in changes for hours, step in SEMANTIC_WINDOWS}
    assert set(semantic_keys) == semantic_expected and len(semantic_keys) == len(semantic_expected) and all(r['passed'] for r in report['semantics'])
    for filename in ('query-browser.json', 'query-synthetic.json'):
        proof = load(root, filename)
        assert proof['passed'] and proof['candidate_sha256'] == sha(after), 'Missing candidate-bound proof: ' + filename
        validate_proof(proof, changes, browser=filename == 'query-browser.json')
    assert normalized(snapshot()) == normalized(before), 'Concurrent resource edit'
    assert fingerprint() == meta['services'], 'Measurement version changed'
    assert runtime_snapshot(changes) == load(root, 'query-runtime-before.json'), 'Concurrent runtime edit'
    assert health_record()['passed'], 'Current service health failed'
    return before, after, changes


def validate_proof(proof, changes, *, browser):
    cases = ('normal', 'zero', 'absent-result', 'gap-result')
    if browser:
        expected = {(c['project'], c['dashboard'], c['panel'], case) for c in changes
                    for case in (cases if c['kind'] == 'generation-results' else cases[:2])}
        actual = []
        assert proof['viewport'] == {'width': 1920, 'height': 1080} and not proof['errors']
        assert proof['requests'] and all(r['status'] == 200 for r in proof['requests'])
        for check in proof['checks']:
            assert check['passed'] and check['before'] == check['after'], 'Browser rendering differs'
            actual.extend((check['project'], check['dashboard'], key, check['testCase'])
                          for key in check['before'] if not key.startswith('__'))
    else:
        expected = {(c['project'], c['dashboard'], c['panel'], case, step)
                    for c in changes for case in cases for step in (5, 15, 60)}
        actual = [(c['project'], c['dashboard'], c['panel'], c['case'], c['step']) for c in proof['checks']]
        assert all(c['passed'] for c in proof['checks'])
    assert set(actual) == expected and len(actual) == len(expected), 'Incomplete or duplicate proof matrix'


def apply(root, batch):
    before, after, changes = validate_admission(root, batch)
    assert not (root / 'query-journal.json').exists(), 'Reconcile prior publication; never replay writes'
    entries = [[c['project'], c['dashboard'], c['panel'], c['kind']] for c in changes]
    generator_plan(root, changes, rewrites=entries)
    journal = []
    save(root, 'query-journal.json', journal)
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
            save(root, 'query-journal.json', journal)
            api(path(new), 'PUT', new)
            readback(api, path(new), new)
        generator_install(root)
        assert normalized(snapshot()) == normalized(after)
        save(root, 'query-publication.json', {'passed': True, 'batch': batch, 'at': time.time(), 'candidate_sha256': sha(after)})
    except BaseException as error:
        record_failure(root, 'query-apply-failure.json', error)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('audit', 'resume', 'apply'))
    parser.add_argument('--batch', choices=BATCHES, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    assert args.evidence.is_dir()
    try:
        if args.action == 'apply':
            apply(args.evidence, args.batch)
        else:
            audit(args.evidence, args.batch, resume=args.action == 'resume')
    except BaseException as error:
        record_failure(args.evidence, 'query-' + args.action + '-failure.json', error)
        raise


if __name__ == '__main__':
    main()
