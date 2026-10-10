"""Environment admission after verified 12h coverage, with 24h history checks.

Run only through SSH MCP. Each group uses a fresh, explicitly created evidence
directory. No historical backfill is performed by this script.
"""
import argparse
import copy
import concurrent.futures
import fcntl
import hashlib
import itertools
import json
import math
import statistics
import threading
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from merge_release import api, equivalent, fingerprint, normalized, path, record_failure, save, sha, snapshot, query
from generator_transaction import plan as generator_plan, install as generator_install
from release_support import readback
from acceleration_publication import published, NAME
from dashboard_columns import panel_index, section_panels

ROOT = Path(__file__).resolve().parents[2]
STATE = Path('/data2/monitoring/state')
CATALOG = ROOT / 'monitoring/perses_acceleration_catalog.json'
GROUPS = ('cpu', 'dcu', 'a3')
PERFORMANCE_POLICY = 'faster-median-v1'
WINDOW_SECONDS = 43200
CORRECTNESS_BATCH_POINTS = 600
OBSERVATION_TIMEOUT_SECONDS = 90


def evaluate_benchmark(benchmark):
    """User policy: strictly faster median, retaining all 41 measured pairs."""
    pairs = benchmark['pairs']
    assert len(pairs) == 41 and all(len(p) == 2 and all(
        isinstance(v, (float, int)) and math.isfinite(v) and v > 0 for v in p) for p in pairs), 'Need 41 finite positive timing pairs'
    old, new = [statistics.median(p[i] for p in pairs) for i in (0, 1)]
    return {**benchmark, 'before_median': old, 'after_median': new,
            'passed': new < old, 'performance_policy': PERFORMANCE_POLICY}


def check_key(check):
    return (check['panel'], check['step'], tuple(check['filters']), check['start'], check['end'])


def benchmark_key(benchmark):
    return (benchmark['panel'], benchmark['step'], benchmark['nocache'])


def read_health():
    with urllib.request.urlopen('http://127.0.0.1:18430/health', timeout=10) as response:
        return json.load(response)


def validate_health(result):
    assert result['status'] == 'ok'
    assert all(v['error'] is None and 0 <= time.time() - v['processed_at'] < 20 for v in result['environments'].values())


def health():
    result = read_health()
    validate_health(result)
    return result


def source_path(source):
    return '/api/v1/projects/' + source['metadata']['project'] + '/datasources/' + source['metadata']['name']


def expression(panel, step, values=()):
    result = panel['expression'].replace('$__interval', str(step) + 's')
    bindings = dict(zip(sorted(panel['variables']), values))
    for key in panel['variables']: result = result.replace('$' + key, bindings.get(key, '.*'))
    return result


def proxy_query(project, source, expr, start, end, step, nocache=True):
    return query(expr, end, 0, step, nocache, project, source, start=start)


def coverage(panel, step, start, end):
    selector = 'monitoring_perses_complete{' + ','.join(k + '=' + json.dumps(v) for k, v in {
        'query_id': panel['id'], 'revision': panel['revision'], 'step': str(step)}.items()) + '}'
    rows = proxy_query(panel['project'], 'victoriametrics', selector + ' and (timestamp(' + selector + ') == time())', start, end, step)
    counts = {}
    for row in rows:
        for tick, value in row['values']:
            assert tick not in counts and float(value) >= 0 and float(value).is_integer(), 'Ambiguous completion marker'
            counts[tick] = int(float(value))
    assert sorted(counts) == list(range(start, end + 1, step)), 'Coverage gap'
    return counts


def compare_correctness_window(panel, expr, start, end, step):
    """Only correctness may split an OOM window; performance always stays whole."""
    source = 'victoriametrics'
    try:
        old = proxy_query(panel['project'], source, expr, start, end, step)
        source = NAME
        new = proxy_query(panel['project'], source, expr, start, end, step)
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors='replace')
        if error.code != 422 or 'not enough memory for processing' not in body:
            raise RuntimeError('Query HTTP ' + str(error.code) + ': ' + body[:1500]) from error
        reason = {'source': source, 'status': error.code, 'body': body[:12000]}
    else:
        assert equivalent(old, new), ('Mismatch', panel['id'], step, start, end)
        counts = {}
        for row in new:
            for tick, _ in row['values']: counts[tick] = counts.get(tick, 0) + 1
        return counts, {'mode': 'whole_window', 'chunks': 1}
    # Resolve the exact native grid before splitting, preserving fractional
    # windows and $__interval. Each frozen expression is evaluated on every
    # original timestamp; no resampling, averaging, interpolation or rank merge.
    assert '@' not in expr, 'Cannot split an expression with range-bound @ modifiers'
    grid = proxy_query(panel['project'], 'victoriametrics', 'vector(time())', start, end, step)
    assert len(grid) == 1
    ticks = [t for t, _ in grid[0]['values']]
    assert len(ticks) > CORRECTNESS_BATCH_POINTS, 'A small window also exceeded memory limits'
    counts = {}; chunks = 0
    for offset in range(0, len(ticks), CORRECTNESS_BATCH_POINTS):
        part = ticks[offset:offset + CORRECTNESS_BATCH_POINTS]; allowed = set(part)
        old = proxy_query(panel['project'], 'victoriametrics', expr, part[0], part[-1], step)
        new = proxy_query(panel['project'], NAME, expr, part[0], part[-1], step)
        assert equivalent(old, new), ('Pointwise mismatch', panel['id'], step, part[0], part[-1])
        for row in new:
            for tick, _ in row['values']:
                assert tick in allowed, 'Split query changed the original grid'
                counts[tick] = counts.get(tick, 0) + 1
        chunks += 1
    return counts, {'mode': 'pointwise_after_memory_limit', 'chunks': chunks,
                    'grid_points': len(ticks), 'original_request_error': reason}


def checked_jobs(catalog, group, accelerator):
    assert group not in accelerator['disabled_groups'] and not accelerator['state_error']
    jobs = {j['job']: j for j in accelerator['jobs']}
    selected = []
    for panel in catalog['panels']:
        if panel['group'] != group: continue
        for step in catalog['steps']:
            key = panel['id'] + ':' + panel['revision'] + ':' + str(step)
            job = jobs[key]
            assert not job['error'], 'Materialization failure: ' + key
            assert not (job.get('backfill') or {}).get('error'), 'Backfill failure: ' + key
            selected.append((key, step, job))
    return selected


def readiness(catalog, group, *, accelerator=None):
    if accelerator is None:
        accelerator = health()['perses_acceleration']
    ranges = {}
    for key, step, job in checked_jobs(catalog, group, accelerator):
        assert job['processed_at'] - job.get('coverage_start', job['started_at']) >= WINDOW_SECONDS, 'Need 12h verified coverage: ' + key
        assert job['lag_seconds'] <= max(300, step), 'Worker behind: ' + key
        end = int(job['processed_at'] // step) * step
        ranges[key] = [end - WINDOW_SECONDS, end]
    return ranges


def batch_terminal(root):
    """Incomplete publication blocks later batches, even after failed admission."""
    if (root / 'batch-rollback.json').exists():
        return 'rolled_back'
    publication, observation = root / 'batch-publication.json', root / 'batch-observation.json'
    if publication.exists():
        return 'observed' if observation.exists() and json.loads(observation.read_text())['passed'] else None
    if (root / 'batch-journal.json').exists():
        return None
    admission = root / 'batch-admission.json'
    if admission.exists() and json.loads(admission.read_text())['passed'] is False:
        return 'admission_failed'
    return None


def require_serial_preparation(root):
    # A later source creation or panel publication changes the full snapshot.
    # Keep that protection and prevent overlapping prepared transactions instead.
    for sibling in root.parent.iterdir():
        if sibling != root and sibling.is_dir() and (sibling / 'batch-before.json').exists():
            assert batch_terminal(sibling), 'Finish prepared batch before preparing another: ' + str(sibling)


def prepare(root, group, previous_catalog=None):
    with (root.parent / 'batch-preparation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if previous_catalog is None:
            _prepare(root, group)
        else:
            _prepare(root, group, previous_catalog)


def _prepare(root, group, previous_catalog=None):
    assert not (root / 'batch-before.json').exists(), 'Use fresh evidence for every batch'
    require_serial_preparation(root)
    catalog = json.loads(CATALOG.read_text())
    before = snapshot()
    original_panels = panel_index(before)
    after = published(before, {'schema': 1, 'merges': [], 'groups': [group]})
    replacing = previous_catalog is not None
    if replacing:
        from query_slimming import validate_revision_catalog, prepare as rewrite_prepare
        from query_release import runtime_snapshot
        previous = json.loads(previous_catalog.read_text())
        validate_revision_catalog(previous, catalog, group)
        old_entries = {p['id']: p for p in previous['panels']}
        for entry in catalog['panels']:
            if entry['group'] != group:
                continue
            document, key = original_panels[(entry['project'], entry['dashboard'], entry['panel'])]
            spec = document['spec']['panels'][key]['spec']['queries'][0]['spec']['plugin']['spec']
            assert spec.get('datasource', {}).get('name') == NAME, 'Replacement requires an already accelerated panel'
            assert spec['query'] == old_entries[entry['id']]['expression'], 'Previous catalog differs from live panel'
        after, _ = rewrite_prepare(after, 'a3-histograms')
    for source in after['datasources']:
        if source['metadata']['name'] == NAME:
            assert not source['spec']['default']
            assert source['spec']['plugin']['spec']['proxy']['spec']['url'] == 'http://127.0.0.1:18430/internal/perses', 'Unexpected accelerated source URL'
    changes = []
    candidate_panels = panel_index(after)
    for panel in catalog['panels']:
        if panel['group'] != group: continue
        identity = (panel['project'], panel['dashboard'], panel['panel'])
        document, key = original_panels[identity]
        candidate, candidate_key = candidate_panels[identity]
        assert (document['metadata']['name'], key) == (candidate['metadata']['name'], candidate_key)
        old, new = document['spec']['panels'][key], candidate['spec']['panels'][candidate_key]
        assert old != new, 'Already switched'
        expected = old_entries[panel['id']]['expression'] if replacing else panel['expression']
        assert old['spec']['queries'][0]['spec']['plugin']['spec']['query'] == expected, 'Target expression edited'
        assert new['spec']['queries'][0]['spec']['plugin']['spec']['query'] == panel['expression'], 'Candidate differs from new catalog'
        changes.append({'project': panel['project'], 'dashboard': document['metadata']['name'], 'panel': key, 'before': old, 'after': new})
    save(root, 'batch-before.json', before); save(root, 'batch-candidate.json', after)
    save(root, 'batch-changes.json', changes); save(root, 'batch-services.json', fingerprint())
    meta = {'group': group, 'catalog_sha256': hashlib.sha256(CATALOG.read_bytes()).hexdigest(), 'candidate_sha256': sha(after)}
    if replacing:
        meta.update(replace_revision=True, previous_catalog_sha256=hashlib.sha256(previous_catalog.read_bytes()).hexdigest())
        # Save the exact old bytes separately; both digests are checked at apply.
        (root / 'batch-previous-catalog.json').write_bytes(previous_catalog.read_bytes())
        runtime_before = runtime_snapshot(changes)
        frozen = root / 'query-runtime-before.json'
        if frozen.exists():
            assert json.loads(frozen.read_text()) == runtime_before, 'Runtime changed after raw-query audit'
        else:
            save(root, frozen.name, runtime_before)
    save(root, 'batch-meta.json', meta)
    # Add only non-default sources, enabling authenticated proxy acceptance while
    # every panel continues using its original datasource.
    created = []
    save(root, 'datasource-journal.json', created)
    for source in after['datasources']:
        if any(source_path(d) == source_path(source) for d in before['datasources']): continue
        created.append(source); save(root, 'datasource-journal.json', created)
        api(source_path(source).rsplit('/', 1)[0], 'POST', source)
        assert api(source_path(source))['spec'] == source['spec']
    expected = copy.deepcopy(before); expected['datasources'] = after['datasources']
    assert normalized(snapshot()) == normalized(expected), 'Non-target edit during source preparation'
    save(root, 'batch-prepared.json', expected)
    print('Prepared non-default sources; target panels still use original paths', flush=True)


def audit(root, group, resume=False):
    catalog = json.loads(CATALOG.read_text()); meta = json.loads((root / 'batch-meta.json').read_text())
    assert meta['group'] == group and meta['catalog_sha256'] == hashlib.sha256(CATALOG.read_bytes()).hexdigest()
    assert normalized(snapshot()) == normalized(json.loads((root / 'batch-prepared.json').read_text()))
    assert fingerprint() == json.loads((root / 'batch-services.json').read_text())
    ranges = readiness(catalog, group)
    report = {'passed': False, 'group': group, 'samples': 41, 'window_seconds': WINDOW_SECONDS, 'catalog_sha256': meta['catalog_sha256'],
              'candidate_sha256': meta['candidate_sha256'], 'performance_policy': PERFORMANCE_POLICY,
              'ranges': ranges, 'checks': [], 'benchmarks': []}
    if resume:
        prior = json.loads((root / 'batch-performance.json').read_text())
        for key in ('group', 'samples', 'window_seconds', 'catalog_sha256', 'candidate_sha256'):
            assert prior[key] == report[key], 'Incompatible resume evidence: ' + key
        assert prior['ranges'].keys() == ranges.keys(), 'Different resume jobs'
        for key, (start, end) in prior['ranges'].items():
            step = int(key.rsplit(':', 1)[1])
            assert end - start == WINDOW_SECONDS and start % step == end % step == 0 and end <= ranges[key][1], 'Invalid frozen resume window'
        ranges = prior['ranges']; report['ranges'] = ranges
        report['checks'] = prior['checks']
        assert all(c['passed'] is True for c in report['checks']), 'Failed correctness evidence cannot be resumed'
        report['benchmarks'] = [evaluate_benchmark(b) for b in prior['benchmarks']]
        report['resumed_at'] = time.time()
        report['reused_checks'] = len(report['checks'])
        report['reused_benchmarks'] = len(report['benchmarks'])
    done_checks = {check_key(c) for c in report['checks']}
    done_benchmarks = {benchmark_key(b) for b in report['benchmarks']}
    assert len(done_checks) == len(report['checks']) and len(done_benchmarks) == len(report['benchmarks']), 'Duplicate resume evidence'
    expected_checks = set(); expected_benchmarks = set()
    for panel in catalog['panels']:
        if panel['group'] != group: continue
        for step in catalog['steps']:
            start, end = ranges[panel['id'] + ':' + panel['revision'] + ':' + str(step)]
            for values in itertools.product(*(panel['variables'][key] for key in sorted(panel['variables']))):
                for left, right in ((start, end), (end - 86400, end), (start + .123, end + .123)):
                    expected_checks.add((panel['id'], step, values, left, right))
            if step in (60, 120):
                expected_benchmarks.update((panel['id'], step, mode) for mode in (True, False))
    assert done_checks <= expected_checks and done_benchmarks <= expected_benchmarks, 'Unexpected resume evidence'
    save(root, 'batch-performance.json', report)
    for panel in catalog['panels']:
        if panel['group'] != group: continue
        for step in catalog['steps']:
            start, end = ranges[panel['id'] + ':' + panel['revision'] + ':' + str(step)]
            counts = coverage(panel, step, start, end)
            for values in itertools.product(*(panel['variables'][key] for key in sorted(panel['variables']))):
                expr = expression(panel, step, values)
                # Full coverage, old history + new, and nonintegral boundaries.
                for left, right in ((start, end), (end - 86400, end), (start + .123, end + .123)):
                    key = (panel['id'], step, values, left, right)
                    if key in done_checks: continue
                    sample_counts, comparison = compare_correctness_window(panel, expr, left, right, step)
                    if left == start and all(value == '.*' for value in values):
                        actual = {tick: 0 for tick in counts}
                        actual.update(sample_counts)
                        assert actual == counts, 'Stored completion count differs from results'
                    report['checks'].append({'panel': panel['id'], 'step': step, 'filters': list(values),
                                            'start': left, 'end': right, 'passed': True, **comparison})
                    done_checks.add(key)
            save(root, 'batch-performance.json', report)
            if step not in (60, 120): continue
            expr = expression(panel, step)
            for nocache in (True, False):
                key = (panel['id'], step, nocache)
                if key in done_benchmarks: continue
                pairs = []
                for iteration in range(41):
                    health()
                    elapsed = [None, None]
                    for version in ((0, 1) if iteration % 2 else (1, 0)):
                        begin = time.monotonic()
                        proxy_query(panel['project'], ('victoriametrics', NAME)[version], expr, start, end, step, nocache)
                        elapsed[version] = time.monotonic() - begin
                    pairs.append(elapsed)
                benchmark = evaluate_benchmark({'panel': panel['id'], 'step': step, 'nocache': nocache, 'pairs': pairs})
                report['benchmarks'].append(benchmark); done_benchmarks.add(key)
                save(root, 'batch-performance.json', report)
                print(panel['id'], step, nocache, round(benchmark['before_median'], 4), round(benchmark['after_median'], 4), benchmark['passed'], flush=True)
    assert done_checks == expected_checks and done_benchmarks == expected_benchmarks, 'Incomplete admission evidence'
    report['resources_unchanged'] = normalized(snapshot()) == normalized(json.loads((root / 'batch-prepared.json').read_text()))
    report['services_unchanged'] = fingerprint() == json.loads((root / 'batch-services.json').read_text())
    report['passed'] = all(r['passed'] for r in report['benchmarks']) and report['resources_unchanged'] and report['services_unchanged']
    save(root, 'batch-performance.json', report)


def impact(root, group):
    catalog = json.loads(CATALOG.read_text())
    meta = json.loads((root / 'batch-meta.json').read_text())
    before = json.loads((root / 'batch-prepared.json').read_text())
    assert normalized(snapshot()) == normalized(before)
    assert fingerprint() == json.loads((root / 'batch-services.json').read_text())
    ranges = readiness(catalog, group)
    report = {'passed': False, 'group': group, 'samples': 41, 'window_seconds': WINDOW_SECONDS, 'catalog_sha256': meta['catalog_sha256'],
              'candidate_sha256': meta['candidate_sha256'], 'benchmarks': [], 'health': [],
              'method': 'one external unchanged probe at a time, concurrent start with group target load; target concurrency 3; alternating 41 pairs'}
    panels = [p for p in catalog['panels'] if p['group'] == group]
    for project in sorted({p['project'] for p in panels}):
        probes = []
        for dashboard in ('accelerator-resources', 'monitoring-health'):
            doc, key = section_panels(before, project, dashboard)[0]
            panel = doc['spec']['panels'][key]
            probes.append((doc['metadata']['name'] + '/' + key, panel['spec']['queries'][0]['spec']['plugin']['spec']['query']))
        for step in (60, 120):
            end = min(ranges[p['id'] + ':' + p['revision'] + ':' + str(step)][1] for p in panels)
            start = end - WINDOW_SECONDS
            for nocache in (True, False):
                for probe_name, probe in probes:
                    pairs = []
                    for iteration in range(41):
                        health()
                        measured = [None, None]
                        for version in ((0, 1) if iteration % 2 else (1, 0)):
                            gate = threading.Barrier(2)
                            def load():
                                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                                    gate.wait()
                                    list(executor.map(lambda p: proxy_query(p['project'], ('victoriametrics', NAME)[version], expression(p, step), start, end, step, nocache), panels))
                            def test():
                                gate.wait(); began = time.monotonic()
                                proxy_query(project, 'victoriametrics', probe.replace('$__interval', str(step) + 's'), start, end, step, nocache)
                                return time.monotonic() - began
                            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                                work, sample = executor.submit(load), executor.submit(test)
                                measured[version] = sample.result(); work.result()
                        pairs.append(measured)
                    old, new = [sorted(p[i] for p in pairs)[math.ceil(.95 * 41) - 1] for i in (0, 1)]
                    report['benchmarks'].append({'project': project, 'probe': probe_name, 'step': step, 'nocache': nocache,
                        'pairs': pairs, 'before_p95': old, 'after_p95': new, 'passed': new <= old * 1.05})
                    current = health()
                    report['health'].append({'at': time.time(), 'passed': True, 'model_lags': {k:time.time()-v['processed_at'] for k,v in current['environments'].items()}})
                    save(root, 'non-target-performance.json', report)
                    print(project, probe_name, step, nocache, old, new, new <= old * 1.05, flush=True)
    report['resources_unchanged'] = normalized(snapshot()) == normalized(before)
    report['services_unchanged'] = fingerprint() == json.loads((root / 'batch-services.json').read_text())
    report['passed'] = all(r['passed'] for r in report['benchmarks']) and report['resources_unchanged'] and report['services_unchanged']
    save(root, 'non-target-performance.json', report)


def apply(root, group):
    before = json.loads((root / 'batch-prepared.json').read_text()); after = json.loads((root / 'batch-candidate.json').read_text())
    report = json.loads((root / 'batch-performance.json').read_text())
    browser = json.loads((root / 'materialized-browser.json').read_text())
    impact = json.loads((root / 'non-target-performance.json').read_text())
    assert report['passed'] and report['samples'] == 41 and report['candidate_sha256'] == sha(after)
    assert report['performance_policy'] == PERFORMANCE_POLICY and report['benchmarks'], 'Missing current performance policy evidence'
    assert all(evaluate_benchmark(b)['passed'] for b in report['benchmarks']), 'Target query is not faster'
    assert report['window_seconds'] == impact['window_seconds'] == WINDOW_SECONDS, 'Wrong admission window'
    assert browser['passed'] and browser['catalog_sha256'] == report['catalog_sha256']
    assert impact['samples'] == 41 and impact['catalog_sha256'] == report['catalog_sha256'] and impact['group'] == group
    assert impact['candidate_sha256'] == sha(after)
    assert impact['passed'], 'Non-target impact admission incomplete'
    assert normalized(snapshot()) == normalized(before), 'Concurrent resource edit'
    assert fingerprint() == json.loads((root / 'batch-services.json').read_text())
    readiness(json.loads(CATALOG.read_text()), group)
    changes = json.loads((root / 'batch-changes.json').read_text())
    sources = [d for d in after['datasources'] if d['metadata']['name'] == NAME]
    meta = json.loads((root / 'batch-meta.json').read_text())
    if meta.get('replace_revision'):
        from query_slimming import validate_revision_catalog
        from query_release import validate_admission
        previous = root / 'batch-previous-catalog.json'
        assert hashlib.sha256(previous.read_bytes()).hexdigest() == meta['previous_catalog_sha256']
        assert hashlib.sha256(CATALOG.read_bytes()).hexdigest() == meta['catalog_sha256']
        validate_revision_catalog(json.loads(previous.read_text()), json.loads(CATALOG.read_text()), group)
        raw_before, raw_after, raw_changes = validate_admission(root, 'a3-histograms', allow_revision=True)
        assert normalized(raw_before) == normalized(before) and normalized(raw_after) == normalized(after)
        assert {(c['project'], c['dashboard'], c['panel']) for c in raw_changes} == {(c['project'], c['dashboard'], c['panel']) for c in changes}
        entries = [[c['project'], c['dashboard'], c['panel'], 'histogram-monotonic'] for c in changes]
        # The datasource already exists and is unchanged. Do not rewrite any
        # datasource, especially those belonging to the other accelerated groups.
        generator_plan(root, changes, group=group, rewrites=entries, replace_revision=True)
    else:
        generator_plan(root, changes, group, sources)
    journal = []; save(root, 'batch-journal.json', journal)
    try:
        for candidate in after['dashboards']:
            old = next(d for d in before['dashboards'] if path(d) == path(candidate))
            if old['spec'] == candidate['spec']: continue
            current = api(path(old)); assert current['spec'] == old['spec']
            new = copy.deepcopy(candidate); new['metadata'] = current['metadata']
            journal.append({'before': current, 'after': new}); save(root, 'batch-journal.json', journal)
            api(path(new), 'PUT', new); readback(api, path(new), new)
        generator_install(root)
        assert normalized(snapshot()) == normalized(after)
        publication = {'group': group, 'at': time.time(), 'panels': len(changes)}
        save(root, 'batch-publication.json', publication)
    except BaseException as error:
        record_failure(root, 'batch-apply-failure.json', error)
        raise


def observe(root, group):
    catalog = json.loads(CATALOG.read_text())
    deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
    bad = 0
    report = {'passed': False, 'mode': 'maintenance-window', 'start': time.time(), 'normal_start': None,
              'normal_seconds': 0, 'timeout_seconds': OBSERVATION_TIMEOUT_SECONDS,
              'state': 'waiting_for_normal_scheduling', 'records': []}
    try:
        save(root, 'batch-observation.json', report)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('Startup observation did not complete within the readiness timeout')
            try:
                result = read_health()
                accelerator = result['perses_acceleration']
                scheduling = {key: accelerator[key] for key in ('backfill_priority_active', 'parallel_a3_backfill')}
                assert all(isinstance(value, bool) for value in scheduling.values()), 'Unknown scheduling state'
                if any(scheduling.values()):
                    # Wait for temporary scheduling to finish before accepting readiness.
                    checked_jobs(catalog, group, accelerator)
                    report.update(state='waiting_for_normal_scheduling', normal_start=None, normal_seconds=0)
                else:
                    validate_health(result)
                    readiness(catalog, group, accelerator=accelerator)
                    report.update(state='ready', normal_start=time.time())
                bad = 0
                report['records'].append({'at': time.time(), 'state': report['state'], **scheduling,
                    'model_lags': {k: time.time()-v['processed_at'] for k,v in result['environments'].items()}})
            except (OSError, ValueError, KeyError, AssertionError) as error:
                report.update(state='unhealthy', normal_start=None, normal_seconds=0)
                report['records'].append({'at': time.time(), 'state': 'unhealthy',
                                          'error': type(error).__name__ + ': ' + str(error)[:300]})
                bad += 1
                if bad >= 3: raise
            if report['state'] == 'ready':
                report.update(passed=True, state='observed', end=time.time())
                save(root, 'batch-observation.json', report)
                return
            save(root, 'batch-observation.json', report)
            time.sleep(5)
    except BaseException as error:
        if hasattr(error, 'add_note'):
            error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
        report.update(passed=False, state='failed', end=time.time(),
                      error=type(error).__name__ + ': ' + str(error)[:300],
                      recovery='fix_forward', automatic_rollback=False)
        try:
            save(root, 'batch-observation.json', report)
        except OSError as reporting_error:
            if hasattr(error, 'add_note'):
                error.add_note('Failure report could not be saved: ' + str(reporting_error))
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('prepare', 'audit', 'impact', 'apply', 'observe'))
    parser.add_argument('--group', choices=GROUPS, required=True); parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--resume', action='store_true', help='Resume interrupted audit with unchanged resources and saved fixed windows')
    parser.add_argument('--catalog', type=Path, default=CATALOG, help='Catalog matching the deployed API')
    parser.add_argument('--replace-revision', action='store_true', help='Explicitly replace an already accelerated A3 group at prepare')
    parser.add_argument('--previous-catalog', type=Path, help='Frozen catalog before the expression replacement')
    args = parser.parse_args(); assert args.evidence.is_dir()
    CATALOG = args.catalog.resolve()
    assert not args.resume or args.action == 'audit', '--resume is only for audit'
    assert bool(args.previous_catalog) == args.replace_revision, 'Use --replace-revision with --previous-catalog'
    assert not args.replace_revision or (args.action == 'prepare' and args.group == 'a3'), 'Revision replacement is an A3 prepare operation'
    if args.replace_revision: prepare(args.evidence, args.group, args.previous_catalog)
    elif args.resume: audit(args.evidence, args.group, resume=True)
    else: globals()[args.action](args.evidence, args.group)
