"""Run on test4 via SSH MCP to stop a group or invalidate corrected raw history."""
import argparse
import fcntl
import json
import math
import time
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
    if action in ('parallel-a3-on', 'parallel-a3-off'):
        value['parallel_a3_backfill'] = action == 'parallel-a3-on'
        if action == 'parallel-a3-off': value.pop('backfill_priority_until', None)
    elif action == 'backfill-priority':
        # Explicit temporary maintenance window; completion also ends priority.
        value['backfill_priority_until'] = time.time() + 7200
    elif action == 'backfill':
        # One bounded request, resumed idempotently after worker/process restarts.
        assert backfill_hours == 12, 'Current authorization is 12 hours'
        value.setdefault('backfill', {'seconds': 43200, 'requested_at': time.time()})
        assert value['backfill']['seconds'] in (43200, 86400)
        value['backfill']['seconds'] = 43200
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
    parser.add_argument('action', choices=('enable', 'disable', 'invalidate', 'backfill', 'parallel-a3-on', 'parallel-a3-off', 'backfill-priority'))
    parser.add_argument('--state', type=Path, default=Path('/data2/monitoring/state'))
    parser.add_argument('--group', choices=('cpu', 'dcu', 'a3'))
    parser.add_argument('--start', type=float); parser.add_argument('--end', type=float)
    parser.add_argument('--panel', default='*'); parser.add_argument('--reason')
    args = parser.parse_args()
    assert args.state.is_dir(), 'State directory must already exist'
    print(json.dumps(update(args.state / 'perses-acceleration-admin.json', args.action, args.group,
                            args.start, args.end, args.panel, args.reason), ensure_ascii=False))
