"""Read-only diagnostic, not admission: native vs installed vs candidate queries."""
import asyncio
import importlib.util
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, '/monitoring')
from monitoring.perses_acceleration import AccelerationService


def samples(response):
    assert response[0] == 200
    return {json.dumps(row['metric'], sort_keys=True): {t: float(v) for t, v in row['values']}
            for row in json.loads(response[1])['data']['result']}


def equal(left, right):
    assert left.keys() == right.keys(), 'Labels differ'
    for key, values in left.items():
        assert values.keys() == right[key].keys(), 'Timestamps differ'
        assert all(a == right[key][t] or math.isclose(a, right[key][t], rel_tol=1e-9, abs_tol=1e-10)
                   for t, a in values.items()), 'Values differ'


async def main():
    spec = importlib.util.spec_from_file_location('candidate_core', '/candidate/candidate_core.py')
    candidate = importlib.util.module_from_spec(spec); spec.loader.exec_module(candidate)
    catalog = json.loads(Path('/monitoring/monitoring/perses_acceleration_catalog.json').read_text())
    old = AccelerationService('http://127.0.0.1:18428', '/state', catalog)
    new = candidate.AccelerationService('http://127.0.0.1:18428', '/state', catalog)
    selected = {'dcu-monitoring/hosts-dcu/core-p0', 'xpu-monitoring/hosts-xpu/core-extra-iowait',
                'dcu-monitoring/backend-prefill/bn-prefill-per_stage_req_latency_seconds',
                'a3-monitoring/backend-performance/extra-request_inference_time_seconds'}
    report = {'diagnostic_only': True, 'performance_admission': False, 'production_imports': 0,
              'started_at': time.time(), 'rows': []}
    try:
        for panel in catalog['panels']:
            if panel['id'] not in selected: continue
            for step in (60, 120):
                end = int((time.time() - 600) // step) * step
                for nocache in ('1', '0'):
                    pairs = [('query', old.expression(panel, step)), ('start', str(end - 43200)),
                             ('end', str(end)), ('step', str(step)), ('nocache', nocache)]
                    async def run(version):
                        if version == 0: return await old.forward('POST', 'api/v1/query_range', pairs)
                        return await (old if version == 1 else new).request('POST', 'api/v1/query_range', pairs)
                    for version in range(3): await run(version)
                    times = [[], [], []]
                    for iteration in range(5):
                        results = {}
                        for version in ([0, 1, 2] if iteration % 2 else [2, 1, 0]):
                            begin = time.perf_counter(); result = await run(version)
                            times[version].append(time.perf_counter() - begin); results[version] = samples(result)
                        equal(results[0], results[1]); equal(results[0], results[2])
                    row = {'panel': panel['id'], 'step': step, 'nocache': nocache, 'pairs': 5,
                           'native_median': statistics.median(times[0]), 'installed_median': statistics.median(times[1]),
                           'candidate_median': statistics.median(times[2]), 'equal': True}
                    report['rows'].append(row)
                    print(json.dumps(row), flush=True)
        report.update(ended_at=time.time(), passed=True)
        print(json.dumps(report), flush=True)
    finally:
        await old.close(); await new.close()


asyncio.run(main())
