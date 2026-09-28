"""Independent raw-counter reconciliation, executed on test4 via SSH MCP."""
import bisect
import gzip
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(path, params):
    with OPENER.open('http://127.0.0.1:18428' + path + '?' + urllib.parse.urlencode(params, doseq=True), timeout=40) as response:
        return response.read()


def run(minimum=600):
    marker = json.loads((ROOT / 'ready.json').read_text())['marker']
    start = int(marker['repair_complete_at'] + 70)
    end = int((time.time() - 20) // 5) * 5
    assert end - start >= minimum, 'Insufficient post-repair observation window'
    names = 'up|vllm:(generation_tokens_total|request_success_total|num_requests_running|num_requests_waiting|kv_cache_usage_perc|time_to_first_token_seconds_count)'
    selectors = ['{environment="a3-vllm",job="vllm-a3",__name__=~"' + names + '"}',
                 '{__name__="sglang:realtime_tokens_total",mode="decode",job="sglang-decode"}',
                 '{__name__=~"monitoring_chart_value|monitoring_chart_valid",environment=~"a3-vllm|dcu-pd|xpu-pd",path=~"nodes.(prefill|decode).(decode_tokens|requests|percentiles.ttft.samples|resources.(queue|kv_usage)..*)"}']
    raw = fetch('/api/v1/export', {'match[]': selectors, 'start': start - 80, 'end': end, 'reduce_mem_usage': 1})
    with gzip.open(ROOT / 'audit-raw.jsonl.gz', 'wb') as file:file.write(raw)
    series = {}
    for line in raw.splitlines():
        obj = json.loads(line); key = json.dumps(obj['metric'], sort_keys=True)
        entry = series.setdefault(key, {'metric': obj['metric'], 'points': {}})
        for ts, value in zip(obj['timestamps'], obj['values']):
            assert ts not in entry['points'] or entry['points'][ts] == value
            entry['points'][ts] = value
    for entry in series.values():entry['times'] = sorted(entry['points'])
    entries = list(series.values())
    def at(entry, tick):
        times = entry['times']; i = bisect.bisect_right(times, tick * 1000) - 1
        if i < 0 or not 0 <= tick * 1000 - times[i] < 10000:return None
        return i
    def rate(entry, tick, window=False):
        i = at(entry, tick)
        if i is None or i < 1:return None
        times = entry['times']; values = entry['points']
        if window:
            eligible = [j for j in range(i) if 55000 <= times[i] - times[j] <= 65000]
            if not eligible:return None
            j = min(eligible, key=lambda j: abs(times[i] - times[j] - 60000))
        else:j = i - 1
        if any(times[k] - times[k-1] >= 10000 or values[times[k]] < values[times[k-1]] for k in range(j+1, i+1)):return None
        delta = values[times[i]] - values[times[j]]
        return delta if window else delta / ((times[i] - times[j]) / 1000)
    def check(env, path, sources, kind, expected_engines):
        actual = next(e for e in entries if e['metric'].get('__name__') == 'monitoring_chart_value' and e['metric'].get('environment') == env and e['metric'].get('path') == path)
        valid = next(e for e in entries if e['metric'].get('__name__') == 'monitoring_chart_valid' and e['metric'].get('environment') == env and e['metric'].get('path') == path)
        assert len({e['metric'].get('engine', e['metric'].get('dp_rank')) for e in sources}) == expected_engines, (env, path, 'coverage')
        errors = []; reused = []; count = 0; absent = 0; observed = []
        for ts, value in sorted(actual['points'].items()):
            tick = ts / 1000
            if not start <= tick <= end:continue
            if valid['points'].get(ts) != 1:absent += 1; continue
            if kind == 'gauge':
                values = [e['points'][e['times'][at(e, tick)]] if at(e,tick) is not None else None for e in sources]
            else:values = [rate(e, tick, kind == 'samples') for e in sources]
            assert all(v is not None for v in values), (env, path, tick, 'source gap')
            expected = sum(values); error = abs(value - expected)
            if error > 1e-6:
                mismatch = {'tick': tick, 'actual': value, 'expected': expected}
                # SGLang materialization can reuse the previous still-fresh source
                # observation. Preserve and report this separately, never call it
                # exact equality or allow the exception for the repaired A3 path.
                previous = [rate(e, tick - 5) for e in sources] if env == 'xpu-pd' and kind == 'rate' else []
                baseline_path = ROOT / 'xpu-baseline-check.json'
                baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
                known = any(abs(x['actual'] - x.get('prior', float('inf'))) < 1e-6 for x in baseline.get('mismatches', []))
                if known and previous and all(v is not None for v in previous) and abs(sum(previous) - value) < 1e-6:
                    reused.append(mismatch)
                else:errors.append(mismatch)
            observed.append(value); count += 1
        assert count > 0
        return {'environment': env, 'path': path, 'compared': count, 'invalid_points': absent,
                'max_error': max([abs(x['actual']-x['expected']) for x in errors + reused] or [0]),
                'unexpected_max_error': max([abs(x['actual']-x['expected']) for x in errors] or [0]),
                'mismatches': errors[:5], 'previous_observation_reuse': reused,
                'mean': sum(observed)/count, 'max': max(observed)}
    results = []
    for role, count in [('prefill',4),('decode',16)]:
        node = 'a3-1' if role == 'prefill' else 'a3-2'
        native = [e for e in entries if e['metric'].get('node') == node and e['metric'].get('__name__','').startswith('vllm:')]
        for metric, path, kind in [('generation_tokens_total','decode_tokens','rate'), ('request_success_total','requests','rate'), ('time_to_first_token_seconds_count','percentiles.ttft.samples','samples')]:
            sources = [e for e in native if e['metric']['__name__'] == 'vllm:' + metric]
            results.append(check('a3-vllm', 'nodes.' + role + '.' + path, sources, kind, count))
        for metric, group, suffix in [('num_requests_running','queue','running'),('num_requests_waiting','queue','waiting'),('kv_cache_usage_perc','kv_usage','ratio')]:
            for engine in range(count):
                sources = [e for e in native if e['metric']['__name__'] == 'vllm:' + metric and e['metric']['engine'] == str(engine)]
                results.append(check('a3-vllm', 'nodes.' + role + '.resources.' + group + '.engine' + str(engine) + '_' + suffix, sources, 'gauge', 1))
    for env in ('dcu-pd','xpu-pd'):
        sources = [e for e in entries if e['metric'].get('environment') == env and e['metric']['__name__'] == 'sglang:realtime_tokens_total']
        results.append(check(env, 'nodes.decode.decode_tokens', sources, 'rate', 8))
    report = {'start': start, 'end': end, 'duration_seconds': end-start,
              'passed': all(not r['mismatches'] and r['invalid_points']==0 for r in results),
              'exact_all_points': all(not r['mismatches'] and not r['previous_observation_reuse'] for r in results),
              'checks': results}
    (ROOT / 'counter-audit.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'passed': report['passed'], 'duration_seconds':end-start, 'checks':len(results),
                     'compared':sum(r['compared'] for r in results), 'failures':[r for r in results if r['mismatches'] or r['invalid_points']],
                     'throughput':[r for r in results if r['path'].endswith('.decode_tokens')]}))
    assert report['passed']


if __name__ == '__main__':run(int(sys.argv[1]) if len(sys.argv)>1 else 600)
