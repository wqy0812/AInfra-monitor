"""A3 Prefill token attribution; independent of prefix-query diagnostics."""
import json
import math
import os
from pathlib import Path
import uuid

SCHEMA = 'vllm-prefill-source-v1'
SOURCES = ('local_compute', 'local_cache_hit', 'external_kv_transfer')
FIELDS = ('ratio', 'device', 'storage', 'input_tokens', 'hit_tokens',
          'computed_tokens', 'device_hit_tokens', 'storage_hit_tokens', 'window_seconds')
PREFIX = 'nodes.prefill.cache_60s.effective.'
PATHS = tuple(PREFIX + field for field in FIELDS)
PATH_REGEX = r'nodes\.prefill\.cache_60s\.effective\..*'
METRICS = 'prompt_tokens(_by_source|_cached)?_total'
NAMES = {'vllm:prompt_tokens_by_source_total', 'vllm:prompt_tokens_total', 'vllm:prompt_tokens_cached_total'}
STATE_FILE = 'a3-prefill-effective-start.json'


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def load_since(state):
    path = Path(state) / STATE_FILE
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value.get('schema') != SCHEMA or not finite(value.get('since')):
        raise ValueError('Invalid A3 effective-cache activation metadata')
    return value['since']


def activate(state, started):
    """Persist one cutover without touching existing materialization watermarks."""
    since = load_since(state)
    if since is not None:
        return since
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    temporary = state / ('.' + STATE_FILE + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('x') as stream:
            json.dump({'schema': SCHEMA, 'since': math.ceil(started / 5) * 5}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, state / STATE_FILE)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return load_since(state)


def empty(since, reason):
    return {**dict.fromkeys(FIELDS), 'semantics': SCHEMA, 'since': since, 'reason': reason}


def counters(rows):
    sources, totals, identities = {}, {}, set()
    for row in rows:
        name = row['name']
        if name not in NAMES:
            continue
        labels = dict(row['labels'])
        instance = labels.get('instance', '')
        if (labels.get('node') != 'a3-1' or instance not in {'122.209.21.24:' + str(port) for port in range(7100, 7104)}
                or labels.get('engine') != str(int(instance.rsplit(':', 1)[1]) - 7100)
                or labels.get('environment') != 'a3-vllm' or labels.get('job') != 'vllm-a3'):
            raise ValueError('Prefill 原生分层来源身份不一致')
        value = row['value']
        if not finite(value):
            raise ValueError('原生分层计数非法')
        source = labels.pop('source', None)
        if name == 'vllm:prompt_tokens_by_source_total':
            if source not in SOURCES or source in sources:
                raise ValueError('原生分层来源缺失、重复或未知')
            sources[source] = value
        else:
            if source is not None or name in totals:
                raise ValueError('原生输入总量来源重复或不一致')
            totals[name] = value
        identities.add(json.dumps(labels, sort_keys=True))
    if set(sources) != set(SOURCES) or len(totals) != 2 or len(identities) != 1:
        raise ValueError('原生分层计数或来源不完整')
    if sum(sources.values()) != totals['vllm:prompt_tokens_total'] or sources['local_cache_hit'] + sources['external_kv_transfer'] != totals['vllm:prompt_tokens_cached_total']:
        raise ValueError('原生分层计数与输入总量不一致')
    return {**sources, **totals}, next(iter(identities))


def observe(histories, complete, since, tick):
    if since is None or tick < since:
        return empty(since, '上线前无新口径数据')
    if not complete or len(histories) != 4:
        return empty(since, 'Prefill 实例不完整或采集已过期')
    changes, seconds, identities = [], [], set()
    try:
        for history in histories:
            history = [(ts, rows) for ts, rows in history if ts >= since]
            eligible = [i for i, (ts, _) in enumerate(history[:-1]) if 55 <= history[-1][0] - ts <= 65]
            if not eligible:
                return empty(since, '正在积累上线后约 60 秒连续观测')
            index = min(eligible, key=lambda i: abs(history[-1][0] - history[i][0] - 60))
            window = history[index:]
            first = previous = identity = None
            previous_ts = None
            for ts, rows in window:
                current, current_identity = counters(rows)
                if previous is not None and (current_identity != identity or not 0 < ts - previous_ts < 10 or any(current[k] < previous[k] for k in current)):
                    raise ValueError('原生分层计数重置、来源变化或缺采')
                if first is None:
                    first = current
                previous, identity, previous_ts = current, current_identity, ts
            if identity in identities:
                raise ValueError('Prefill 原生分层来源重复')
            identities.add(identity)
            changes.append({key: previous[key] - first[key] for key in SOURCES})
            seconds.append(window[-1][0] - window[0][0])
        if len({json.loads(identity).get('model_name') for identity in identities}) != 1:
            raise ValueError('Prefill 原生分层模型来源不一致')
    except ValueError as error:
        return empty(since, str(error))
    totals = {source: sum(change[source] for change in changes) for source in SOURCES}
    total = sum(totals.values())
    device, storage = totals['local_cache_hit'], totals['external_kv_transfer']
    result = empty(since, None if total else '窗口内无有效输入 Token')
    result.update(input_tokens=total, hit_tokens=device + storage, computed_tokens=totals['local_compute'],
                  device_hit_tokens=device, storage_hit_tokens=storage, window_seconds=sum(seconds) / len(seconds))
    if total:
        result.update(ratio=(device + storage) / total, device=device / total, storage=storage / total)
    return result
