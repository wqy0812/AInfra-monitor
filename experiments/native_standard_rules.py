"""Review candidate for a fresh monitoring installation; NEVER loaded at runtime.

Generate short, independent raw-source rules plus a migration audit. The proposed
semantics must be approved before wiring these metrics into dashboards or APIs.
JSON is accepted by vmalert as YAML. No existing recording names are reused.
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'monitoring/perses_acceleration_catalog.json'
SOURCE = 'environment,job,instance'
HOSTS = {
    'dcu-pd': 'job=~"node-prefill|node-decode"',
    'a3-vllm': 'job="node-a3"',
    'xpu-pd': 'job="node-xpu"',
}


def selector(name, scope, extra=''):
    return name + '{' + scope + (',' + extra if extra else '') + '}'


def source_value(name, scope, extra='', *, counter=False):
    """Native reset handling and a 15s current-source guard, not window vetoes."""
    s = selector(name, scope, extra)
    u = selector('up', scope)
    operation = 'rate(s[1m])' if counter else 's'
    return (f'WITH (s = {s}, u = {u}) '
            f'({operation} and (time() - timestamp(s) < 15)) '
            f'and on({SOURCE}) ((u == 1) and (time() - timestamp(u) < 15))')


def aggregate(name, scope, extra='', group='environment,node', *, counter=True):
    return f'sum by({group}) ({source_value(name, scope, extra, counter=counter)})'


def ratio(numerator, denominator):
    # Unknown remains absent; zero denominator is never replaced with a zero ratio.
    return f'WITH (n = ({numerator}), d = ({denominator})) ((n / (d > 0)) >= 0) <= 1'


def histogram(name, scope, extra='', group='environment,node'):
    buckets = aggregate(name + '_bucket', scope, extra, 'le,' + group)
    return f'histogram_quantiles("quantile", 0.5, 0.95, 0.99, {buckets})'


def cpu(environment, field):
    scope = f'environment="{environment}",' + HOSTS[environment]
    if field == 'iowait':
        value = source_value('node_cpu_seconds_total', scope, 'mode="iowait"', counter=True)
        return f'100 * avg by(environment,node) ({value})'
    # Busy excludes iowait and idle, as in the current host charts. A3 retains
    # its ratio-of-mode-rates definition; other hosts retain per-core averaging.
    if environment == 'a3-vllm':
        idle = aggregate('node_cpu_seconds_total', scope, 'mode=~"idle|iowait"')
        total = aggregate('node_cpu_seconds_total', scope, 'mode!~"guest|guest_nice"')
        return f'100 * (1 - ({idle}) / ({total}))'
    idle = source_value('node_cpu_seconds_total', scope, 'mode="idle"', counter=True)
    wait = source_value('node_cpu_seconds_total', scope, 'mode="iowait"', counter=True)
    return f'100 * (1 - avg by(environment,node) (({idle}) + ignoring(mode) ({wait})))'


def catalog_candidates(catalog):
    result = []
    for panel in catalog['panels']:
        old = panel['expression']
        environment = {'a3': 'a3-vllm', 'dcu': 'dcu-pd', 'xpu': 'xpu-pd'}[panel['project'].split('-')[0]]
        if panel['group'] == 'cpu':
            field = 'iowait' if 'iowait' in panel['panel'] else 'busy'
            expression = cpu(environment, field)
            metric = 'monitoring_next_host_cpu_' + field + '_percent'
            labels = {}
        else:
            # Extract only the recorded population and grouping, not the legacy
            # validation expressions. Keep per-rank/per-engine diagnostic lines.
            match = re.search(r'sum by\(le,([^()]+)\) \(rate\(([^{}]+)_bucket\{([^{}]*)\}\[1m\]\)\)', old)
            if match is None:
                raise ValueError('Unrecognized histogram: ' + panel['id'])
            group, name, scope = match.groups()
            for variable in panel['variables']:
                scope = scope.replace('$' + variable, '.*')
            scope = scope.replace(',node=~".*"', '')
            expression = histogram(name, scope, group=group)
            metric = 'monitoring_next_backend_diagnostic_quantile'
            labels = {'diagnostic': panel['panel'], 'unit': 'milliseconds' if name.endswith('_ms') else 'seconds'}
        result.append({'id': panel['id'], 'old_chars': len(old),
                       'old_selectors': old.count('{'), 'new_chars': len(expression),
                       'new_selectors': expression.count('{'),
                       'record': metric, 'expr': expression, 'labels': labels})
    return result


def build():
    catalog = json.loads(CATALOG.read_text())
    audited = catalog_candidates(catalog)
    rules = [{'record': p['record'], 'expr': p['expr'], 'labels': p['labels']} for p in audited]

    def add(name, expression, **labels):
        rules.append({'record': 'monitoring_next_' + name, 'expr': expression, 'labels': labels})

    for field in ('busy', 'iowait'):
        add('host_cpu_' + field + '_percent', cpu('a3-vllm', field))

    # Tokenizer-level counters and histograms, retaining stream partitions until
    # after rate(). Scheduler replicas must not be added to these populations.
    no_rank = 'tp_rank="",pp_rank="",dp_rank="",moe_ep_rank=""'
    for env in ('dcu-pd', 'xpu-pd', 'a3-vllm'):
        for role in ('prefill', 'decode'):
            a3 = env == 'a3-vllm'
            scope = f'environment="{env}",job="vllm-a3",node="a3-{1 if role == "prefill" else 2}"' if a3 else f'environment="{env}",job="sglang-{role}"'
            prefix = 'vllm:' if a3 else 'sglang:'
            # Current source inventory: DCU has disjoint true/false tokenizer
            # partitions; XPU has an unpartitioned tokenizer series. Never sum
            # both representations if an exporter accidentally emits both.
            extra = '' if a3 else no_rank + (',is_streaming=~"true|false"' if env == 'dcu-pd' else ',is_streaming=""')
            for metric, short in [('request_success_total' if a3 else 'num_requests_total', 'requests'),
                                  ('generation_tokens_total', 'output_tokens')]:
                add('backend_' + short + '_per_second', aggregate(prefix + metric, scope, extra), role=role)
            for short, metric in [('ttft', 'time_to_first_token_seconds'),
                                  ('itl', 'inter_token_latency_seconds'),
                                  ('e2e', 'e2e_request_latency_seconds')]:
                add('backend_latency_seconds', histogram(prefix + metric, scope, extra), role=role, kind=short)
            # Availability is visible independently of values; partial aggregates
            # are explicitly not called complete. Expected topology comes from
            # scrape.yml, not a legacy Python all-or-nothing output contract.
            up = selector('up', scope)
            add('backend_available_instances', f'count by(environment,node) (({up} == 1) and (time() - timestamp({up}) < 15))', role=role)
            expected = (4 if role == 'prefill' else 16) if a3 else 1
            node = ('a3-1' if role == 'prefill' else 'a3-2') if a3 else ({'dcu-pd': {'prefill':'dcu1','decode':'dcu2'}, 'xpu-pd': {'prefill':'xpu-2','decode':'xpu-1'}}[env][role])
            add('backend_expected_instances', f'vector({expected})', environment=env, node=node, role=role)
            if a3:
                for kind, family in [('local', 'prefix_cache_'), ('external', 'external_prefix_cache_')]:
                    hits = aggregate(prefix + family + 'hits_total', scope)
                    queries = aggregate(prefix + family + 'queries_total', scope)
                    add('prefix_query_hit_ratio', ratio(hits, queries), role=role, kind=kind)

    # Each independent DP group has one PP/TP/EP representative. Fixed zero or
    # absent rank is a proposed configuration contract, not dynamic election.
    representative = 'tp_rank=~"0|",pp_rank=~"0|",moe_ep_rank=~"0|"'
    scope = 'environment="dcu-pd",job="sglang-prefill"'
    for mode, tier in [('device_hit','device'), ('host_hit','host'), ('storage_hit','storage')]:
        num = aggregate('sglang:prefill_effective_tokens_total', scope, representative + ',mode="' + mode + '"')
        den = aggregate('sglang:prefill_effective_tokens_total', scope, representative + ',mode=~"device_hit|host_hit|storage_hit|input"')
        add('prefill_cache_tier_ratio', ratio(num, den), tier=tier)
    scope = 'environment="a3-vllm",job="vllm-a3",node="a3-1"'
    for mode, tier in [('local_cache_hit','device'), ('external_kv_transfer','storage')]:
        num = aggregate('vllm:prompt_tokens_by_source_total', scope, 'source="' + mode + '"')
        den = aggregate('vllm:prompt_tokens_total', scope)
        add('prefill_cache_tier_ratio', ratio(num, den), tier=tier)
    scope = 'environment="xpu-pd",job="sglang-prefill"'
    value = source_value('sglang:cache_hit_rate', scope, 'tp_rank="0",pp_rank="0"')
    add('native_prefix_hit_ratio', f'(({value}) >= 0) <= 1')

    for env, job in [('dcu-pd','mooncake'), ('a3-vllm','mooncake-a3')]:
        scope = f'environment="{env}",job="{job}"'
        for tier, used, total in [('memory','master_allocated_bytes','master_total_capacity_bytes'),
                                  ('ssd','master_allocated_file_size_bytes','master_total_file_capacity_bytes')]:
            # Preserve individual store identities instead of rejecting any
            # future multi-store installation or summing replicated capacity.
            u = source_value(used, scope)
            t = source_value(total, scope)
            add('store_used_bytes', f'(({u}) >= 0) < Inf', tier=tier)
            add('store_total_bytes', f'(({t}) >= 0) < Inf', tier=tier)
            add('store_usage_ratio', ratio(u, t), tier=tier)
        if env == 'dcu-pd':
            for name, kind in [('valid_get_nums_','valid'), ('mem_cache_hit_nums_','memory'), ('file_cache_hit_nums_','ssd')]:
                add('store_query_hit_ratio', ratio(source_value(name, scope, counter=True),
                    source_value('total_get_nums_', scope, counter=True)), kind=kind)

    # Every rule reads raw input. No cross-rule visibility/scheduling assumptions.
    config = {'groups': [{'name': 'native_standard_candidate', 'interval':'5s',
                         'eval_delay':'3s', 'concurrency':1, 'eval_alignment':True,
                         'params': {'step':['5s'], 'latency_offset':['1ms'], 'deny_partial_response':['1']},
                         'rules':rules}]}
    audit = {'status':'candidate_requires_semantic_approval', 'existing_panels':audited,
             'existing_panel_rules':len(audited) * len(catalog['steps']),
             'candidate_panel_rules':len(audited), 'candidate_total_rules':len(rules),
             'candidate_max_expression_chars':max(len(r['expr']) for r in rules),
             'not_yet_covered':['dynamic resource paths (rank/device/segment)',
                 'realtime decode scheduler throughput', 'API latest/history JSON contract',
                 'gateway expressions beyond the migrated catalog', 'dashboard rewiring and source ownership cutover']}
    return config, audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    config, audit = build()
    (args.output / 'rules.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in audit.items() if k != 'existing_panels'}, ensure_ascii=False))
