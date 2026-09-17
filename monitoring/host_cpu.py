"""Materialize A3's guarded CPU and iowait queries at new five-second points."""
import asyncio
import math

import httpx

from .a3 import ENVIRONMENT, NODES

SCHEMA = 'host-cpu-v1'
FIELDS = ('cpu', 'cpu_iowait')
PATH_REGEX = r'nodes\.(prefill|decode)\.(cpu|cpu_iowait)'
PATHS = {f'nodes.{role}.{field}': node for role, (node, _) in NODES.items() for field in FIELDS}


def expression():
    """Keep the existing A3 host panel's one-minute calculation and guards."""
    labels = 'environment="a3-vllm",job="node-a3",node=~".*"'
    boot = 'node_boot_time_seconds{' + labels + '}'
    up = 'up{' + labels + '}'

    def rate(extra):
        metric = 'node_cpu_seconds_total{' + labels + extra + '}'
        return (f'(rate({metric}[1m]) and (resets({metric}[1m]) == 0)'
                f' and (count_over_time({metric}[1m]) >= 12)'
                f' and (time() - timestamp({metric}) < 15)) and on(job,instance)'
                f' ((changes({boot}[1m]) == 0) and (count_over_time({boot}[1m]) >= 12)'
                f' and (min_over_time({up}[1m]) == 1) and (count_over_time({up}[1m]) >= 12))')

    idle = rate(',mode=~"idle|iowait"')
    total = rate(',mode!~"guest|guest_nice"')
    cpu = f'100 * (1 - sum by(job,instance,node) ({idle}) / sum by(job,instance,node) ({total}))'
    up = 'up{environment="a3-vllm",job="node-a3"}'
    return (f'({cpu}) and on(job,instance) ((min_over_time({up}[5s]) == 1)'
            f' and (count_over_time({up}[5s]) >= (5s / 5))'
            f' and (time() - timestamp({up}) < 15))')


def iowait_expression():
    """Retain the existing per-core rate average and its separate guards."""
    labels = 'environment="a3-vllm",job="node-a3",node=~".*"'
    metric = 'node_cpu_seconds_total{' + labels + ',mode="iowait"}'
    up = 'up{' + labels + '}'
    boot = 'node_boot_time_seconds{' + labels + '}'
    source_labels = 'job,instance,environment'

    def source(window):
        return (f'({up} == 1) and (time() - timestamp({up}) < 15)'
                f' and (min_over_time({up}[{window}]) == 1)'
                f' and (count_over_time({up}[{window}]) >= ({window} / 5))'
                f' and on({source_labels}) ((changes({boot}[{window}]) == 0)'
                f' and (count_over_time({boot}[{window}]) >= ({window} / 5))'
                f' and (time() - {boot} >= {window})'
                f' and (time() - timestamp({boot}) < 15))')

    good = (f'(time() - timestamp({metric}) < 15)'
            f' and (count_over_time({metric}[5s]) >= (5s / 5))'
            f' and on({source_labels}) ({source("5s")})'
            f' and (count_over_time({metric}[1m]) >= 12)'
            f' and (resets({metric}[1m]) == 0) and (resets({metric}[5s]) == 0)'
            f' and on({source_labels}) ({source("1m")})')
    return f'100 * avg by(environment,node) ((rate({metric}[1m]) and ({good})))'


def query_expression():
    # Distinct labels prevent the two percentages from matching in the union.
    return ' or '.join(f'label_set(({query}), "host_cpu_field", "{field}")'
                       for field, query in [('cpu', expression()), ('cpu_iowait', iowait_expression())])


def samples(series, start, end, field=None):
    """Admit only known hosts and unambiguous, finite, aligned percentages."""
    hosts = {(node, address + ':9100'): role for role, (node, address) in NODES.items()}
    result, seen = {}, set()
    for row in series:
        labels = row['metric']
        metric = field or labels.get('host_cpu_field')
        if metric == 'cpu':
            role = hosts.get((labels.get('node'), labels.get('instance'))) if labels.get('job') == 'node-a3' else None
        elif metric == 'cpu_iowait':
            role = next((r for r, (node, _) in NODES.items() if labels.get('node') == node), None)
            if labels.get('environment') != ENVIRONMENT:
                continue
        else:
            continue
        if role is None or labels.get('environment', ENVIRONMENT) != ENVIRONMENT:
            continue
        for timestamp, raw in row['values']:
            ts, value = float(timestamp), float(raw)
            if not math.isfinite(ts) or not start <= ts <= end or ts % 5:
                continue
            key = (int(ts), role, metric)
            if key in seen:
                result.pop(key, None)
                continue
            seen.add(key)
            if math.isfinite(value) and 0 <= value <= 100:
                result[key] = value
    return result


async def collect(client, vm, start, end):
    if start > end:
        return {}, 'ok'
    # Service.cycle bounds catch-up batches to 300 seconds. Never accept a
    # browser-selected history range or the 80-second replay warm-up here.
    if end - start > 295 or start % 5 or end % 5:
        raise ValueError('CPU materialization requires at most 60 aligned points')
    try:
        async with asyncio.timeout(4):
            response = await client.get(vm + '/api/v1/query_range', params={
                'query': query_expression(), 'start': start, 'end': end, 'step': 5,
                'latency_offset': '1ms', 'nocache': '1',
            })
            response.raise_for_status()
            data = response.json()
            if data.get('status') != 'success' or data.get('data', {}).get('resultType') != 'matrix':
                raise ValueError('Invalid CPU query response')
            return samples(data['data']['result'], start, end), 'ok'
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
        return {}, 'unavailable'


def attach(snapshots, points, values, query_status):
    for point in points:
        for role in NODES:
            for field in FIELDS:
                point['nodes'][role][field] = values.get((point['ts'], role, field))
    for snapshot in snapshots:
        for role in NODES:
            value = values.get((snapshot['ts'], role, 'cpu'))
            iowait = values.get((snapshot['ts'], role, 'cpu_iowait'))
            statuses = {field: 'ok' if values.get((snapshot['ts'], role, field)) is not None else 'unavailable' for field in FIELDS}
            snapshot['nodes'][role]['host_cpu'] = {
                'percent': value, 'iowait_percent': iowait,
                'status': 'ok' if all(x == 'ok' for x in statuses.values()) else 'partial' if 'ok' in statuses.values() else 'unavailable',
                'field_status': statuses,
                'query_status': query_status, 'computed_at': snapshot['ts'],
                'window_seconds': 60, 'schema': SCHEMA,
            }
