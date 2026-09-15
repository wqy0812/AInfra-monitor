"""Resumable, single-writer rebuild of streaming latency, never deleting existing data."""
import argparse
import collections
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .api import encode
from .latency import LATENCIES, LatencyWindow
from .replay import decode_export, latest_rows

SELECTOR = '{environment="dcu-pd",job=~"sglang-prefill|sglang-decode",__name__=~"up|sglang:(time_to_first_token_seconds|inter_token_latency_seconds|e2e_request_latency_seconds)_(bucket|count)"}'


def replay_latency(groups, start, end):
    windows = {role: LatencyWindow() for role in ('prefill', 'decode')}
    previous = {}
    points = []
    for tick in range(int(start // 5) * 5, int(end // 5) * 5 + 1, 5):
        point = {'ts': tick, 'nodes': {}}
        for role in windows:
            ts, rows = latest_rows(groups, 'sglang-' + role, tick)
            if ts is None:
                windows[role].clear()
                previous.pop(role, None)
                data = {'latency_quality': dict.fromkeys(LATENCIES, 'source_unavailable')}
            elif previous.get(role, (None,))[0] == ts:
                data = previous[role][1]
            else:
                data = windows[role].add(rows, ts)
                previous[role] = (ts, data)
            point['nodes'][role] = data
        points.append(point)
    return points


class VM:
    def __init__(self, url):
        self.url = url.rstrip('/')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, params=None, body=None):
        url = self.url + path + ('?' + urllib.parse.urlencode(params) if params else '')
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'text/plain'} if body is not None else {})
        with self.opener.open(req, timeout=20) as response:
            return response.read().decode()

    def earliest(self, end):
        q = 'min(tfirst_over_time({environment="dcu-pd",job=~"sglang-prefill|sglang-decode",__name__=~"sglang:(time_to_first_token_seconds|inter_token_latency_seconds|e2e_request_latency_seconds)_count"}[30d]))'
        data = json.loads(self.request('/api/v1/query', {'query': q, 'time': end}))
        values = data['data']['result']
        if not values:
            raise ValueError('No retained DCU latency observations')
        return max(end - 30 * 86400, int(float(values[0]['value'][1]) // 5) * 5)

    def raw(self, start, end):
        text = self.request('/api/v1/export', {'match[]': SELECTOR, 'start': start, 'end': end, 'reduce_mem_usage': 1})
        return decode_export(json.loads(line) for line in text.splitlines() if line)

    def write(self, points):
        self.request('/api/v1/import/prometheus', body=encode(points, latency_only=True).encode())


def fingerprint():
    root = Path(__file__).parent
    return hashlib.sha256(b''.join((root / p).read_bytes() for p in ('request_scope.py', 'latency.py', 'replay.py', 'latency_rebuild.py', 'api.py'))).hexdigest()


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, sort_keys=True, indent=2) + '\n')
    temp.replace(path)


def batch(vm, state, until):
    before = state['watermark']
    points = replay_latency(vm.raw(before - 80, until), before - 80, until)
    points = [p for p in points if before < p['ts'] <= until]
    expected = list(range(before + 5, until + 1, 5))
    assert [p['ts'] for p in points] == expected
    vm.write(points)  # Persist cursor and summary only after acknowledged import.
    result = dict(state)
    counts = collections.Counter(state.get('quality', {}))
    for p in points:
        for role, node in p['nodes'].items():
            for kind in LATENCIES:
                counts[role + '.' + kind + '.' + node['latency_quality'][kind]] += 1
    result.update(watermark=until, points=state.get('points', 0) + len(points),
                  batches=state.get('batches', 0) + 1, quality=dict(counts), updated_at=time.time())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', default='http://127.0.0.1:18428')
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--start', type=int)
    parser.add_argument('--end', type=int)
    parser.add_argument('--follow', action='store_true', help='Separate live continuation from a completed fixed historical cutoff')
    args = parser.parse_args()
    vm = VM(args.vm)
    # One process per checkpoint, including retries and live continuation.
    import fcntl
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.state.exists():
            state = json.loads(args.state.read_text())
            assert state['fingerprint'] == fingerprint(), 'Code changed: use a new checkpoint and review overlapping output'
            assert state['vm'] == args.vm
            if args.end is not None: assert state['end'] == args.end
            if args.start is not None: assert state['start'] == args.start
        else:
            end = args.end if args.end is not None else int((time.time() - 15) // 5) * 5
            start = args.start if args.start is not None else vm.earliest(end)
            assert start % 5 == end % 5 == 0 and start <= end
            state = dict(vm=args.vm, start=start, end=end, watermark=start - 5, points=0, batches=0, quality={}, fingerprint=fingerprint())
            save(args.state, state)
        while True:
            until = min(state['end'], state['watermark'] + 300)
            if until <= state['watermark']:
                if not args.follow:
                    print(json.dumps({'complete': True, **state}), flush=True)
                    return
                # Historical end remains immutable; live ownership uses a separate state file.
                assert args.start is not None, '--follow requires an explicit live start'
                until = min(int((time.time() - 15) // 5) * 5, state['watermark'] + 300)
                if until <= state['watermark']:
                    time.sleep(5)
                    continue
            state = batch(vm, state, until)
            save(args.state, state)
            if state['batches'] % 20 == 0:
                print(json.dumps({k: state[k] for k in ('watermark', 'end', 'points', 'batches')}), flush=True)


if __name__ == '__main__':
    main()
