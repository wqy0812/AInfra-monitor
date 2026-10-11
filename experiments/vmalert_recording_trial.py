"""Run issue #4 semantics experiment against disposable local VM + vmalert.

Requires the project's Python environment and locally available Docker images.
Uses the existing original-expression fixture builder. It is not a production
or browser performance benchmark. All containers and the network are removed
in finally; rules, logs and evidence remain in the specified local directory.
"""
import argparse
import asyncio
import hashlib
import itertools
import json
import math
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'deploy/perses_acceleration')]
from synthetic import fixtures
from tests.legacy_acceleration import AccelerationService, points_by_time, result_rows
from vmalert_recording_rules import CATALOG, expression, record_name, rules, selector

VM_IMAGE = 'victoriametrics/victoria-metrics:v1.151.0'
ALERT_IMAGE = 'victoriametrics/vmalert:v1.151.0'


def docker(*args, timeout=60):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:])
    return (result.stdout + result.stderr if args[0] == 'logs' or '-dryRun' in args
            else result.stdout).strip()


def canonical(rows):
    return {json.dumps(row['metric'], sort_keys=True): {int(t): float(v) for t, v in row['values']}
            for row in rows}


def equal(left, right):
    a, b = canonical(left), canonical(right)
    return a.keys() == b.keys() and all(a[k].keys() == b[k].keys() and all(
        math.isclose(v, b[k][t], rel_tol=1e-9, abs_tol=1e-10) for t, v in a[k].items()) for k in a)


def points(rows):
    return sum(len(row['values']) for row in rows)


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    token = 'issue4-' + uuid.uuid4().hex[:10]
    vm_name, alert_name = token + '-vm', token + '-vmalert'
    catalog_bytes = CATALOG.read_bytes()
    catalog = json.loads(catalog_bytes)
    config = rules(catalog)
    (output / 'rules.json').write_text(json.dumps(config, indent=2) + '\n')
    report = {'scope': 'local synthetic semantics, not production performance',
              'catalog_sha256': hashlib.sha256(catalog_bytes).hexdigest(),
              'panels': len(catalog['panels']), 'steps': catalog['steps'],
              'rules': sum(len(g['rules']) for g in config['groups']), 'checks': [],
              'images': {image: json.loads(docker('image', 'inspect', image))[0]['Id']
                         for image in (VM_IMAGE, ALERT_IMAGE)}}
    made_network = made_vm = made_alert = made_dry = False
    try:
        docker('network', 'create', token)
        made_network = True
        docker('run', '-d', '--name', vm_name, '--network', token,
               '-p', '127.0.0.1::8428', '--cpus', '2', '--memory', '1g',
               '--tmpfs', '/storage:rw,nosuid,nodev,size=536870912', VM_IMAGE,
               '-storageDataPath=/storage', '-retentionPeriod=1d',
               '-memory.allowedBytes=512MiB', '-storage.minFreeDiskSpaceBytes=16MiB',
               '-search.latencyOffset=1ms')
        made_vm = True
        url = 'http://' + docker('port', vm_name, '8428/tcp')
        async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=60) as client:
            for attempt in range(30):
                try:
                    (await client.get('/health')).raise_for_status()
                    break
                except (httpx.HTTPError, httpx.RequestError):
                    await asyncio.sleep(.2)
            else:
                raise RuntimeError('Temporary VM did not become healthy')
            end = int((time.time() - 21600) // 3600) * 3600
            base = end - 3720
            cases = ('normal', 'zero', 'gap', 'stale', 'down', 'reset',
                     'missing-bucket', 'disappeared', 'recovered')
            case_base = end + 300
            ends = {case: case_base + i * 660 + 300 for i, case in enumerate(cases)}
            batches = itertools.chain(fixtures(catalog, [], base, cases=('normal',), length=3720),
                                      fixtures(catalog, [], case_base, cases=cases))
            lines, count = [], 0
            for line in batches:
                if line is None:
                    continue
                lines.append(line)
                count += 1
                if len(lines) == 10000:
                    (await client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n')).raise_for_status()
                    lines.clear()
            if lines:
                (await client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n')).raise_for_status()
            (await client.get('/internal/force_flush')).raise_for_status()
            report.update(fixture_samples=count, long_normal_end=end, case_ends=ends)
            # Replay starts its grid at timeFrom, not the Unix-aligned grid used
            # by live evaluation. Align to all catalog intervals, including 1h.
            common_grid = math.lcm(*catalog['steps'])
            replay_start = base // common_grid * common_grid
            report['replay_start'] = replay_start
            print(json.dumps({'stage': 'fixtures', 'samples': count}), flush=True)
            # docker cp works when the workspace is outside Desktop's shared
            # directories; no host file-sharing configuration needs changing.
            docker('create', '--name', token + '-dry', '--network', 'none', ALERT_IMAGE,
                   '-rule=/rules.json', '-dryRun')
            made_dry = True
            docker('cp', str(output / 'rules.json'), token + '-dry:/rules.json')
            docker('start', '-a', token + '-dry')
            (output / 'dry-run.log').write_text(docker('logs', token + '-dry'))
            assert json.loads(docker('inspect', token + '-dry'))[0]['State']['ExitCode'] == 0
            start_time = time.perf_counter()
            arguments = ['create', '--name', alert_name, '--network', token,
                       '--cpus', '1', '--memory', '256m', ALERT_IMAGE,
                       '-rule=/rules.json', f'-datasource.url=http://{vm_name}:8428',
                       f'-remoteWrite.url=http://{vm_name}:8428', '-datasource.queryStep=0',
                       '-remoteWrite.flushInterval=100ms', '-replay.rulesDelay=0',
                       '-replay.ruleRetryAttempts=1', '-replay.disableProgressBar',
                       '-replay.timeFrom=' + datetime.fromtimestamp(replay_start, timezone.utc).isoformat(),
                       '-replay.timeTo=' + datetime.fromtimestamp(max(ends.values()) + 60, timezone.utc).isoformat()]
            docker(*arguments)
            made_alert = True
            docker('cp', str(output / 'rules.json'), alert_name + ':/rules.json')
            with (output / 'replay.log').open('w') as log:
                process = await asyncio.create_subprocess_exec('docker', 'start', '-a', alert_name, stdout=log, stderr=log)
                try:
                    rc = await asyncio.wait_for(process.wait(), 600)
                except TimeoutError:
                    docker('stop', '-t', '5', alert_name)
                    await process.wait()
                    raise
            if rc or json.loads(docker('inspect', alert_name))[0]['State']['ExitCode']:
                raise RuntimeError(f'vmalert replay exited {rc}; see replay.log')
            report['replay_seconds'] = time.perf_counter() - start_time
            (await client.get('/internal/force_flush')).raise_for_status()
            print(json.dumps({'stage': 'replay', 'seconds': report['replay_seconds']}), flush=True)

            async def query(expr, left, right, step):
                result = await client.post('/api/v1/query_range', data={'query': expr,
                    'start': left, 'end': right, 'step': step, 'nocache': '1', 'latency_offset': '1ms'})
                result.raise_for_status()
                body = result.json()
                assert body['status'] == 'success' and not body.get('warnings') and not body.get('isPartial')
                return body['data']['result']

            for step in catalog['steps']:
                for panel in catalog['panels']:
                    windows = [('long-normal', end - step, end)]
                    if step <= 120:
                        windows += [(case, int((t - 60) // step) * step, int(t // step) * step)
                                    for case, t in ends.items()]
                    for case, left, right in windows:
                        raw = await query(expression(panel, step), left, right, step)
                        plain = await query(selector(panel, step), left, right, step)
                        exact = await query(selector(panel, step, exact=True), left, right, step)
                        if case == 'long-normal':
                            assert raw, ('empty positive control', panel['id'], step)
                        check = {'panel': panel['id'], 'step': step, 'case': case,
                                 'raw_points': points(raw), 'plain_points': points(plain),
                                 'exact_points': points(exact),
                                 'plain_equal': equal(raw, plain), 'exact_equal': equal(raw, exact)}
                        report['checks'].append(check)
                        if not check['plain_equal'] and 'first_plain_mismatch' not in report:
                            report['first_plain_mismatch'] = dict(check, raw=raw, recorded=plain)
                        if not check['exact_equal'] and 'first_exact_mismatch' not in report:
                            report['first_exact_mismatch'] = dict(check, raw=raw, recorded=exact)
                    # All finite catalog filters, including empty intersections.
                    instant = await client.post('/api/v1/query', data={
                        'query': expression(panel, step), 'time': end, 'step': step,
                        'nocache': '1', 'latency_offset': '1ms'})
                    instant.raise_for_status()
                    body = instant.json()
                    assert body['status'] == 'success' and not body.get('warnings') and not body.get('isPartial')
                    instant_rows = [{'metric': r['metric'], 'values': [r['value']]} for r in body['data']['result']]
                    raw = await query(expression(panel, step), end, end, step)
                    report.setdefault('instant_checks', []).append({'panel': panel['id'], 'step': step,
                        'equal': equal(raw, instant_rows)})
                    keys = sorted(panel['variables'])
                    for values in itertools.product(*(panel['variables'][key] for key in keys)):
                        bindings = dict(zip(keys, values))
                        expr = panel['expression'].replace('$__interval', f'{step}s')
                        for key, value in bindings.items():
                            expr = expr.replace('$' + key, value)
                        raw = await query(expr, end, end, step)
                        exact = await query(selector(panel, step, exact=True, bindings=bindings), end, end, step)
                        report.setdefault('filter_checks', []).append({'panel': panel['id'], 'step': step,
                            'bindings': bindings, 'equal': equal(raw, exact)})
                print(json.dumps({'stage': 'verified', 'step': step}), flush=True)

            # Existing writer and native replay on exactly the same fixture/grid.
            # This is equality validation, not a substitute for the browser matrix.
            state = output / 'custom-state'
            state.mkdir()
            service = AccelerationService(url, state, catalog, client=client)
            original_import = service.import_lines
            async def flushed(lines):
                await original_import(lines)
                (await client.get('/internal/force_flush')).raise_for_status()
            service.import_lines = flushed
            for step in catalog['steps']:
                for panel in catalog['panels']:
                    await service.materialize(panel, step, end - step, end)
                    status, body, _ = await service.request('GET', 'api/v1/query_range', [
                        ('query', expression(panel, step)), ('start', str(end - step)),
                        ('end', str(end)), ('step', str(step)), ('nocache', '1')])
                    assert status == 200
                    rows = json.loads(body)['data']['result']
                    native = await query(selector(panel, step, exact=True), end - step, end, step)
                    report.setdefault('custom_comparisons', []).append({'panel': panel['id'],
                        'step': step, 'equal': equal(rows, native),
                        'series_order_equal': [r['metric'] for r in rows] == [r['metric'] for r in native],
                        'adapter_order_equal': [r['metric'] for r in rows] == [r['metric'] for r in
                            result_rows(points_by_time(native, [end - step, end]))]})
            report['custom_fast_requests'] = service.counters['fast_requests']
            report['summary'] = {
                'checks': len(report['checks']),
                'plain_mismatches': sum(not c['plain_equal'] for c in report['checks']),
                'exact_mismatches': sum(not c['exact_equal'] for c in report['checks']),
                'filter_checks': len(report['filter_checks']),
                'filter_mismatches': sum(not c['equal'] for c in report['filter_checks']),
                'custom_comparisons': len(report['custom_comparisons']),
                'custom_mismatches': sum(not c['equal'] for c in report['custom_comparisons']),
                'series_order_mismatches': sum(not c['series_order_equal'] for c in report['custom_comparisons']),
                'adapter_order_mismatches': sum(not c['adapter_order_equal'] for c in report['custom_comparisons']),
                'instant_checks': len(report['instant_checks']),
                'instant_mismatches': sum(not c['equal'] for c in report['instant_checks']),
            }
            print(json.dumps(report['summary']), flush=True)
            report['guarded_semantics_passed'] = all(report['summary'][k] == 0 for k in (
                'exact_mismatches', 'filter_mismatches', 'custom_mismatches',
                'adapter_order_mismatches', 'instant_mismatches'))
            assert report['guarded_semantics_passed'], 'Guarded candidate still differs; see report.json'
    except BaseException as error:
        report['error'] = repr(error)
        raise
    finally:
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        if made_alert:
            docker('rm', '-f', alert_name)
        if made_dry:
            docker('rm', '-f', token + '-dry')
        if made_vm:
            (output / 'vm.log').write_text(docker('logs', vm_name))
            docker('rm', '-f', vm_name)
        if made_network:
            docker('network', 'rm', token)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output))
