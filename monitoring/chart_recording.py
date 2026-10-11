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


def store_parts(environment):
    a3 = environment == 'a3-vllm'
    job, freshness = ('mooncake-a3', 10) if a3 else ('mooncake', 15)
    scope = f'{{environment="{environment}",job="{job}"}}'
    inventory = scope if not a3 else scope[:-1] + ',__name__=~"up|master_(allocated_bytes|total_capacity_bytes|allocated_file_size_bytes|total_file_capacity_bytes)"}'
    up='up'+scope
    definitions=[f'latest = max(tlast_over_time({inventory}[{freshness}s]))',
                 f'current_up = last_over_time({up}[{freshness}s]) and (tlast_over_time({up}[{freshness}s]) == scalar(latest))']
    healthy='((count(current_up) == 1) and (min(current_up) == 1))'
    if a3:
        definitions.append(f'sources = group by(node,instance) (last_over_time({inventory}[{freshness}s]))')
        healthy+=' and on() (count(sources) == 1)'
    else:healthy=f'({healthy} or ((count(current_up) or vector(0)) == 0))'
    definitions.append(f'healthy = ({healthy}) and on() (time() - latest < {freshness})')
    expressions={}
    for tier,metrics in CAPACITY_METRICS.items():
        for short,metric in zip(('u','t'),metrics):
            selector=metric+scope; key=tier+'_'+short
            definitions.append(f'{key}_raw = last_over_time({selector}[{freshness}s]) and (tlast_over_time({selector}[{freshness}s]) == scalar(latest))')
            rows=key+'_raw'
            if not a3:
                # Full-label matching with the projection excludes all unknown
                # business labels, rather than a hard-coded label denylist.
                definitions.append(f'{key}_rows = {rows} and (group by(job,instance,environment,node,service,role) ({rows}))')
                rows=key+'_rows'
            definitions.append(f'{key} = sum({rows}) and (count({rows}) == 1) and (min({rows}) >= 0) and (max({rows}) < Inf) and on() healthy')
        u,t=tier+'_u',tier+'_t'
        for field,expr in [('used',u),('total',t),('ratio',f'({u} / ({t} > 0)) and on() ({u} <= {t})')]:
            if a3 and field!='ratio':expr=f'({expr}) and on() (({u} <= {t}) and on() {t})'
            expressions[f'mooncake.{tier}.{field}']=expr
    return 'WITH ('+', '.join(definitions)+') ',expressions


def store_expressions(environment):
    definitions,expressions=store_parts(environment)
    return {path:definitions+expr for path,expr in expressions.items()}


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
RULE_COUNT = 6


def cutover(state):
    path = Path(state) / CUTOVER_FILE
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    ts = value['start']
    if value.get('schema') != 1 or type(ts) is not int or ts <= 0 or ts % 5:
        raise ValueError('Invalid chart recording cutover')
    return ts


def tagged(expression, labels):
    args = ', '.join(json.dumps(x) for pair in labels.items() for x in pair)
    return f'label_set(({expression}), {args})'


def cpu_vector():
    """Calculate each full CPU expression once, then label the two known hosts."""
    hosts = 'union(' + ', '.join(tagged('vector(1)', {'job':'node-a3', 'instance':address+':9100', 'node':node})
                                for node,address in host_cpu.NODES.values()) + ')'
    values=[]
    for field, expression in [('cpu',host_cpu.expression()), ('cpu_iowait',host_cpu.iowait_expression())]:
        value=f'(({expression}) >= 0) <= 100'
        if field=='cpu':value=f'({value}) and on(job,instance,node) {hosts}'
        else:value=f'label_match(({value}), "node", "a3-1|a3-2")'
        value=f'label_keep(({value}), "node")'
        value=f'label_copy(({value}), "node", "path")'
        pairs=', '.join(json.dumps(x) for role,(node,_) in host_cpu.NODES.items()
                        for x in (node,f'nodes.{role}.{field}'))
        value=f'label_map(({value}), "path", {pairs})'
        values.append(tagged(value, {'environment':'a3-vllm','schema':host_cpu.SCHEMA}))
    return 'union('+', '.join(values)+')'


def recording_rules(start):
    if type(start) is not int or start < 0 or start % 5:
        raise ValueError('Aligned chart cutover required')
    groups=[]
    for name, selected in [('cpu', [f for f in FIELDS if f['schema']==host_cpu.SCHEMA]),
                           *[(env, [f for f in FIELDS if f['environment']==env and f['schema']=='v1'])
                             for env in ('dcu-pd','a3-vllm')]]:
        values=[]; zeroes=[]
        for field in selected:
            labels={k:v for k,v in field.items() if k!='expression'}
            values.append(tagged(field['expression'], labels))
            zeroes.append(tagged('vector(0)', labels))
        if name=='cpu':value=cpu_vector()
        else:
            definitions,expressions=store_parts(name)
            value=definitions+'union('+', '.join(tagged(expr, {'environment':name,'path':path,'schema':'v1'})
                                                 for path,expr in expressions.items())+')'
        zeros='union('+', '.join(zeroes)+')'
        # Each group evaluates a value vector and a validity vector. No rule
        # reads the preceding rule, and the expensive CPU scan is shared across
        # hosts instead of repeated for each field and each validation check.
        rules=[]
        for kind, expr in [('value',value), ('valid',f'(({value}) * 0 + 1) or ({zeros})')]:
            rules.append({'record':'monitoring_chart_'+kind,
                          'expr':f'({expr}) and on() (vector(time()) >= {start})'})
        groups.append({'name':'chart_native_v1_'+name, 'interval':'5s',
                       'eval_delay':'3s', 'eval_alignment':True, 'concurrency':1,
                       'params':{'step':['5s'],'nocache':['1'],'latency_offset':['1ms'],
                                 'deny_partial_response':['1']}, 'rules':rules})
    return {'groups':groups}
