"""Small vmalert lifecycle/replay supervisor; no custom metric calculation or writes."""
import argparse
import asyncio
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import time
import uuid
from datetime import datetime, timezone

from .perses_acceleration import atomic_json, digest
from .perses_recording import RecordingService, recording_rules


def allocate(state):
    """Never reuse a process identity, including after an abrupt container restart."""
    with (state / 'perses-recording-generation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = state / 'perses-recording-generation.json'
        previous = json.loads(path.read_text())['generation'] if path.exists() else 0
        value = {'generation': max(previous + 1, time.time_ns()), 'publication': uuid.uuid4().hex}
        temporary = path.with_suffix('.tmp')
        with temporary.open('w') as out:
            json.dump(value, out); out.flush(); os.fsync(out.fileno())
        temporary.replace(path)
        fd = os.open(state, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return value


def replay_range(catalog, now, start=None, end=None):
    grid = math.lcm(*catalog['steps'])
    right = math.floor(now - 60) if end is None else end
    left = right - 43200 if start is None else start
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (left, right)):
        raise ValueError('Finite replay boundaries required')
    if left > right or right > now - 60 or right - left > 43200:
        raise ValueError('Replay must be sealed and bounded to 12 hours per request')
    return math.floor(left / grid) * grid, math.floor(right)


def command(binary, vm, config, listen, replay=None):
    args = [binary, '-rule=' + str(config), '-datasource.url=' + vm, '-remoteWrite.url=' + vm,
            '-datasource.queryStep=0', '-httpListenAddr=' + listen, '-group.maxStartDelay=1s',
            '-remoteWrite.flushInterval=1s', '-rule.evalDelay=60s']
    if replay:
        args += ['-replay.rulesDelay=0', '-replay.ruleRetryAttempts=3', '-replay.disableProgressBar',
                 '-replay.ruleEvaluationConcurrency=1', '-replay.maxDatapointsPerQuery=60',
                 '-replay.timeFrom=' + datetime.fromtimestamp(replay[0], timezone.utc).isoformat(),
                 # replay timeTo is exclusive; include the requested final grid point.
                 '-replay.timeTo=' + datetime.fromtimestamp(replay[1] + 1, timezone.utc).isoformat()]
    return args


async def verify(service, start, end, generation):
    """Only validate coverage. vmalert itself computes and publishes all values."""
    checked = 0
    for panel in service.panels.values():
        for step in service.steps:
            first, last = math.ceil(start / step) * step, math.floor(end / step) * step
            for left in range(first, last + 1, 600 * step):
                ticks = list(range(left, min(last, left + 599 * step) + 1, step))
                found = await service.stored(panel, step, ticks, minimum_generation=generation)
                if len(found) != len(ticks):
                    raise ValueError('Replay incomplete: ' + panel['id'] + ':' + str(step) + ':' + str(left))
                checked += len(ticks)
    return checked


def clear_repaired(state, originals, start, end, panel):
    """Remove only unchanged invalidations fully covered by this verified repair."""
    path = state / 'perses-acceleration-admin.json'
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        admin = json.loads(path.read_text())
        admin['invalidated'] = [item for item in admin['invalidated'] if not (
            item in originals and start <= item['start'] <= item['end'] <= end
            and (panel == '*' or item.get('panel', '*') == panel))]
        atomic_json(path, admin)


async def run(args):
    state = args.state
    if not state.is_dir():
        raise ValueError('Persistent state directory must already exist')
    with (state / 'perses-recording-runtime.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        catalog = json.loads(args.catalog.read_text())
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        status = {'backend': 'vmalert', 'catalog_sha256': digest(catalog), 'started_at': time.time(),
                  'rule_count': len(catalog['panels']) * len(catalog['steps']), 'live': None, 'replay': None}
        processes = set()
        service = RecordingService(args.vm, state, catalog)

        async def launch(kind, selected, replay=None):
            identity = allocate(state)
            key = str(identity['generation'])
            config = state / ('perses-recording-' + kind + '-' + key + '.json')
            atomic_json(config, recording_rules(selected, **identity))
            log = state / ('perses-recording-' + kind + '-' + key + '.log')
            with log.open('xb') as output:
                process = await asyncio.create_subprocess_exec(*command(args.binary, args.vm, config,
                    args.listen if replay is None else '127.0.0.1:0', replay), stdout=output, stderr=output)
            processes.add(process)
            return process, dict(identity, config=str(config), log=str(log), pid=process.pid)

        async def replay(request):
            result = {'id':request['id'], 'state':'preparing', 'panel':request.get('panel','*')}
            status['replay'] = result
            try:
                selected = dict(catalog)
                if request.get('panel', '*') != '*':
                    selected['panels'] = [p for p in catalog['panels'] if p['id'] == request['panel']]
                    if not selected['panels']: raise ValueError('Unknown replay panel')
                start, end = replay_range(catalog, time.time(), request.get('start'), request.get('end'))
                originals = service.admin()['invalidated']
                process, identity = await launch('replay', selected, (start, end))
            except Exception as error:
                result.update(state='failed', error=type(error).__name__ + ': ' + str(error)[:400])
                atomic_json(state / ('perses-recording-replay-rejected-' + request['id'] + '.json'), result)
                return
            result.update(identity, state='running', start=start, end=end,
                          requested_at=request.get('requested_at'))
            try:
                code = await asyncio.wait_for(process.wait(), timeout=3600)
                if code != 0: raise RuntimeError('vmalert replay exited ' + str(code))
                verifier = RecordingService(args.vm, state, selected, client=service.client)
                deadline = time.monotonic() + 120
                while True:
                    try:
                        count = await verify(verifier, start, end, identity['generation'])
                        break
                    except (ValueError, OSError) as error:
                        if time.monotonic() >= deadline: raise
                        result['visibility_error'] = str(error)
                        await asyncio.sleep(5)
                if request.get('repair'):
                    clear_repaired(state, originals, request['start'], request['end'], result['panel'])
                result.update(state='complete', verified_ticks=count, completed_at=time.time())
                result.pop('visibility_error', None)
            except asyncio.CancelledError:
                result.update(state='interrupted', error='Supervisor stopped; next start replays recent 12h')
                raise
            except Exception as error:
                result.update(state='failed', error=type(error).__name__ + ': ' + str(error)[:400])
            finally:
                if process.returncode is None:
                    process.terminate()
                    try: await asyncio.wait_for(process.wait(), 10)
                    except TimeoutError: process.kill(); await process.wait()
                processes.discard(process)
                atomic_json(state / ('perses-recording-replay-result-' + str(identity['generation']) + '.json'), result)

        live = task = None
        try:
            live, identity = await launch('live', catalog)
            status['live'] = dict(identity, state='running')
            # A new bounded native replay on every supervisor start; older history stays raw.
            task = asyncio.create_task(replay({'id': 'startup-' + identity['publication']}))
            handled_path = state / 'perses-recording-handled.json'
            handled = json.loads(handled_path.read_text()) if handled_path.exists() else []
            while not stop.is_set():
                if live.returncode is not None:
                    raise RuntimeError('vmalert live exited ' + str(live.returncode))
                if task is not None and task.done():
                    task.result()
                    done_id = status['replay']['id']
                    if not done_id.startswith('startup-'):
                        handled.append(done_id); atomic_json(handled_path, handled)
                    task = None
                if task is None:
                    for request in service.admin().get('native_replays', []):
                        if request['id'] not in handled:
                            # Failed requests retain evidence; retry uses a new explicit request ID.
                            task = asyncio.create_task(replay(request))
                            break
                try:
                    response = await service.client.get('http://' + args.listen + '/api/v1/rules', timeout=4)
                    response.raise_for_status()
                    groups = response.json()['data']['groups']
                    rules = [r for group in groups for r in group['rules']]
                    status['live'].update(loaded_rules=len(rules),
                        evaluation_errors=sum(bool(r.get('lastError')) for r in rules), probe_error=None)
                except Exception as error:
                    status['live']['probe_error'] = type(error).__name__ + ': ' + str(error)[:200]
                status['updated_at'] = time.time()
                atomic_json(state / 'perses-recording-runtime.json', status)
                try: await asyncio.wait_for(stop.wait(), 2)
                except TimeoutError: pass
        finally:
            if task:
                task.cancel(); await asyncio.gather(task, return_exceptions=True)
            for process in list(processes):
                if process.returncode is None: process.terminate()
            for process in list(processes):
                try: await asyncio.wait_for(process.wait(), 10)
                except TimeoutError: process.kill(); await process.wait()
            if status['live']: status['live']['state'] = 'stopped'
            status['updated_at'] = time.time()
            atomic_json(state / 'perses-recording-runtime.json', status)
            await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path('/state'))
    parser.add_argument('--catalog', type=Path, default=Path('/monitoring/monitoring/perses_acceleration_catalog.json'))
    parser.add_argument('--vm', default='http://127.0.0.1:18428')
    parser.add_argument('--binary', default='/vmalert-prod')
    parser.add_argument('--listen', default='127.0.0.1:18432')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
