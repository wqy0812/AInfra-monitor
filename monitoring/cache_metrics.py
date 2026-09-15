"""Cache tier attribution and capacity; counters never substitute for missing data."""
import collections
import json
import math

MODES = ('device_hit', 'host_hit', 'storage_hit', 'input')
SEMANTICS = 'prefill-effective-v1'


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def identity(labels):
    return json.dumps(labels, sort_keys=True, separators=(',', ':'))


def rank_key(labels):
    return tuple(int(labels.get(k, 0)) for k in ('pp_rank', 'tp_rank', 'moe_ep_rank'))


def effective_counters(rows):
    schedulers = {}
    for row in rows:
        if row['name'] == 'sglang:num_running_reqs' and row['labels'].get('engine_type') == 'prefill':
            labels = {k: v for k, v in row['labels'].items() if k != 'priority'}
            schedulers[identity(labels)] = labels
    values = {}
    for row in rows:
        if row['name'] != 'sglang:prefill_effective_tokens_total' or row['labels'].get('engine_type') != 'prefill':
            continue
        labels = {k: v for k, v in row['labels'].items() if k not in ('mode', 'priority')}
        key, mode = identity(labels), row['labels'].get('mode')
        if mode not in MODES or not finite(row['value']) or mode in values.get(key, {}):
            return None
        values.setdefault(key, {})[mode] = row['value']
    if not schedulers or not values or not set(values).issubset(schedulers):
        return None
    if len({x.get('model_name') for x in schedulers.values()}) != 1:
        return None
    groups = collections.defaultdict(list)
    for labels in schedulers.values():
        groups[labels.get('dp_rank', '')].append(labels)
    if '' in groups and len(groups) > 1:
        return None
    selected = {}
    try:
        for group in groups.values():
            key = identity(min(group, key=rank_key))
            if set(values.get(key, {})) != set(MODES):
                return None
            selected[key] = values[key]
    except (ValueError, TypeError):
        return None
    return {'topology': sorted(schedulers), 'groups': selected}


def capacity(used, total):
    reason = None
    if not finite(used) or not finite(total):
        reason = '容量指标缺失'
    elif total == 0:
        reason = '总容量为零'
    elif used > total:
        reason = '已用容量超过总容量，等待一致观测'
    return {'used': used, 'total': total, 'ratio': used / total if reason is None else None, 'reason': reason}


def host_capacity(rows):
    pools = {}
    for row in rows:
        if row['name'] not in ('sglang:hicache_host_used_tokens', 'sglang:hicache_host_total_tokens'):
            continue
        if row['labels'].get('engine_type') != 'prefill':
            continue
        labels = row['labels']
        item = pools.setdefault(identity(labels), {'labels': labels})
        field = 'used' if row['name'].endswith('used_tokens') else 'total'
        item[field] = row['value'] if field not in item else None
    ranks = []
    for key in sorted(pools):
        item = pools[key]
        ranks.append({'labels': item['labels'], **capacity(item.get('used'), item.get('total'))})
    try:
        representative = min(ranks, key=lambda x: (int(x['labels'].get('dp_rank', 0)), rank_key(x['labels']))) if ranks else None
    except (ValueError, TypeError):
        representative = None
    return {'representative': representative, 'ranks': ranks,
            'reason': None if representative else 'HiCache 容量指标缺失'}


def store_metrics(rows):
    def single(name):
        found = [x['value'] for x in rows if x['name'] == name and not x['labels']]
        return found[0] if len(found) == 1 and finite(found[0]) else None
    segments = {}
    for row in rows:
        if row['name'] not in ('segment_allocated_bytes', 'segment_total_capacity_bytes'):
            continue
        label = row['labels'].get('segment')
        if not label:
            continue
        item = segments.setdefault(label, {})
        field = 'used' if row['name'] == 'segment_allocated_bytes' else 'total'
        item[field] = row['value'] if field not in item else None
    return {'capacity': capacity(single('master_allocated_bytes'), single('master_total_capacity_bytes')),
            'ssd_capacity': capacity(single('master_allocated_file_size_bytes'), single('master_total_file_capacity_bytes')),
            'tier_query_counters': {'memory': single('mem_cache_hit_nums_'), 'ssd': single('file_cache_hit_nums_'),
                                    'valid': single('valid_get_nums_'), 'total': single('total_get_nums_')},
            'segments': [{'segment': key, **capacity(value.get('used'), value.get('total'))} for key, value in sorted(segments.items())],
            'query_counters': {'valid': single('valid_get_nums_'), 'total': single('total_get_nums_')}}


class DeltaWindow:
    """A 55–65 second counter window, reset on gaps or any identity change."""
    def __init__(self):
        self.points = collections.deque()

    def add(self, ts, counters, topology=None):
        if not counters or not all(finite(x) for x in counters.values()) or not finite(ts):
            self.points.clear()
            return None, None, '指标缺失'
        if self.points:
            old = self.points[-1]
            if not (0 < ts - old[0] < 20 and topology == old[2] and set(counters) == set(old[1])
                    and all(counters[k] >= old[1][k] for k in counters)):
                self.points.clear()
        self.points.append((ts, counters, topology))
        while len(self.points) > 1 and ts - self.points[1][0] >= 60:
            self.points.popleft()
        while len(self.points) > 1 and ts - self.points[0][0] > 65:
            self.points.popleft()
        old = self.points[0]
        seconds = ts - old[0]
        if not 55 <= seconds <= 65:
            return None, seconds, '正在积累约 60 秒数据'
        return {k: counters[k] - old[1][k] for k in counters}, seconds, None


class TierWindow:
    def __init__(self):
        self.window = DeltaWindow()

    def add(self, sample):
        raw = (sample or {}).get('cache_effective')
        counters = {key + '/' + mode: n for key, group in raw['groups'].items() for mode, n in group.items()} if raw else None
        changes, seconds, reason = self.window.add((sample or {}).get('ts'), counters, raw['topology'] if raw else None)
        result = dict(ratio=None, device=None, host=None, storage=None, window_seconds=seconds,
                      input_tokens=None, hit_tokens=None, tier_tokens=None, semantics=SEMANTICS, reason=reason)
        if changes is None:
            return result
        totals = {mode: sum(n for key, n in changes.items() if key.endswith('/' + mode)) for mode in MODES}
        total = sum(totals.values())
        result.update(input_tokens=total, hit_tokens=total - totals['input'], tier_tokens=totals)
        if total == 0:
            result['reason'] = '窗口内无输入 Token'
        else:
            result.update(ratio=(total - totals['input']) / total,
                          **{tier: totals[mode] / total for tier, mode in zip(('device', 'host', 'storage'), MODES)})
        return result


class QueryWindow:
    def __init__(self):
        self.window = DeltaWindow()

    def add(self, sample):
        counters = (sample or {}).get('query_counters')
        changes, seconds, reason = self.window.add((sample or {}).get('ts'), counters)
        value = dict(ratio=None, window_seconds=seconds, reason=reason)
        if changes is not None:
            value.update(changes)
            if changes['total'] == 0:
                value['reason'] = '窗口内无 Store 查询'
            elif changes['valid'] > changes['total']:
                value['reason'] = '查询计数不一致'
            else:
                value['ratio'] = changes['valid'] / changes['total']
        return value


class StoreTierWindow:
    """Store replica lookups, not model tokens or completed physical reads."""
    def __init__(self):
        self.window = DeltaWindow()

    def add(self, sample):
        counters = (sample or {}).get('tier_query_counters')
        if counters is not None and (set(counters) != {'memory', 'ssd', 'valid', 'total'}
                                    or not all(finite(v) for v in counters.values())):
            counters = None
        changes, seconds, reason = self.window.add((sample or {}).get('ts'), counters)
        value = dict(memory=None, ssd=None, memory_hits=None, ssd_hits=None, total=None,
                     window_seconds=seconds, reason=reason, semantics='store-replica-query-v1')
        if changes is None:
            return value
        value.update(memory_hits=changes['memory'], ssd_hits=changes['ssd'], total=changes['total'])
        if not 0 <= changes['memory'] + changes['ssd'] <= changes['valid'] <= changes['total']:
            value['reason'] = '查询计数不一致'
        elif changes['total'] == 0:
            value['reason'] = '窗口内无 Store 查询'
        else:
            value.update(memory=changes['memory'] / changes['total'], ssd=changes['ssd'] / changes['total'])
        return value
