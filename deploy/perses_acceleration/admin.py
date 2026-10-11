"""Run on test4 via SSH MCP to stop a group or invalidate corrected raw history."""
import argparse
import fcntl
import json
import math
import time
import uuid
from pathlib import Path


def update(path, action, group=None, start=None, end=None, panel='*', reason=None, backfill_hours=12):
    # The watcher and an operator invalidating repaired history may update at
    # the same time. Keep both changes instead of losing one atomic replacement.
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _update(path, action, group, start, end, panel, reason, backfill_hours)


def _update(path, action, group=None, start=None, end=None, panel='*', reason=None, backfill_hours=12):
    value = json.loads(path.read_text()) if path.exists() else {'disabled_groups': [], 'invalidated': []}
    assert isinstance(value['disabled_groups'], list) and isinstance(value['invalidated'], list)
    if action in ('parallel-a3-on', 'parallel-a3-off', 'backfill-priority'):
        raise ValueError('Legacy worker scheduling has been retired; use native backfill')
    elif action in ('backfill', 'repair'):
        if backfill_hours != 12:
            raise ValueError('Native replay is bounded to 12 hours per request')
        if (start is None) != (end is None):
            raise ValueError('Supply both replay boundaries')
        if action == 'repair' and start is None:
            raise ValueError('Repair requires explicit boundaries')
        if start is not None and (not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end))
                or start > end or end - start > 43200 or end > time.time() - 60):
            raise ValueError('Replay must be sealed and bounded to 12 hours')
        request = {'id': uuid.uuid4().hex, 'requested_at': time.time(), 'panel': panel,
                   'repair': action == 'repair'}
        if start is not None: request.update(start=start, end=end)
        value.setdefault('native_replays', []).append(request)
    elif action == 'invalidate':
        assert all(isinstance(x, (float, int)) and math.isfinite(x) for x in (start, end)) and start <= end
        assert reason and panel
        value['invalidated'].append({'start': start, 'end': end, 'panel': panel, 'reason': reason, 'at': time.time()})
    else:
        assert group in ('cpu', 'dcu', 'a3')
        disabled = set(value['disabled_groups'])
        if action == 'disable': disabled.add(group)
        elif action == 'enable': disabled.discard(group)
        else: raise ValueError('Unknown action')
        value['disabled_groups'] = sorted(disabled)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)
    assert json.loads(path.read_text()) == value
    return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('enable', 'disable', 'invalidate', 'backfill', 'repair', 'parallel-a3-on', 'parallel-a3-off', 'backfill-priority'))
    parser.add_argument('--state', type=Path, default=Path('/data2/monitoring/state'))
    parser.add_argument('--group', choices=('cpu', 'dcu', 'a3'))
    parser.add_argument('--start', type=float); parser.add_argument('--end', type=float)
    parser.add_argument('--panel', default='*'); parser.add_argument('--reason')
    args = parser.parse_args()
    assert args.state.is_dir(), 'State directory must already exist'
    print(json.dumps(update(args.state / 'perses-acceleration-admin.json', args.action, args.group,
                            args.start, args.end, args.panel, args.reason), ensure_ascii=False))
