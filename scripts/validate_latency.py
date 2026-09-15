"""Read-only candidate/production latency validation, run inside candidate image."""
import argparse
import asyncio
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from monitoring.api import Service, get
from monitoring.latency import PATHS
from monitoring.latency_rebuild import VM, replay_latency


def fetch(base, path, params=None):
    url = base + path + ('?' + urllib.parse.urlencode(params) if params else '')
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=20) as r:
        return json.load(r)


async def validate(root, production):
    vm = VM('http://127.0.0.1:18428')
    s = Service()
    report = {'passed': False, 'at': time.time(), 'production': production, 'checks': []}
    hist = json.loads((root / 'historical.json').read_text())
    assert hist['watermark'] == hist['end']
    assert hist['points'] == (hist['end'] - hist['start']) // 5 + 1
    async def history(start, end, hours=1, env='dcu-pd'):
        if production:
            return await asyncio.to_thread(fetch, 'http://127.0.0.1:18430', '/api/monitoring/history', {'hours': hours, 'start': start, 'end': end, 'environment': env})
        service = s if env == 'dcu-pd' else Service(env)
        try:
            return await asyncio.wait_for(service.history(hours, start, end), 8)
        finally:
            if service is not s: await service.client.aclose()
    try:
        # Every 5-second timestamp in the full rebuild, independent of valid/invalid status.
        q = 'count_over_time(monitoring_chart_valid{environment="dcu-pd",schema="latency-v2"}[' + str(hist['end'] - hist['start'] + 5) + 's])'
        data = json.loads(vm.request('/api/v1/query', {'query': q, 'time': hist['end']}))['data']['result']
        assert len(data) == len(PATHS)
        assert all(float(r['value'][1]) == hist['points'] for r in data), data
        report['checks'].append({'full_history_paths': len(data), 'ticks': hist['points'], 'start': hist['start'], 'end': hist['end']})
        for start, end in [(1789379970, 1789380030), (hist['start'], hist['start'] + 180), (hist['end'] - 180, hist['end'])]:
            points = (await history(start, end))['points']
            expected = {p['ts']: p for p in replay_latency(vm.raw(start - 80, end), start - 80, end) if p['ts'] >= start}
            comparisons = 0
            for p in points:
                for path in PATHS:
                    a, b = get(p, path), get(expected[p['ts']], path)
                    assert a == b or (a is not None and b is not None and math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-8)), (p['ts'], path, a, b)
                    comparisons += 1
            if start == 1789379970:
                p = next(p for p in points if p['ts'] == 1789379990)
                assert p['nodes']['decode']['percentiles']['e2e'] == {'samples': 2, 'p50': 6, 'p95': 98, 'p99': 99.6}
            report['checks'].append({'same_raw_window': [start, end], 'field_comparisons': comparisons})
        end = int((time.time() - 30) // 5) * 5
        for hours in (1, 6, 24, 720):
            t = time.monotonic()
            result = await history(end - hours * 3600, end, hours)
            report['checks'].append({'history_hours': hours, 'seconds': time.monotonic() - t, 'points': len(result['points'])})
            assert result['points'] and len(result['points']) <= 721
        a3 = await history(end - 300, end, env='a3-vllm')
        assert a3['environment'] == 'a3-vllm' and a3['points']
        report['checks'].append({'a3_history_available': True})
        if production:
            # Exact dashboard expression through Perses and direct VM.
            dash = fetch('http://122.247.53.162:18431', '/api/v1/projects/dcu-monitoring/dashboards/overview')
            count = 0
            for panel in dash['spec']['panels'].values():
                for query in panel['spec'].get('queries', []):
                    expr = query['spec']['plugin']['spec'].get('query', '')
                    if '.percentiles.' not in expr: continue
                    assert 'schema="latency-v2"' in expr and 'schema="v1"' not in expr
                    expr = expr.replace('$role', 'decode').replace('$__interval', '5s')
                    params = {'query': expr, 'time': 1789379990}
                    direct = fetch('http://127.0.0.1:18428', '/api/v1/query', params)['data']
                    proxied = fetch('http://122.247.53.162:18431/proxy/projects/dcu-monitoring/datasources/victoriametrics', '/api/v1/query', params)['data']
                    assert direct == proxied
                    count += 1
            assert count == 9
            report['checks'].append({'perses_queries_match_vm': count})
        report['passed'] = True
    finally:
        await s.client.aclose()
        (root / ('production-acceptance.json' if production else 'candidate-acceptance.json')).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--production', action='store_true')
    args = p.parse_args()
    asyncio.run(validate(args.root, args.production))
