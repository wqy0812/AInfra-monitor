"""Production read-only comparison and timing after CPU publication."""
import json
import math
import statistics
import time
import urllib.parse
import urllib.request

import host_cpu_release as release


def query(expression, start, end, proxy=True, nocache=True):
    params = urllib.parse.urlencode({'query': expression, 'start': start, 'end': end, 'step': 5,
                                    'latency_offset': '1ms', 'nocache': int(nocache)})
    started = time.monotonic()
    if proxy:
        data = release.http('/proxy/projects/a3-monitoring/datasources/victoriametrics/api/v1/query_range?' + params)
    else:
        data = json.load(urllib.request.urlopen('http://127.0.0.1:18428/api/v1/query_range?' + params, timeout=30))
    assert data['status'] == 'success'
    return data['data']['result'], time.monotonic() - started


def normalized(rows):
    return {(row['metric']['node'], float(ts)): float(value) for row in rows for ts, value in row['values']}


def main():
    health, latest = release.api_check()
    count = release.check_resources(after=True)
    before = release.release.read('resources-before.json')[release.IDENTITY]['spec']['panels']
    after = release.http(release.ENDPOINT)['spec']['panels']
    end = int(time.time() // 5) * 5 - 40
    cutover = release.release.read('api-switched.json')['health']['environments']['a3-vllm']['processed_at']
    start = cutover + 5
    assert end - start >= 30, 'Wait for at least 30 seconds of stable materialized history'
    report = {'passed': False, 'at': time.time(), 'cutover': cutover, 'comparison_range': [start, end],
              'resources_checked': count, 'health': health, 'panels': {}}
    for key in release.PANELS:
        expressions = [panels[key]['spec']['queries'][0]['spec']['plugin']['spec']['query'].replace('$node', '.*').replace('$__interval', '5s')
                       for panels in (before, after)]
        old, _ = query(expressions[0], start, end)
        new, _ = query(expressions[1], start, end)
        a, b = normalized(old), normalized(new)
        assert a.keys() == b.keys(), (key, len(a), len(b), list(a.keys() - b.keys())[:3])
        assert len(a) >= 12
        max_error = max(abs(a[k] - b[k]) for k in a)
        assert all(math.isclose(a[k], b[k], rel_tol=1e-9, abs_tol=1e-8) for k in a), (key, max_error)
        measurements = []
        for i in range(5):
            # Alternate order. Same 1h requested range, but materialized history
            # exists only after cutover; report this population difference.
            pair = {}
            for index in ([0, 1] if i % 2 == 0 else [1, 0]):
                rows, elapsed = query(expressions[index], end - 3600, end)
                pair['old' if index == 0 else 'new'] = {'seconds': round(elapsed, 6), 'points': len(normalized(rows))}
            measurements.append(pair)
        filters = {}
        for node in ('a3-1', 'a3-2'):
            q = after[key]['spec']['queries'][0]['spec']['plugin']['spec']['query'].replace('$node', node).replace('$__interval', '5s')
            rows, _ = query(q, start, end)
            assert len(rows) == 1 and rows[0]['metric']['node'] == node
            filters[node] = len(rows[0]['values'])
        report['panels'][key] = {'matched_points': len(a), 'max_absolute_error': max_error,
            'median_old_seconds': statistics.median(x['old']['seconds'] for x in measurements),
            'median_new_seconds': statistics.median(x['new']['seconds'] for x in measurements),
            'timing_samples': measurements, 'filtered_points': filters,
            'timing_note': 'same 1h requested range; new data only since cutover, no backfill'}
    histories = {}
    for env in ('dcu-pd', 'a3-vllm'):
        for hours in (1, 24, 720):
            started = time.monotonic()
            data = release.release.get('/api/monitoring/history?environment=' + env + '&hours=' + str(hours))
            assert data['environment'] == env and len(data['points']) > 0
            histories[env + '/' + str(hours)] = {'points': len(data['points']), 'seconds': round(time.monotonic() - started, 3)}
            if env == 'a3-vllm' and hours == 1:
                for role in ('prefill', 'decode'):
                    for field in ('cpu', 'cpu_iowait'):
                        assert any(p['nodes'][role].get(field) is not None for p in data['points']), (role, field)
    report.update(passed=True, histories=histories, latest_host_cpu={r: n['host_cpu'] for r, n in latest['nodes'].items()})
    release.release.save('acceptance.json', report)
    print(json.dumps({'passed': True, 'comparison_range': [start, end],
        'panels': {k: {x: v[x] for x in ('matched_points', 'max_absolute_error', 'median_old_seconds', 'median_new_seconds')} for k, v in report['panels'].items()},
        'histories': histories}))


if __name__ == '__main__':
    main()
