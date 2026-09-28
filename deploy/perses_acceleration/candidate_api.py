"""Candidate-container read-only checks against real VM; no loops or imports."""
import asyncio
import copy
import json
import math
import tempfile
import time
import sys
from pathlib import Path

sys.path.insert(0, '/monitoring')
from monitoring.api import Service, summary_point, VM
from monitoring.perses_acceleration import AccelerationService


async def main():
    end = int((time.time() - 120) // 60) * 60
    report = {}
    for env in ('dcu-pd', 'a3-vllm', 'xpu-pd'):
        service = Service(env)
        try:
            full = await service.history(1, end - 3600, end)
            reduced = await service.history(1, end - 3600, end, view='summary')
            assert full['points'] and reduced == {**full, 'points': [summary_point(copy.deepcopy(p)) for p in full['points']]}
            report[env] = {'history_points': len(full['points']), 'full_summary_match': True}
        finally:
            await service.close()
    catalog = json.loads(Path('/monitoring/monitoring/perses_acceleration_catalog.json').read_text())
    accelerator = AccelerationService(VM, tempfile.mkdtemp(prefix='perses-candidate-'), catalog)
    checks = []
    try:
        # Read existing acceleration coverage without starting a worker. The
        # unsupported timeout parameter independently verifies whole-query fallback.
        for panel in catalog['panels']:
            expr = accelerator.expression(panel, 60)
            pairs = [('query', expr), ('start', str(end - 60)), ('end', str(end)), ('step', '60'), ('nocache', '1')]
            raw = await accelerator.forward('POST', 'api/v1/query_range', pairs)
            actual = await accelerator.request('POST', 'api/v1/query_range', pairs)
            assert raw[0] == actual[0] == 200
            def samples(response):
                return {json.dumps(row['metric'], sort_keys=True): {t: float(v) for t, v in row['values']}
                        for row in json.loads(response[1])['data']['result']}
            def compare(left, right):
                assert left.keys() == right.keys(), (panel['id'], 'labels')
                for key in left:
                    assert left[key].keys() == right[key].keys(), (panel['id'], 'timestamps')
                    assert all(math.isclose(v, right[key][t], rel_tol=1e-9, abs_tol=1e-10) for t, v in left[key].items()), (panel['id'], 'values')
            left, right = samples(raw), samples(actual)
            compare(left, right)
            checks.append(panel['id'])
            fast = accelerator.counters['fast_requests']
            fallback = await accelerator.request('POST', 'api/v1/query_range', pairs + [('timeout', '10s')])
            assert fallback[0] == 200
            compare(left, samples(fallback))
            assert accelerator.counters['fast_requests'] == fast
    finally:
        await accelerator.close()
    print(json.dumps({'passed': True, 'production_imports': 0, 'environments': report,
                      'compatible_query_checks': checks, 'raw_fallback_checks': checks,
                      'existing_coverage_hits': accelerator.counters['fast_requests']}))


asyncio.run(main())
