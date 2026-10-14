"""Node-average utilization, with the same source gates as the Perses summary.

Query builders stay independent of dashboard tooling in the API image. Parity
tests compare their expressions with the checked-in dashboards for every node.
"""
import asyncio
import math

import httpx

from .native_schema import NODES

FIELD = 'accelerator_utilization_percent'


def expression(environment, step):
    if environment not in NODES or not isinstance(step, int) or step < 5 or step % 5:
        raise ValueError('Invalid accelerator history environment or step')
    if environment == 'dcu-pd':
        value = 'dcu_utilization_percent{node=~".*",device=~".*",environment="dcu-pd",node=~".*"}'
        up = 'up{environment="dcu-pd",job=~"dcu-.*",node=~".*"}'
        query = (f'({value} * 1) and on(job,instance) ((min_over_time({up}[$__interval]) == 1) '
                 f'and (count_over_time({up}[$__interval]) >= ($__interval / 5)) '
                 f'and (time() - timestamp({up}) < 15)) '
                 'and on(job,instance) (dcu_sample_success == 1 and (time() - dcu_sample_timestamp_seconds < 15))')
    else:
        a3 = environment == 'a3-vllm'
        job, metric, device = ('npu-a3', 'npu_chip_info_utilization', 'id') if a3 else (
            'xpu-hardware', 'node_xpu_utilization', 'devid')
        scope = f'environment="{environment}",job="{job}",node=~".*"'
        value = metric + '{' + scope + f',{device}=~".*",node=~".*"' + '}'
        up = 'up{' + scope + ',node=~".*"}'
        source = (f'({up} == 1) and (time() - timestamp({up}) < 15) '
                  f'and (min_over_time({up}[$__interval]) == 1) '
                  f'and (count_over_time({up}[$__interval]) >= ($__interval / 5))')
        if a3:
            query = (f'({value} and (({value} >= 0) and ({value} < +Inf) and ({value} <= 100)) '
                     f'and (time() - timestamp({value}) >= 0) and (time() - timestamp({value}) < 15) '
                     f'and on(job,instance,environment) ({source}))')
        else:
            query = (f'(({value} >= 0) and ({value} <= 100) and (time() - timestamp({value}) < 15) '
                     f'and (count_over_time({value}[$__interval]) >= ($__interval / 5)) '
                     f'and on(job,instance,environment) ({source}))')
    query = f'avg by(node) ({query})'
    for role, node in NODES[environment].items():
        query = f'label_replace({query}, "role", "{role.title()}", "node", "{node}")'
    return query.replace('$__interval', f'{step}s')


def samples(rows, environment, start, end):
    result, seen = {}, set()
    roles = {node: role for role, node in NODES[environment].items()}
    for row in rows:
        labels = row['metric']
        role = roles.get(labels.get('node'))
        # avg by(node) removes environment; the query itself is environment-scoped.
        if not role or labels.get('environment', environment) != environment or labels.get('role') != role.title():
            continue
        for raw_ts, raw_value in row['values']:
            ts, value = float(raw_ts), float(raw_value)
            if not math.isfinite(ts) or not start <= ts <= end:
                continue
            identity = (ts, role)
            if identity in seen:
                result.get(ts, {}).pop(role, None)
                continue
            seen.add(identity)
            if math.isfinite(value) and 0 <= value <= 100:
                result.setdefault(ts, {})[role] = value
    return result


async def history(query, environment, start, end, step, timeout=4):
    try:
        async with asyncio.timeout(timeout):
            rows = await query(expression(environment, step), start, end, step)
            return samples(rows, environment, start, end), 'ok'
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
        return {}, 'unavailable'


def attach(points, values, step):
    previous = None
    for point in points:
        ts = point['ts']
        for role, node in point['nodes'].items():
            node[FIELD] = values.get(ts, {}).get(role)
            contiguous = previous and 0 < ts - previous['ts'] <= step + .001
            if not contiguous or node[FIELD] is None or previous['nodes'][role][FIELD] is None:
                node.setdefault('gap_before', []).append(FIELD)
        previous = point
