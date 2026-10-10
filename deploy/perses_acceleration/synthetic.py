"""Original-expression acceptance in a disposable VM, never a production benchmark."""
import argparse
import asyncio
import itertools
import json
import math
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'perses')]
from acceleration_catalog import build
from query_acceleration import prepare, SERIES_LABEL, ORDER_LABEL
from dashboard_columns import source_change
from monitoring.perses_acceleration import AccelerationService, metric_identity, digest

CASES = ('normal', 'zero', 'no-flow', 'gap', 'stale', 'down', 'restart', 'reset',
         'missing-bucket', 'inconsistent-buckets', 'disappeared', 'recovered')


def same(left, right):
    def keyed(rows):
        return {metric_identity(r['metric']): {t: v for t, v in r['values']} for r in rows}
    a, b = keyed(left), keyed(right)
    assert a.keys() == b.keys(), ('labels', a.keys() - b.keys(), b.keys() - a.keys())
    for key in a:
        assert a[key].keys() == b[key].keys(), ('timestamps or gaps', key)
        for ts, value in a[key].items():
            assert value == b[key][ts] or math.isclose(float(value), float(b[key][ts]), rel_tol=1e-9, abs_tol=1e-10)


def fixtures(catalog, changes, base, cases=CASES, length=300):
    """Each case occupies a separate time block; use exact original metric names."""
    histograms = {}
    for panel in catalog['panels']:
        if panel['group'] == 'cpu': continue
        expression = panel['expression']
        name = re.search(r'((?:sglang|vllm):\w+)_bucket\{', expression)[1]
        if panel['group'] == 'dcu':
            count = int(re.search(r'\) != (\d+)\)', expression)[1])
            bounds = [str(i) for i in range(count - 1)] + ['+Inf']
            role = 'prefill' if 'backend-prefill' in panel['id'] else 'decode'
            scopes = [('dcu-pd', 'sglang-' + role, 'dcu1' if role == 'prefill' else 'dcu2', role)]
        else:
            bounds = sorted(set(re.findall(r'\ble="([^\"]+)"', expression)), key=float)
            scopes = [('a3-vllm', 'vllm-a3', 'a3-1', 'prefill'), ('a3-vllm', 'vllm-a3', 'a3-2', 'decode')]
        for scope in scopes:
            histograms[(name, scope)] = bounds
    mooncake = set()
    stages = set()
    for change in changes:
        for q in change['before']['spec']['queries']:
            expression = q['spec']['plugin']['spec']['query']
            mooncake.update(re.findall(r'(master_[a-z_]+_total)\{', expression))
            stages.update(re.findall(r'\bstage="([^\"]+)"', expression))
    ends = {case: base + i * (length + 360) + length for i, case in enumerate(cases)}
    for case, end in ends.items():
        start = end - length
        for i in range(length // 5 + 1):
            if (case == 'gap' and i == 58) or (case == 'stale' and i >= 56) or (case == 'disappeared' and i >= 31) or (case == 'recovered' and 25 <= i <= 40):
                continue
            timestamp = (start + i * 5) * 1000
            progress = 100 if case in ('zero', 'no-flow') else i + 100
            if case == 'reset' and i >= 58: progress = i - 58
            boot = start + 290 if case == 'restart' and i >= 58 else start - 7200
            emitted = set()
            def emit(name, value, labels):
                text = name + '{' + ','.join(k + '=' + json.dumps(v) for k, v in labels.items()) + '}'
                if text in emitted: return None
                emitted.add(text)
                return text + ' ' + str(value) + ' ' + str(timestamp)
            for env, prefix, host_job in [('dcu-pd', 'dcu', None), ('xpu-pd', 'xpu-', 'node-xpu')]:
                for num, role in [(1, 'prefill'), (2, 'decode')]:
                    labels = {'environment': env, 'job': host_job or 'node-' + role, 'node': prefix + str(num), 'instance': prefix + str(num) + ':9100'}
                    yield emit('up', 0 if case == 'down' and i == 58 else 1, labels)
                    yield emit('node_boot_time_seconds', boot, labels)
                    for cpu in ('0', '1'):
                        for mode, factor in [('idle', 1 if case == 'zero' else .5), ('iowait', 0 if case == 'zero' else .1), ('user', 0 if case == 'zero' else .4)]:
                            yield emit('node_cpu_seconds_total', progress * 5 * factor, dict(labels, cpu=cpu, mode=mode))
            for (name, (env, job, node, role)), bounds in histograms.items():
                labels = {'environment': env, 'job': job, 'node': node, 'instance': node + ':8000'}
                yield emit('up', 0 if case == 'down' and i == 58 else 1, labels)
                for rank in ('0', '1'):
                    tags = dict(labels, engine=rank) if env == 'a3-vllm' else dict(labels, role=role, engine_type='fixture', model_name='fixture', dp_rank='0', tp_rank=rank, pp_rank='0', moe_ep_rank='0', stage='fixture', cache_type='fixture')
                    for j, bound in enumerate(bounds):
                        if case == 'missing-bucket' and j == 0 and rank == '0': continue
                        value = progress * (j + 1) ** (int(rank) + 1)
                        if case == 'inconsistent-buckets' and j == len(bounds) - 1 and rank == '0': value += 1
                        yield emit(name + '_bucket', value, dict(tags, le=bound))
                    total = progress * len(bounds) ** (int(rank) + 1)
                    yield emit(name + '_count', total, tags)
                    if env == 'dcu-pd': yield emit(name + '_sum', total * 10, tags)
                    else: yield emit(name + '_created', boot, tags)
            for env in ('dcu-pd', 'a3-vllm', 'xpu-pd'):
                labels = {'environment': env, 'job': 'aigate', 'instance': 'gateway'}
                yield emit('up', 0 if case == 'down' and i == 58 else 1, labels)
                labels['request_scope'] = 'all'
                yield emit('aigate_profile_start_time_seconds', boot, labels)
                yield emit('aigate_live_backend_groups', 2, labels)
                for stage in sorted(stages):
                    for backend in ('first', 'second'):
                        if case == 'missing-bucket' and backend == 'second' and stage == sorted(stages)[0] and i >= 56: continue
                        yield emit('aigate_inflight_requests_by_stage', 0 if case == 'zero' else 10 + i % 7, dict(labels, stage=stage, backend=backend))
                for role in ('prefill', 'decode'):
                    for kind in ('ttft', 'itl', 'e2e'):
                        for percentile in ('p50', 'p95', 'p99'):
                            tags = {'environment': env, 'schema': 'request-metrics-v2', 'path': 'nodes.' + role + '.percentiles.' + kind + '.' + percentile}
                            yield emit('monitoring_chart_value', 0 if case == 'zero' else int(percentile[1:]) / 100, tags)
                            yield emit('monitoring_chart_valid', 0 if case in ('down', 'restart', 'reset') and i == 58 else 1, tags)
            moon_labels = {'environment': 'a3-vllm', 'job': 'mooncake-a3', 'instance': 'master'}
            yield emit('up', 0 if case == 'down' and i == 58 else 1, moon_labels)
            for n, name in enumerate(sorted(mooncake)):
                yield emit(name, progress * (n + 1), moon_labels)


async def main(args):
    assert args.vm.startswith('http://127.0.0.1:'), 'Synthetic data may only enter a disposable loopback VM'
    before = json.loads(args.snapshot.read_text())
    after, changes = prepare(before)
    catalog = build(before)
    args.evidence.mkdir(parents=True, exist_ok=True)
    state = args.evidence / 'synthetic-state'; state.mkdir(exist_ok=True)
    # Stable alignment for all three required grids; cases never overlap.
    base = int(time.time() // 60) * 60 - len(CASES) * 660 - 1200
    if args.reuse_fixtures:
        base = json.loads((args.evidence / 'synthetic-fixtures.json').read_text())['base']
    ends = {case: base + i * 660 + 300 for i, case in enumerate(CASES)}
    service = AccelerationService(args.vm, state, catalog)
    lines, count = [], 0
    for line in (() if args.reuse_fixtures else fixtures(catalog, changes, base)):
        if line is None: continue
        lines.append(line); count += 1
        if len(lines) == 10000:
            await service.import_lines(lines); lines.clear()
    await service.import_lines(lines)
    (await service.client.get(args.vm + '/internal/force_flush')).raise_for_status()
    (args.evidence / 'synthetic-fixtures.json').write_text(json.dumps({'base': base, 'ends': ends}))
    original_import = service.import_lines
    async def flushed(lines):
        await original_import(lines)
        (await service.client.get(args.vm + '/internal/force_flush')).raise_for_status()
    service.import_lines = flushed
    report = {'passed': False, 'synthetic': True, 'vm': args.vm, 'fixture_points': count, 'ends': ends,
              'candidate_sha256': __import__('hashlib').sha256(json.dumps(after, sort_keys=True).encode()).hexdigest(), 'checks': []}
    try:
        for case, end in ends.items():
            for step in (5, 15, 60):
                start = end - 60
                for change in changes:
                    tagged = source_change(change)['panel'] not in ('core-ttft', 'core-itl', 'core-e2e')
                    old = []
                    for index, q in enumerate(change['before']['spec']['queries']):
                        spec = q['spec']['plugin']['spec']
                        expr = spec['query'].replace('$__interval', str(step) + 's').replace('$role', '.*')
                        rows = await service.vm_query(expr, start, end, step)
                        if tagged:
                            for row in rows:
                                row['metric'].update({SERIES_LABEL: spec['seriesNameFormat'], ORDER_LABEL: '%02d' % index})
                        else:
                            for row in rows:
                                row['metric'][ORDER_LABEL] = '%02d' % index
                        old.extend(rows)
                    expr = change['after']['spec']['queries'][0]['spec']['plugin']['spec']['query'].replace('$__interval', str(step) + 's').replace('$role', '.*')
                    new = await service.vm_query(expr, start, end, step)
                    same(old, new)
                    if case == 'normal': assert new, (case, change['panel'])
                    report['checks'].append({'kind': 'merge', 'case': case, 'step': step, 'project': change['project'], 'panel': change['panel'], 'series': len(new)})
                for panel in (() if args.merge_only else catalog['panels']):
                    await service.materialize(panel, step, start, end)
                    keys = sorted(panel['variables'])
                    for values in itertools.product(*(panel['variables'][key] for key in keys)):
                        bindings = dict(zip(keys, values))
                        expr = service.expression(panel, step, bindings)
                        raw = await service.vm_query(expr, start, end, step)
                        status, data, _ = await service.request('GET', 'api/v1/query_range', [
                            ('query', expr), ('start', str(start)), ('end', str(end)), ('step', str(step)), ('nocache', '1'), ('latency_offset', '1ms')])
                        assert status == 200
                        same(raw, json.loads(data)['data']['result'])
                        assert [r['metric'] for r in raw] == [r['metric'] for r in json.loads(data)['data']['result']], ('series order', panel['id'])
                        if case == 'normal' and all(v == '.*' for v in values): assert raw, (case, panel['id'])
                        report['checks'].append({'kind': 'materialize', 'case': case, 'step': step, 'panel': panel['id'], 'filters': bindings, 'series': len(raw)})
            print('Verified case:', case, flush=True)
            (args.evidence / 'synthetic-progress.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        if args.merge_only:
            report.update(passed=True, scope='merge-only')
            (args.evidence / 'merge-synthetic.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'passed': True, 'checks': len(report['checks']), 'scope': 'merge-only'}), flush=True)
            return
        # Additional steps measured from the unchanged 1920x1080 frontend.
        extra_steps = (20, 120, 600, 3600)
        long_end = int((base - 600) // 3600) * 3600
        lines = []
        for line in fixtures(catalog, changes, long_end - 3720, cases=('normal',), length=3720):
            if line is None: continue
            lines.append(line)
            if len(lines) == 10000:
                await original_import(lines); lines.clear()
        await original_import(lines)
        (await service.client.get(args.vm + '/internal/force_flush')).raise_for_status()
        extra_catalog = build(before, extra_steps)
        extra = AccelerationService(args.vm, state, extra_catalog, client=service.client)
        extra.import_lines = flushed
        for step in extra_steps:
            for panel in extra_catalog['panels']:
                await extra.materialize(panel, step, long_end, long_end)
                keys = sorted(panel['variables'])
                for values in itertools.product(*(panel['variables'][key] for key in keys)):
                    bindings = dict(zip(keys, values))
                    expr = extra.expression(panel, step, bindings)
                    raw = await extra.vm_query(expr, long_end, long_end, step)
                    for nocache in ('0', '1'):
                        status, data, _ = await extra.request('GET', 'api/v1/query_range', [
                            ('query', expr), ('start', str(long_end)), ('end', str(long_end)), ('step', str(step)), ('nocache', nocache)])
                        assert status == 200
                        same(raw, json.loads(data)['data']['result'])
                        assert [r['metric'] for r in raw] == [r['metric'] for r in json.loads(data)['data']['result']], ('long series order', panel['id'])
                    if all(v == '.*' for v in values): assert raw, ('long normal', panel['id'], step)
                    report['checks'].append({'kind': 'materialize', 'case': 'long-normal', 'step': step,
                        'panel': panel['id'], 'filters': bindings, 'series': len(raw), 'cache_modes': [0, 1]})
            print('Verified measured step:', step, flush=True)
        report['passed'] = True
        report['long_normal_end'] = long_end
        report['fast_requests'] = service.counters['fast_requests'] + extra.counters['fast_requests']
        await extra.close()
        (args.evidence / 'merge-synthetic.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'passed': True, 'checks': len(report['checks']), 'fixture_points': count}), flush=True)
    finally:
        await service.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--vm', default='http://127.0.0.1:18547')
    parser.add_argument('--reuse-fixtures', action='store_true')
    parser.add_argument('--merge-only', action='store_true')
    asyncio.run(main(parser.parse_args()))
