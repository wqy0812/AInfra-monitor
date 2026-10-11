"""Raw-source recording rules for the chart fields with exact MetricsQL semantics.

Output labels retain the historical chart contract. The persistent cutover bounds
native writes; Python stops publishing these paths, including catch-up batches.
No rules read a Python-derived metric or another recording rule.
"""
import json
from pathlib import Path

from . import host_cpu

CUTOVER_FILE = 'chart-recording-cutover.json'
CAPACITY_METRICS = {
    'capacity': ('master_allocated_bytes', 'master_total_capacity_bytes'),
    'ssd_capacity': ('master_allocated_file_size_bytes', 'master_total_file_capacity_bytes'),
}


def store_expressions(environment):
    """Match replay's latest scrape, single source and per-tier validity rules."""
    a3 = environment == 'a3-vllm'
    job, freshness = ('mooncake-a3', 10) if a3 else ('mooncake', 15)
    scope = f'{{environment="{environment}",job="{job}"}}'
    # Export/replay chooses the newest scrape of the whole job (DCU), or rejects
    # more than one fresh source (A3). Do not let an old gauge survive a newer up.
    up = 'up' + scope
    inventory = scope if not a3 else scope[:-1] + ',__name__=~"up|master_(allocated_bytes|total_capacity_bytes|allocated_file_size_bytes|total_file_capacity_bytes)"}'
    latest = f'max(tlast_over_time({inventory}[{freshness}s]))'
    if a3:
        sources = f'group by(node,instance) (last_over_time({inventory}[{freshness}s]))'
        source_guard = f'(count({sources}) == 1)'
    else:
        source_guard = 'vector(1)'
    current_up = f'(last_over_time({up}[{freshness}s]) and (tlast_over_time({up}[{freshness}s]) == scalar({latest})))'
    healthy = f'((count({current_up}) == 1) and (min({current_up}) == 1))'
    if not a3:
        healthy = f'({healthy} or ((count({current_up}) or vector(0)) == 0))'
    guard = f'({healthy} and on() {source_guard} and on() (time() - {latest} < {freshness}))'

    def single(metric):
        selector = metric + scope
        aligned = f'(last_over_time({selector}[{freshness}s]) and (tlast_over_time({selector}[{freshness}s]) == scalar({latest})))'
        if not a3:
            # Default full-label matching against the projection rejects ANY
            # business label, including labels unknown to this implementation.
            aligned = f'({aligned} and (group by(job,instance,environment,node,service,role) ({aligned})))'
        return f'(sum({aligned}) and (count({aligned}) == 1) and (min({aligned}) >= 0) and (max({aligned}) < Inf) and on() {guard})'

    result = {}
    for tier, (used, total) in CAPACITY_METRICS.items():
        definitions = f'WITH (u = {single(used)}, t = {single(total)}) '
        paired = '((u <= t) and on() t)'
        for field, expression in [('used', 'u'), ('total', 't'), ('ratio', '(u / (t > 0)) and on() (u <= t)')]:
            if a3 and field != 'ratio':
                expression = f'({expression}) and on() {paired}'
            result[f'mooncake.{tier}.{field}'] = definitions + expression
    return result


def fields():
    result = []
    for role, (node, address) in host_cpu.NODES.items():
        for field, expression in [('cpu', host_cpu.expression()), ('cpu_iowait', host_cpu.iowait_expression())]:
            # samples() rejects duplicate results per host and out-of-range values.
            source = f'label_match(({expression}), "node", "{node}")'
            if field == 'cpu':
                source = f'label_match(label_match(({source}), "instance", "{address.replace(".", "[.]")}:9100"), "job", "node-a3")'
            expr = f'WITH (v = ({source})) (sum(v) and (count(v) == 1) and (min(v) >= 0) and (max(v) <= 100))'
            result.append({'path': f'nodes.{role}.{field}', 'environment': 'a3-vllm',
                           'schema': host_cpu.SCHEMA, 'node': node, 'expression': expr})
    for environment in ('dcu-pd', 'a3-vllm'):
        for path, expression in store_expressions(environment).items():
            result.append({'path': path, 'environment': environment, 'schema': 'v1', 'expression': expression})
    return result


FIELDS = fields()
PATHS = {env: frozenset(f['path'] for f in FIELDS if f['environment'] == env)
         for env in ('dcu-pd', 'a3-vllm', 'xpu-pd')}
RULE_COUNT = len(FIELDS) * 2


def cutover(state):
    path = Path(state) / CUTOVER_FILE
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    ts = value['start']
    if value.get('schema') != 1 or type(ts) is not int or ts <= 0 or ts % 5:
        raise ValueError('Invalid chart recording cutover')
    return ts


def recording_rules(start):
    if type(start) is not int or start < 0 or start % 5:
        raise ValueError('Aligned chart cutover required')
    groups = []
    for environment in ('dcu-pd', 'a3-vllm'):
        rules = []
        for field in FIELDS:
            if field['environment'] != environment:
                continue
            labels = {key: value for key, value in field.items() if key != 'expression'}
            original = field['expression']
            # An invalid value is absent, never a plausible zero with a stale
            # validity flag. Valid zeroes remain real zeroes.
            for kind, expression in [('value', f'({original})'),
                                     ('valid', f'WITH (v = ({original})) ((count(v) > bool 0) or vector(0))')]:
                rules.append({'record': 'monitoring_chart_' + kind, 'labels': labels,
                              'expr': f'({expression}) and on() (vector(time()) >= {start})'})
        groups.append({'name': 'chart_native_v1_' + environment, 'interval': '5s',
                       'eval_delay': '3s', 'eval_alignment': True, 'concurrency': 1,
                       'params': {'step': ['5s'], 'nocache': ['1'], 'latency_offset': ['1ms'],
                                  'deny_partial_response': ['1']}, 'rules': rules})
    return {'groups': groups}
