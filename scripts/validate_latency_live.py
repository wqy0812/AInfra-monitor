"""Verify two complete live windows and exact timestamp coverage at handoff."""
import json
import time
import urllib.request
from pathlib import Path
from monitoring.latency_rebuild import VM, replay_latency

root = Path('/evidence')
vm = VM('http://127.0.0.1:18428')
report = {'passed': False, 'samples': []}
start = time.monotonic()
try:
    while True:
        health = json.load(urllib.request.urlopen('http://127.0.0.1:18430/health', timeout=10))
        assert health['status'] == 'ok', health
        live = json.load(urllib.request.urlopen('http://127.0.0.1:18430/api/monitoring/latest', timeout=10))
        ts = live['ts']
        age = time.time() - ts
        assert age < 20
        if not report['samples'] or ts != report['samples'][-1]['ts']:
            point = replay_latency(vm.raw(ts - 80, ts), ts - 80, ts)[-1]
            for role, node in live['nodes'].items():
                actual = node['metrics']['data']['percentiles']
                expected = point['nodes'][role]['percentiles']
                assert actual == expected, (ts, role, actual, expected)
            report['samples'].append({'ts': ts, 'age': age, 'health': health['status'], 'decode': live['nodes']['decode']['metrics']['data']['percentiles']})
        if time.monotonic() - start >= 135:
            break
        time.sleep(5)
    report['start'] = report['samples'][0]['ts']
    report['end'] = report['samples'][-1]['ts']
    assert report['end'] - report['start'] >= 130
    handoff = json.loads((root / 'handoff.json').read_text())['ts']
    selector = '{environment="dcu-pd",schema="latency-v2",__name__=~"monitoring_chart_(valid|value)"}'
    raw = vm.request('/api/v1/export', {'match[]': selector, 'start': handoff - 30, 'end': handoff + 30})
    series = {}
    for line in raw.splitlines():
        row = json.loads(line)
        key = row['metric']['__name__'] + ':' + row['metric']['path']
        values = series.setdefault(key, {})
        for ts, value in zip(row['timestamps'], row['values']):
            assert ts not in values or values[ts] == value, 'Conflicting handoff duplicate'
            values[ts] = value
    expected = set(range((handoff - 30) * 1000, (handoff + 30) * 1000 + 1, 5000))
    assert len(series) == 52 and all(set(values) == expected for values in series.values()), 'Handoff gap'
    report.update(passed=True, handoff=handoff, handoff_paths_without_gaps=26)
except BaseException as error:
    report['error'] = repr(error)
    raise
finally:
    (root / 'live-window-acceptance.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'passed': True, 'duration_seconds': report['end'] - report['start'], 'samples': len(report['samples']), 'max_age_seconds': max(s['age'] for s in report['samples']), 'handoff_paths_without_gaps': 26}))
