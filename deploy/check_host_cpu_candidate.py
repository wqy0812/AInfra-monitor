"""Exercise the candidate against live reads, intercepting every VM import."""
import asyncio
import json
import time
from pathlib import Path

import httpx

from monitoring import api, host_cpu


async def check():
    baseline = json.loads(Path('/release-checks/resources-before.json').read_text())
    panels = baseline['a3-monitoring/dashboards/a3-hosts']['spec']['panels']
    for field, key, expression in [('cpu', 'p0', host_cpu.expression()),
                                    ('cpu_iowait', 'extra-iowait', host_cpu.iowait_expression())]:
        old = panels[key]['spec']['queries'][0]['spec']['plugin']['spec']['query']
        assert expression == old.replace('$node', '.*').replace('$__interval', '5s'), field
    report = {}
    for environment in api.ENVIRONMENTS:
        service = api.Service(environment)
        imports = []

        async def intercept(url, **kwargs):
            assert url == api.VM + '/api/v1/import/prometheus'
            imports.append(kwargs['content'])
            return httpx.Response(204, request=httpx.Request('POST', url))

        service.client.post = intercept
        try:
            started = time.monotonic()
            await service.cycle()
            elapsed = time.monotonic() - started
            assert len(imports) == 1
            assert all(n['metrics']['status'] == 'ok' for n in service.latest['nodes'].values())
            if environment == 'a3-vllm':
                assert service.host_cpu_status == 'ok'
                for role, node in service.latest['nodes'].items():
                    assert node['host_cpu']['status'] == 'ok', node['host_cpu']
                    assert all(service.latest_point['nodes'][role][field] is not None for field in host_cpu.FIELDS)
                assert sum(line.startswith('monitoring_chart_valid') and 'schema="host-cpu-v1"' in line and '} 1 ' in line
                           for line in imports[0].splitlines()) == 4
            else:
                assert 'host-cpu-v1' not in imports[0]
            report[environment] = {'cycle_seconds': round(elapsed, 3), 'computed_at': service.watermark,
                'imports_intercepted': len(imports),
                'host_cpu': {r: n.get('host_cpu') for r, n in service.latest['nodes'].items()}}
        finally:
            await service.client.aclose()
    print(json.dumps({'passed': True, 'production_imports': 0, 'environments': report}))


if __name__ == '__main__':
    asyncio.run(check())
