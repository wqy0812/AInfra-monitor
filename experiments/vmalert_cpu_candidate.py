"""All-catalog native writer/read verification on disposable local infrastructure.

--serve retains local comparison pages
for at most 15 minutes, or until OUTPUT/stop is created. Every writer invocation
gets a fresh publication identity; do not restart its container/config in place.
"""
import argparse
import asyncio
import copy
import hashlib
import itertools
import json
import math
import secrets
import socket
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from vmalert_recording_trial import docker, fixtures, equal, VM_IMAGE, ALERT_IMAGE, ROOT, CATALOG
from tests.legacy_acceleration import AccelerationService, install_routes
from monitoring.perses_recording import RecordingService, KIND, PUBLICATION, GENERATION, recording_rules, record_name


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    token = 'cpu-rr-' + uuid.uuid4().hex[:10]
    containers = []
    network = False
    servers = []
    report = {'scope': 'All 18 panels; local synthetic data, not production performance', 'checks': [], 'faults': []}
    catalog_bytes = CATALOG.read_bytes()
    catalog = json.loads(catalog_bytes)
    report.update(catalog_sha256=hashlib.sha256(catalog_bytes).hexdigest(),
                  panels=len(catalog['panels']), steps=catalog['steps'])
    try:
        docker('network', 'create', token)
        network = True
        vm_name = token + '-vm'
        docker('run', '-d', '--name', vm_name, '--network', token, '-p', '127.0.0.1::8428',
               '--cpus', '2', '--memory', '1g', '--tmpfs', '/storage:rw,size=536870912', VM_IMAGE,
               '-storageDataPath=/storage', '-retentionPeriod=1d', '-memory.allowedBytes=512MiB',
               '-storage.minFreeDiskSpaceBytes=16MiB', '-search.latencyOffset=1ms')
        containers.append(vm_name)
        vm = 'http://' + docker('port', vm_name, '8428/tcp')
        async with httpx.AsyncClient(base_url=vm, trust_env=False, timeout=60) as client:
            for _ in range(30):
                try:
                    (await client.get('/health')).raise_for_status()
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(.2)
            else:
                raise RuntimeError('VM not ready')
            baseline_state, native_state = output / 'baseline-state', output / 'native-state'
            baseline_state.mkdir(); native_state.mkdir()
            baseline = AccelerationService(vm, baseline_state, catalog, client=client)
            candidate = RecordingService(vm, native_state, catalog, client=client)

            async def import_lines(lines):
                if lines:
                    (await client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n')).raise_for_status()
                (await client.get('/internal/force_flush')).raise_for_status()
            baseline.import_lines = import_lines
            end = int((time.time() - 43200) // 3600) * 3600
            base = end - 7440
            cases = ('normal', 'zero', 'gap', 'stale', 'down', 'restart', 'reset', 'missing-bucket', 'disappeared', 'recovered')
            case_base = end + 300
            ends = {case: case_base + i * 660 + 300 for i, case in enumerate(cases)}
            samples, lines = 0, []
            for line in itertools.chain(fixtures(catalog, [], base, cases=('normal',), length=7440),
                                         fixtures(catalog, [], case_base, cases=cases)):
                if line is None:
                    continue
                lines.append(line); samples += 1
                if len(lines) == 10000:
                    await import_lines(lines); lines.clear()
            await import_lines(lines)
            report.update(source_samples=samples, end=end, case_ends=ends)

            async def start_writer(label, *, replay=None, datasource=None, selected_catalog=catalog):
                # New config, new publication and new container for EVERY run.
                publication = uuid.uuid4().hex
                config = recording_rules(selected_catalog, publication, time.time_ns())
                path = output / (label + '-rules.json')
                with path.open('x') as stream:
                    json.dump(config, stream, indent=2)
                name = token + '-' + label
                options = ['-rule=/rules.json', '-datasource.url=' + (datasource or f'http://{vm_name}:8428'),
                           f'-remoteWrite.url=http://{vm_name}:8428', '-datasource.queryStep=0',
                           '-remoteWrite.flushInterval=100ms', '-group.maxStartDelay=1s']
                if replay:
                    options += ['-replay.rulesDelay=0', '-replay.ruleRetryAttempts=1', '-replay.disableProgressBar',
                                '-replay.timeFrom=' + datetime.fromtimestamp(replay[0], timezone.utc).isoformat(),
                                '-replay.timeTo=' + datetime.fromtimestamp(replay[1], timezone.utc).isoformat()]
                docker('create', '--name', name, '--network', token, '--cpus', '1', '--memory', '256m',
                       '-p', '127.0.0.1::8880', ALERT_IMAGE, *options)
                containers.append(name)
                docker('cp', str(path), name + ':/rules.json')
                docker('start', name)
                if replay:
                    process = await asyncio.create_subprocess_exec('docker', 'wait', name,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 180)
                    (output / (label + '.log')).write_text(docker('logs', name))
                    if process.returncode or stdout.strip() != b'0':
                        raise RuntimeError('Replay failed: ' + stderr.decode())
                return name, publication

            await start_writer('replay', replay=(base // 3600 * 3600, max(ends.values()) + 60))
            await import_lines([])

            def pairs(panel, step, left, right, bindings=None, extra=()):
                return [('query', baseline.expression(panel, step, bindings)), ('start', str(left)),
                        ('end', str(right)), ('step', str(step)), ('nocache', '1'), *extra]

            async def request(service, params):
                status, body, _ = await service.request('GET', 'api/v1/query_range', params)
                assert status == 200, body
                return json.loads(body)['data']['result']

            async def check(panel, step, left, right, bindings=None, extra=()):
                params = pairs(panel, step, left, right, bindings, extra)
                raw = await baseline.vm_query(dict(params)['query'], left, right, step)
                native = await request(candidate, params)
                assert equal(raw, native), (panel['id'], step, left, bindings)
                return raw, native

            for step in catalog['steps']:
                for panel in catalog['panels']:
                    windows = [('long-normal', end - step, end)]
                    if step <= 120:
                        windows += [(case, (t - 60) // step * step, t // step * step) for case, t in ends.items()]
                    for case, left, right in windows:
                        await baseline.materialize(panel, step, left, right)
                        raw, native = await check(panel, step, left, right)
                        old = await request(baseline, pairs(panel, step, left, right))
                        assert equal(old, native)
                        assert [r['metric'] for r in old] == [r['metric'] for r in native]
                        covered = await candidate.stored(panel, step, list(range(left, right + 1, step)))
                        assert len(covered) == (right - left) // step + 1
                        report['checks'].append({'case': case, 'panel': panel['id'], 'step': step, 'passed': True})
                    for values in itertools.product(*(panel['variables'][k] for k in sorted(panel['variables']))):
                        bindings = dict(zip(sorted(panel['variables']), values))
                        await check(panel, step, end - step, end, bindings)
                        report.setdefault('filters', []).append({'panel': panel['id'], 'step': step, 'bindings': bindings})
                print(json.dumps({'stage': 'native semantics', 'step': step}), flush=True)

            # Track actual raw evaluation, rather than infer fallback from a counter.
            source_calls = []
            original_forward = candidate.forward
            async def tracked(method, endpoint, params):
                source_calls.append(dict(params)['query'])
                return await original_forward(method, endpoint, params)
            candidate.forward = tracked
            p = catalog['panels'][0]
            for label, params in (
                ('unrecognized-expression', [('query', 'vector(123)'), ('start', str(end)), ('end', str(end)), ('step', '5')]),
                ('unsupported-step', pairs(p, 7, end - 70, end)),
                ('unsupported-parameter', pairs(p, 5, end - 60, end, extra=[('round_digits', '4')])),
                ('unaligned', pairs(p, 5, end - 59.877, end + .123)),
            ):
                source_calls.clear()
                await request(candidate, params)
                assert dict(params)['query'] in source_calls, label
                report['faults'].append({'case': label, 'raw_fallback': True})
            for label, admin in (
                ('disabled', {'disabled_groups': ['cpu'], 'invalidated': []}),
                ('invalidated', {'disabled_groups': [], 'invalidated': [{'panel': p['id'], 'start': end-60, 'end': end}]}),
            ):
                candidate.admin_path.write_text(json.dumps(admin))
                source_calls.clear()
                await check(p, 5, end-60, end)
                assert baseline.expression(p, 5) in source_calls, label
                report['faults'].append({'case': label, 'raw_fallback': True})
            candidate.admin_path.unlink()
            candidate.now = lambda: end + 30
            source_calls.clear()
            await check(p, 5, end, end)
            assert baseline.expression(p, 5) in source_calls
            candidate.now = time.time
            report['faults'].append({'case': 'recent-60-seconds', 'raw_fallback': True})

            # Exercise actual VM visibility for torn and overlapping writes.
            raw = await baseline.vm_query(baseline.expression(p, 5), end, end, 5)
            assert len(raw) == 2
            for mode in ('missing-manifest', 'partial-values', 'overlap', 'empty'):
                fault_panel = dict(p, revision='fault-' + mode)
                fault_catalog = dict(catalog, panels=[fault_panel])
                fault_service = RecordingService(vm, native_state, fault_catalog, client=client)
                name = record_name(fault_panel, 5)
                pub = uuid.uuid4().hex
                at = ends['stale'] if mode == 'empty' else end
                def line(kind, value, labels=None, publication=pub):
                    tags = dict(labels or {}, **{KIND: kind, PUBLICATION: publication, GENERATION: '1'})
                    return name + '{' + ','.join(k+'='+json.dumps(v) for k,v in tags.items()) + '} ' + str(value) + ' ' + str(at*1000)
                records = []
                if mode != 'missing-manifest': records.append(line('complete', 0 if mode == 'empty' else 2))
                if mode != 'empty':
                    records.append(line('value', raw[0]['values'][0][1], raw[0]['metric']))
                    if mode != 'partial-values':
                        records.append(line('value', raw[1]['values'][0][1], raw[1]['metric'],
                                            uuid.uuid4().hex if mode == 'overlap' else pub))
                await import_lines(records)
                covered = await fault_service.stored(fault_panel, 5, [at])
                assert covered == ({at: {}} if mode == 'empty' else {}), mode
                actual = await request(fault_service, pairs(fault_panel, 5, at, at))
                expected = await baseline.vm_query(baseline.expression(p, 5), at, at, 5)
                assert equal(actual, expected), mode
                if mode == 'partial-values':
                    await import_lines([line('value', raw[1]['values'][0][1], raw[1]['metric'])])
                    assert len((await fault_service.stored(fault_panel, 5, [at]))[at]) == 2
                report['faults'].append({'case': mode, 'passed': True})

            # Real periodic evaluations, then a paused process and a new writer.
            now = int(time.time() // 5) * 5
            await import_lines([l for l in fixtures(catalog, [], now-7440, cases=('normal',), length=7500) if l])
            live, live_publication = await start_writer('live')
            await asyncio.sleep(6)
            await import_lines([])
            live_tick = int((time.time()-60)//5)*5 - 5
            assert await candidate.stored(p, 5, [live_tick]), 'Live writer did not publish'
            docker('pause', live)
            pause_start = int((time.time()-60)//5)*5 + 5
            await asyncio.sleep(11)
            pause_end = int((time.time()-60)//5)*5 - 5
            assert pause_end >= pause_start
            assert await candidate.stored(p, 5, [pause_start]) == {}
            source_calls.clear()
            await check(p, 5, pause_start, pause_start)
            assert baseline.expression(p, 5) in source_calls
            docker('unpause', live)
            docker('stop', '-t', '5', live)
            resume_after = int((time.time()-60)//5)*5 + 5
            restarted, restart_publication = await start_writer('restarted')
            assert restart_publication != live_publication
            # A restart in the same evaluation interval can legitimately create
            # two publications; that boundary MUST fall back. Verify recovery
            # on a later tick which the stopped writer could never publish.
            resumed_tick = resume_after
            for _ in range(20):
                await asyncio.sleep(1)
                await import_lines([])
                if await candidate.stored(p, 5, [resumed_tick]): break
            else:
                raise AssertionError('New writer failed to publish after restart boundary')
            report['faults'].append({'case': 'pause-and-fresh-process', 'gap': pause_start,
                                     'resumed': resumed_tick, 'passed': True})
            # An actual vmalert query failure writes no false successful marker.
            fail_catalog = copy.deepcopy(catalog)
            for panel in fail_catalog['panels']: panel['revision'] += '-failure'
            failed, _ = await start_writer('failure', datasource=f'http://{vm_name}:8428/not-an-api', selected_catalog=fail_catalog)
            await asyncio.sleep(6)
            failure_log = docker('logs', failed)
            assert 'error' in failure_log.lower()
            (output/'failure.log').write_text(failure_log)
            failed_service = RecordingService(vm, native_state, fail_catalog, client=client)
            fp = fail_catalog['panels'][0]
            tick = int((time.time()-60)//5)*5 - 5
            assert await failed_service.stored(fp, 5, [tick]) == {}
            assert equal(await request(failed_service, pairs(fp, 5, tick, tick)),
                         await baseline.vm_query(baseline.expression(fp, 5), tick, tick, 5))
            report['faults'].append({'case': 'native-query-error', 'passed': True})
            native_url = 'http://' + docker('port', restarted, '8880/tcp')
            async with httpx.AsyncClient(trust_env=False) as observer:
                (output/'native-rules-status.json').write_text(json.dumps((await observer.get(native_url+'/api/v1/rules')).json(),indent=2))
            for name in (restarted, failed): docker('stop', '-t', '5', name)
            print(json.dumps({'stage': 'faults', 'checks': len(report['faults'])}), flush=True)

            # Matched coverage; paired local backend timings, not browser latency.
            benchmarks = []
            for panel in catalog['panels']:
                for step in catalog['steps']:
                    left = end - min(1200, 59*step)
                    left = left // step * step
                    await baseline.materialize(panel, step, left, end)
                    for nocache in ('0', '1'):
                        params = [(k, nocache if k == 'nocache' else v) for k,v in pairs(panel, step, left, end)]
                        times = [[], []]
                        await request(baseline, params); await request(candidate, params)
                        for iteration in range(21):
                            for version in ((0,1) if iteration % 2 else (1,0)):
                                started = time.perf_counter()
                                await request((baseline, candidate)[version], params)
                                times[version].append((time.perf_counter()-started)*1000)
                        benchmarks.append({'panel': panel['id'], 'step': step, 'nocache': nocache,
                            'samples': 21, 'baseline_median_ms': statistics.median(times[0]),
                            'candidate_median_ms': statistics.median(times[1])})
            report['benchmarks'] = benchmarks
            report['summary'] = {'semantic_checks': len(report['checks']), 'filter_checks': len(report['filters']),
                                 'fault_checks': len(report['faults']), 'backend_cohorts': len(benchmarks), 'passed': True}
            (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
            print(json.dumps(report['summary']), flush=True)

            if args.serve:
                # Equal historical coverage for the actual browser requests.
                for window_end, seconds in [(end, 300), (end, 3600),
                                             *[(ends[c], 300) for c in ('zero','gap','stale')]]:
                    for panel in catalog['panels']:
                        for step in (5,15,20,60):
                            left = (window_end-seconds)//step*step
                            right = window_end//step*step
                            for start in range(left, right+1, 60*step):
                                await baseline.materialize(panel,step,start,min(right,start+59*step))
                app = FastAPI()
                for name, service in [('baseline', baseline), ('candidate', candidate)]:
                    child = FastAPI()
                    child.state.perses_acceleration = service
                    install_routes(child)
                    app.mount('/'+name, child)
                port = free_port()
                server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
                servers.append(server)
                task = asyncio.create_task(server.serve())
                await asyncio.sleep(.3)
                ui_name = token + '-perses'
                image = 'monitoring-slimming-browser:20261009'
                config = output/'perses.yaml'
                config.write_text('security:\n  enable_auth: false\n  readonly: false\n  encryption_key: '+json.dumps(secrets.token_urlsafe(24))+
                                  '\ndatabase:\n  file:\n    folder: /perses\n    extension: json\nephemeral_dashboard:\n  enable: false\n'
                                  'plugin:\n  path: /plugins\n  archive_path: /plugin-archives\n')
                docker('create', '--name', ui_name, '--cpus', '1', '--memory', '1g', '-p', '127.0.0.1::8080',
                       '--tmpfs', '/perses:rw,size=134217728', '--tmpfs', '/plugin-archives:rw,size=16777216',
                       image, '--config=/config.yaml', '--web.listen-address=0.0.0.0:8080')
                containers.append(ui_name)
                docker('cp', str(args.plugins.resolve()), ui_name+':/plugins', timeout=120)
                docker('cp', str(config), ui_name+':/config.yaml'); docker('start', ui_name)
                ui = 'http://'+docker('port', ui_name, '8080/tcp')
                async with httpx.AsyncClient(base_url=ui, trust_env=False, timeout=30) as ui_client:
                    for _ in range(120):
                        try:
                            (await ui_client.get('/api/v1/health')).raise_for_status(); break
                        except httpx.HTTPError: await asyncio.sleep(.25)
                    targets=[]
                    for version in ('baseline','candidate'):
                        for project in ('dcu-monitoring','xpu-monitoring','a3-monitoring'):
                            folder=ROOT/'perses/projects'/project
                            project_name=version+'-'+project
                            doc=json.loads((folder/'project.json').read_text()); doc['metadata']={'name':project_name}
                            (await ui_client.post('/api/v1/projects',json=doc)).raise_for_status()
                            for filename in ('datasource.json','perses-accelerated-datasource.json'):
                                doc=json.loads((folder/filename).read_text())
                                doc['metadata']={'name':doc['metadata']['name'],'project':project_name}
                                doc['spec']['plugin']['spec']['proxy']['spec']['url']=f'http://host.docker.internal:{port}/{version}/internal/perses'
                                response=await ui_client.post(f'/api/v1/projects/{project_name}/datasources',json=doc)
                                assert response.is_success, response.text
                            selected = [p for p in catalog['panels'] if p['project'] == project]
                            for dashboard in sorted({p['dashboard'] for p in selected}):
                                doc=json.loads((folder/'dashboards'/(dashboard+'.json')).read_text())
                                doc['metadata']={'name':dashboard,'project':project_name}
                                keys=[p['panel'] for p in selected if p['dashboard']==dashboard]
                                doc['spec']['panels']={k:doc['spec']['panels'][k] for k in keys}
                                doc['spec']['layouts']=[{'kind':'Grid','spec':{'display':{'title':'Native recording'},'items':[
                                    {'x':(i%2)*12,'y':(i//2)*8,'width':12,'height':8,'content':{'$ref':'#/spec/panels/'+k}} for i,k in enumerate(keys)]}}]
                                (await ui_client.post(f'/api/v1/projects/{project_name}/dashboards',json=doc)).raise_for_status()
                                targets.append({'version':version,'project':project_name,'dashboard':dashboard,
                                                'key':project+'/'+dashboard,
                                                'titles':[doc['spec']['panels'][k]['spec']['display']['name'] for k in keys]})
                metadata={'ui':ui,'targets':targets,'end':end,'case_ends':ends,'proxy_port':port,'scope':'all 18 affected panels'}
                (output/'browser-ready.json').write_text(json.dumps(metadata,indent=2)+'\n')
                print(json.dumps({'stage':'browser ready','ui':ui}),flush=True)
                deadline=time.monotonic()+900
                while time.monotonic()<deadline and not (output/'stop').exists(): await asyncio.sleep(1)
                server.should_exit=True
                await task
    except BaseException as error:
        report['error']=repr(error)
        if 'summary' in report: report['summary']['passed']=False
        raise
    finally:
        for server in servers: server.should_exit=True
        (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        for name in reversed(containers):
            try:
                (output/(name+'.log')).write_text(docker('logs',name))
                docker('rm','-f',name)
            except RuntimeError as error: print('Cleanup error:',error,flush=True)
        if network: docker('network','rm',token)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--plugins',type=Path,default=ROOT/'evidence/query-slimming-20261009/browser-assets/etc/perses/plugins')
    asyncio.run(run(parser.parse_args()))
