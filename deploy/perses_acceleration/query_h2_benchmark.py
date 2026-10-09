"""Read-only query comparison over verified, persistent TLS/HTTP2 connections.

This diagnostic writes h2-* evidence only; it cannot publish or replace the
separate query release admission artifacts.
"""
import argparse
import concurrent.futures
import hashlib
import json
import threading
import time
import urllib.parse
from pathlib import Path

import merge_release as remote
import query_release as release
from h2_transport import H2Pool


def request(expression, project, end, hours, step, nocache):
    expression = expression.replace('$__interval', str(step) + 's')
    for variable in ('role', 'node', 'device'):
        expression = expression.replace('$' + variable, '.*')
    body = urllib.parse.urlencode(dict(query=expression, start=end - hours * 3600,
                                      end=end, step=step, nocache=int(nocache))).encode()
    return ('/proxy/projects/' + project + '/datasources/victoriametrics/api/v1/query_range', body)


def validate_results(result):
    for value in result['results']:
        assert value['status'] == 'success' and not value.get('isPartial') and not value.get('warnings')
    return result


def measure(target_pool, probe_pool, expressions, probe, project, end, hours, step, nocache, executor):
    gate = threading.Barrier(2)
    def run(pool, items):
        requests = [request(e, project, end, hours, step, nocache) for e in items]
        gate.wait()
        return validate_results(pool.run(requests, remote.TOKEN))
    target = executor.submit(run, target_pool, expressions)
    external = executor.submit(run, probe_pool, [probe])
    target, external = target.result(), external.result()
    return ({'target': target['elapsed'], 'probe': external['elapsed']},
            {'target': target['transfers'], 'probe': external['transfers']})


def run(root, ca):
    assert root.is_dir() and not (root / 'h2-meta.json').exists(), 'Use a fresh evidence directory'
    release.require_predecessors('generation-results')
    before = remote.snapshot()
    after, changes = release.prepare(before, 'generation-results')
    assert len(changes) == 3, 'Expected three unmodified generation-0 panels'
    end = int(time.time() // 3600) * 3600 - 3600
    source = Path(__file__).resolve()
    tool_hashes = release.tool_fingerprint()
    for name in ('query_h2_benchmark.py', 'h2_transport.py', '../../perses/connection.py'):
        p = source.parent / name
        tool_hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    meta = dict(at=time.time(), end=end, candidate_sha256=remote.sha(after),
                services=remote.fingerprint(), tool_sha256=tool_hashes, base=remote.BASE,
                ca_sha256=hashlib.sha256(ca.read_bytes()).hexdigest(), samples=release.SAMPLES)
    for name, value in [('before', before), ('candidate', after), ('changes', changes), ('meta', meta)]:
        remote.save(root, 'h2-' + name + '.json', value)
    report = dict(passed=False, diagnostic_only=True, candidate_sha256=remote.sha(after), end=end,
                  samples=release.SAMPLES, semantics=[], benchmarks=[], health=[release.health_record()],
                  method='TLS verified; target/probe separate persistent HTTP2 pools; target concurrency 3; '
                         '41 alternating paired trials per cohort; complete proxy data, not browser paint')
    remote.api('/api/v1/projects')
    target_pool = H2Pool(remote.BASE, str(ca), concurrency=3)
    probe_pool = H2Pool(remote.BASE, str(ca), concurrency=1)
    try:
        report['transport'] = {'target': target_pool.metadata, 'probe': probe_pool.metadata,
                               'warmup': 'one authenticated GET per pool before semantics and measurements'}
        report['warmup'] = [pool.run([('/api/v1/projects', None)], remote.TOKEN)['transfers']
                            for pool in (target_pool, probe_pool)]
        assert report['warmup'][0][0]['local_port'] != report['warmup'][1][0]['local_port'], 'Probe shares the target connection'
        # Use the same complete curve comparison with a real h2 proxy leg.
        original_query = release.query
        def semantic_query(expression, end, hours, step, nocache, project=None):
            if project is None:
                return original_query(expression, end, hours, step, nocache)
            value = validate_results(target_pool.run([request(expression, project, end, hours, step, nocache)], remote.TOKEN))
            report.setdefault('semantic_transfers', []).extend(value['transfers'])
            return value['results'][0]['data']['result']
        release.query = semantic_query
        try:
            report['semantics'] = [release.check_change(c, end, hours, step) for c in changes
                                   for hours, step in release.SEMANTIC_WINDOWS]
        finally:
            release.query = original_query
        remote.save(root, 'h2-performance.json', report)
        documents = lambda resources, project, dashboard: next(d for d in resources['dashboards']
                     if (d['metadata']['project'], d['metadata']['name']) == (project, dashboard))
        subjects = [(c['project'], c['dashboard'], c['panel'], [c['before'], c['after']]) for c in changes]
        subjects += [(p, d, '__page__', [documents(r, p, d) for r in (before, after)])
                     for p, d in sorted({(c['project'], c['dashboard']) for c in changes})]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            for project, dashboard, panel, versions in subjects:
                health = documents(before, project, 'monitoring-health')
                probe = release.plugin(next(iter(health['spec']['panels'].values()))['spec']['queries'][0])['query']
                if panel == '__page__':
                    groups = [remote.expressions(d) for d in versions]
                else:
                    groups = [[release.plugin(q)['query'] for q in p['spec']['queries']] for p in versions]
                for hours, step in release.WINDOWS:
                    for nocache in (True, False):
                        key = (project, dashboard, panel, hours, step, nocache)
                        pairs = []
                        totals = dict(requests=0, new_connections=0, ports={'target': set(), 'probe': set()})
                        for iteration in range(release.SAMPLES):
                            pair = [None, None]
                            for version in ((0, 1) if iteration % 2 else (1, 0)):
                                pair[version], transfers = measure(target_pool, probe_pool, groups[version], probe,
                                    project, end, hours, step, nocache, executor)
                                with (root / 'h2-samples.jsonl').open('a') as stream:
                                    stream.write(json.dumps(dict(cohort=key, pair=iteration, version=version,
                                                               measurement=pair[version], transfers=transfers)) + '\n')
                                for kind, rows in transfers.items():
                                    totals['requests'] += len(rows)
                                    totals['new_connections'] += sum(r['new_connections'] for r in rows)
                                    totals['ports'][kind].update(r['local_port'] for r in rows)
                            pairs.append(pair)
                        totals['ports'] = {kind: sorted(ports) for kind, ports in totals['ports'].items()}
                        row = dict(zip(('project', 'dashboard', 'panel', 'hours', 'step', 'nocache'), key))
                        row.update(pairs=pairs, transport=totals, **release.evaluate(pairs, target=panel != '__page__'))
                        report['benchmarks'].append(row)
                        report['health'].append(release.health_record())
                        remote.save(root, 'h2-performance.json', report)
                        print(json.dumps({k: v for k, v in row.items() if k != 'pairs'}), flush=True)
        report['resources_unchanged'] = remote.normalized(remote.snapshot()) == remote.normalized(before)
        report['services_unchanged'] = remote.fingerprint() == meta['services']
        report['passed'] = (len(report['benchmarks']) == 24 and len(report['semantics']) == 12
                            and all(r['passed'] for r in report['benchmarks'] + report['semantics'] + report['health'])
                            and report['resources_unchanged'] and report['services_unchanged'])
        remote.save(root, 'h2-performance.json', report)
    finally:
        target_pool.close()
        probe_pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--ca', type=Path, default=Path('/data2/monitoring/perses/tls/server.crt'))
    args = parser.parse_args()
    try:
        run(args.evidence, args.ca)
    except BaseException as error:
        remote.record_failure(args.evidence, 'h2-failure.json', error)
        raise
