"""Approved fixed-window native metrics; every rule reads raw VM series.

No publication generations, legacy chart writes, step variants or automatic
replay. JSON output is a vmalert rule file. See docs/native-standard-review-20261011.md.
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'monitoring_native_'
EVAL_DELAY_SECONDS = 5
READ_DELAY_SECONDS = 15
MAX_RECORD_AGE_SECONDS = 30
SOURCE = 'environment,job,instance'
HOSTS = {
    'dcu-pd': 'job=~"node-prefill|node-decode"',
    'a3-vllm': 'job="node-a3"',
    'xpu-pd': 'job="node-xpu"',
}


def selector(name, scope, extra=''):
    return name + '{' + scope + (',' + extra if extra else '') + '}'


def fresh(value):
    # Explicit lookback is independent of query step, while default_rollup
    # still honors exporter staleness markers.
    return f'default_rollup({value}[15s])'


def source_value(name, scope, extra='', *, counter=False):
    """Native reset handling and a 15s current-source guard, not window vetoes."""
    s = selector(name, scope, extra)
    u = selector('up', scope)
    operation = (str(counter) + '(s[1m])') if isinstance(counter, str) else 'rate(s[1m])' if counter else fresh('s')
    if counter: operation += ' and ' + fresh('s')
    return (f'WITH (s = {s}, u = {u}) '
            f'({operation}) and on({SOURCE}) ({fresh("u")} == 1)')


def aggregate(name, scope, extra='', group='environment,node', *, counter=True):
    return f'sum by({group}) ({source_value(name, scope, extra, counter=counter)})'


def ratio(numerator, denominator):
    # Unknown remains absent; zero denominator is never replaced with a zero ratio.
    return f'WITH (n = ({numerator}), d = ({denominator})) (((n >= 0) < Inf) / ((d > 0) < Inf)) <= 1'


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


def diagnostic_rules():
    """Named diagnostic populations, independent of old query expressions."""
    result=[]
    for env in ('dcu-pd', 'xpu-pd'):
        for field in ('busy','iowait'):
            result.append({'record':PREFIX+'host_cpu_'+field+'_percent','expr':cpu(env,field),'labels':{}})
    dcu_group='environment,job,instance,node,role,engine_type,model_name,dp_rank,tp_rank,pp_rank,moe_ep_rank,stage,cache_type'
    for role in ('prefill','decode'):
        names=['per_stage_req_latency_seconds','queue_time_seconds']
        if role=='decode':names+=['kv_transfer_bootstrap_ms','kv_transfer_alloc_ms']
        for name in names:
            scope='environment="dcu-pd",job="sglang-'+role+'"'
            result.append({'record':PREFIX+'backend_diagnostic_quantile',
                'expr':histogram('sglang:'+name,scope,group=dcu_group),
                'labels':{'diagnostic':'bn-'+role+'-'+name,'unit':'milliseconds' if name.endswith('_ms') else 'seconds'}})
    for role in (None,'prefill','decode'):
        names=['request_inference_time_seconds','request_time_per_output_token_seconds'] if role is None else ['request_queue_time_seconds','request_prefill_time_seconds','request_decode_time_seconds']
        scope='environment="a3-vllm",job="vllm-a3"'
        if role:scope+=',node="'+('a3-1' if role=='prefill' else 'a3-2')+'"'
        for name in names:
            result.append({'record':PREFIX+'backend_diagnostic_quantile',
                'expr':histogram('vllm:'+name,scope,group='environment,node,instance,engine'),
                'labels':{'diagnostic':'extra-'+name,'unit':'seconds'}})
    return result


def quality_ratio(numerator, denominator):
    return f'WITH (n=({numerator}), d=({denominator})) (n > bool d)'


def histogram_quality(name,scope,extra=''):
    b=fresh(selector(name+'_bucket',scope,','.join(x for x in (extra,'le="+Inf"') if x)))
    c=fresh(selector(name+'_count',scope,extra))
    u=fresh(selector('up',scope))
    return (f'WITH (b={b}, c={c}, u=({u} == 1)) '
            f'(count by(environment,node) ((b != ignoring(le) c) or '
            f'(u unless on({SOURCE}) c) or (u unless on({SOURCE}) b)) '
            f'or (0 * group by(environment,node) (u)))')


def cache_mode_quality(scope,extra):
    s=fresh(selector('sglang:prefill_effective_tokens_total',scope,extra))
    return f'count by(environment,node) (count without(mode) ({s}) != 4) or (0 * group by(environment,node) ({s}))'


def store_quality(scope):
    names=['mem_cache_hit_nums_','file_cache_hit_nums_','valid_get_nums_','total_get_nums_']
    definitions=', '.join(k+'=rate('+selector(n,scope)+'[1m])' for k,n in zip(('m','s','v','t'),names))
    return f'WITH ({definitions}) ((m+s > bool v) + (v > bool t))'


def resource_rules(add):
    """Labels retain device/rank/segment identity; no encoded paths in storage."""
    for env in ('dcu-pd','xpu-pd','a3-vllm'):
        scope='environment="'+env+'",'+HOSTS[env]
        for family,fields in [('network', [('rx','receive_bytes_total'),('tx','transmit_bytes_total')]),
                              ('disk',[('read','read_bytes_total'),('write','written_bytes_total')])]:
            for field,suffix in fields:
                add('host_'+family+'_bytes_per_second',source_value('node_'+family+'_'+suffix,scope,counter=True),field=field)
        for role in ('prefill','decode'):
            if env!='a3-vllm':
                scope='environment="'+env+'",job="sglang-'+role+'"'
                u=source_value('sglang:hicache_host_used_tokens',scope)
                t=source_value('sglang:hicache_host_total_tokens',scope)
                add('hicache_usage_ratio',ratio(u,t),role=role)
                if role=='prefill' and env=='dcu-pd':
                    # One configured representative, no sum of copied pools.
                    extra='tp_rank="0",pp_rank="0",moe_ep_rank="0"'
                    add('hicache_representative_ratio',ratio(source_value('sglang:hicache_host_used_tokens',scope,extra),source_value('sglang:hicache_host_total_tokens',scope,extra)),role=role)
    for env,job in [('dcu-pd','mooncake'),('a3-vllm','mooncake-a3')]:
        scope='environment="'+env+'",job="'+job+'"'
        add('store_segment_usage_ratio',ratio(source_value('segment_allocated_bytes',scope),source_value('segment_total_capacity_bytes',scope)))
    for role in ('prefill','decode'):
        scope='environment="dcu-pd",job="dcu-'+role+'"'
        stamp=fresh(selector('dcu_sample_timestamp_seconds',scope))
        success=fresh(selector('dcu_sample_success',scope))
        value=ratio(source_value('dcu_memory_used_bytes',scope),source_value('dcu_memory_total_bytes',scope))
        add('device_memory_ratio',f'({value}) and on({SOURCE}) (({success} == 1) and (time()-{stamp} < 15))',role=role)


def _template_build():
    rules = diagnostic_rules()

    def add(name, expression, **labels):
        if name.startswith('prefill_') or name=='native_prefix_hit_ratio':labels.setdefault('role','prefill')
        rules.append({'record': PREFIX + name, 'expr': expression, 'labels': labels})

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
                add('backend_'+short+'_total',aggregate(prefix+metric,scope,extra,counter=False),role=role)
            add('backend_input_tokens_total',aggregate(prefix+'prompt_tokens_total',scope,extra,counter=False),role=role)
            # A3 output/decode throughput is the same population. Keep one
            # record; readers expose both display fields from that value.
            if not a3 and role=='decode':
                # Current DCU and XPU DP-attention inventory: one independent
                # scheduler per DP, TP index equals DP index, PP=0. Preserve all
                # eight DP groups; a global TP=0 filter would undercount by 8x.
                selected=source_value('sglang:realtime_tokens_total',scope,'mode="decode",pp_rank="0",dp_rank!=""',counter=True)
                add('backend_decode_tokens_per_second',f'sum by(environment,node) (labels_equal(({selected}), "dp_rank", "tp_rank"))',role=role)
            for short, metric in [('ttft', 'time_to_first_token_seconds'),
                                  ('itl', 'inter_token_latency_seconds'),
                                  ('e2e', 'e2e_request_latency_seconds')]:
                # DCU ITL is an unpartitioned native histogram; TTFT/E2E use
                # the disjoint streaming/nonstreaming partitions.
                population=no_rank+',is_streaming=""' if env=='dcu-pd' and short=='itl' else extra
                add('backend_latency_seconds', histogram(prefix + metric, scope, population), role=role, kind=short)
                add('backend_latency_samples',aggregate(prefix+metric+'_count',scope,population,counter='increase'),role=role,kind=short)
                add('histogram_quality_issues',histogram_quality(prefix+metric,scope,population),role=role,kind=short)
            # Availability is visible independently of values; partial aggregates
            # are explicitly not called complete. Expected topology comes from
            # scrape.yml, not a legacy Python all-or-nothing output contract.
            up = fresh(selector('up', scope))
            expected = (4 if role == 'prefill' else 16) if a3 else 1
            node = ('a3-1' if role == 'prefill' else 'a3-2') if a3 else ({'dcu-pd': {'prefill':'dcu1','decode':'dcu2'}, 'xpu-pd': {'prefill':'xpu-2','decode':'xpu-1'}}[env][role])
            add('backend_available_instances',f'count({up} == 1) or vector(0)',environment=env,node=node,role=role)
            add('backend_expected_instances', f'vector({expected})', environment=env, node=node, role=role)
            if a3:
                for kind, family in [('local', 'prefix_cache_'), ('external', 'external_prefix_cache_')]:
                    hits = aggregate(prefix + family + 'hits_total', scope)
                    queries = aggregate(prefix + family + 'queries_total', scope)
                    add('prefix_query_hit_ratio', ratio(hits, queries), role=role, kind=kind)
                    add('prefix_query_tokens',aggregate(prefix+family+'queries_total',scope,counter='increase'),role=role,kind=kind)
                    add('prefix_hit_tokens',aggregate(prefix+family+'hits_total',scope,counter='increase'),role=role,kind=kind)
                    add('cache_quality_issues',quality_ratio(hits,queries),role=role,kind=kind)
            elif env=='dcu-pd' and role=='decode':
                hits=aggregate('sglang:cached_tokens_total',scope)
                inputs=aggregate('sglang:prompt_tokens_total',scope,extra)
                add('backend_cache_hit_ratio',ratio(hits,inputs),role=role)

    # Each independent DP group has one PP/TP/EP representative. Fixed zero or
    # absent rank is a proposed configuration contract, not dynamic election.
    representative = 'tp_rank=~"0|",pp_rank=~"0|",moe_ep_rank=~"0|"'
    scope = 'environment="dcu-pd",job="sglang-prefill"'
    for mode, tier in [('device_hit','device'), ('host_hit','host'), ('storage_hit','storage')]:
        num = aggregate('sglang:prefill_effective_tokens_total', scope, representative + ',mode="' + mode + '"')
        den = aggregate('sglang:prefill_effective_tokens_total', scope, representative + ',mode=~"device_hit|host_hit|storage_hit|input"')
        add('prefill_cache_tier_ratio', ratio(num, den), tier=tier)
    for mode in ('device_hit','host_hit','storage_hit','input'):
        add('prefill_effective_tokens',aggregate('sglang:prefill_effective_tokens_total',scope,representative+',mode="'+mode+'"',counter='increase'),mode=mode)
    hit=aggregate('sglang:prefill_effective_tokens_total',scope,representative+',mode=~"device_hit|host_hit|storage_hit"')
    add('prefill_cache_hit_ratio',ratio(hit,den))
    for mode,values in [('total','device_hit|host_hit|storage_hit|input'),('hit','device_hit|host_hit|storage_hit')]:
        add('prefill_effective_tokens',aggregate('sglang:prefill_effective_tokens_total',scope,representative+',mode=~"'+values+'"',counter='increase'),mode=mode)
    add('cache_quality_issues',cache_mode_quality(scope,representative),role='prefill',kind='effective')
    scope = 'environment="a3-vllm",job="vllm-a3",node="a3-1"'
    for mode, tier in [('local_cache_hit','device'), ('external_kv_transfer','storage')]:
        num = aggregate('vllm:prompt_tokens_by_source_total', scope, 'source="' + mode + '"')
        den = aggregate('vllm:prompt_tokens_total', scope)
        add('prefill_cache_tier_ratio', ratio(num, den), tier=tier)
    hit=aggregate('vllm:prompt_tokens_cached_total',scope)
    add('prefill_cache_hit_ratio',ratio(hit,den))
    for mode,name in [('total','prompt_tokens_total'),('hit','prompt_tokens_cached_total')]:
        add('prefill_effective_tokens',aggregate('vllm:'+name,scope,counter='increase'),mode=mode)
    for mode in ('local_compute','local_cache_hit','external_kv_transfer'):
        add('prefill_effective_tokens',aggregate('vllm:prompt_tokens_by_source_total',scope,'source="'+mode+'"',counter='increase'),mode=mode)
    parts=aggregate('vllm:prompt_tokens_by_source_total',scope,'source=~"local_compute|local_cache_hit|external_kv_transfer"')
    add('cache_quality_issues',f'WITH (p=({parts}), t=({den})) abs(p-t) > bool 0.000001',role='prefill',kind='effective')

    for env, job in [('dcu-pd','mooncake'), ('a3-vllm','mooncake-a3')]:
        scope = f'environment="{env}",job="{job}"'
        for tier, used, total in [('memory','master_allocated_bytes','master_total_capacity_bytes'),
                                  ('ssd','master_allocated_file_size_bytes','master_total_file_capacity_bytes')]:
            # Preserve individual store identities instead of rejecting any
            # future multi-store installation or summing replicated capacity.
            u = source_value(used, scope)
            t = source_value(total, scope)
            add('store_usage_ratio', ratio(u, t), tier=tier)
        if env == 'dcu-pd':
            for name, kind in [('valid_get_nums_','valid'), ('mem_cache_hit_nums_','memory'), ('file_cache_hit_nums_','ssd')]:
                add('store_query_hit_ratio', ratio(source_value(name, scope, counter=True),
                    source_value('total_get_nums_', scope, counter=True)), kind=kind)
                add('store_query_count',source_value(name,scope,counter='increase'),kind=kind)
            add('store_query_count',source_value('total_get_nums_',scope,counter='increase'),kind='total')
            add('store_quality_issues',store_quality(scope))

    resource_rules(add)
    # Store regular per-source quality signals; they are not value masks.
    for rule in list(rules):
        if rule['record']==PREFIX+'backend_diagnostic_quantile':
            expr=rule['expr']
            found=re.search(r's = ([^{}]+)_bucket\{([^{}]*)\}',expr)
            if not found:raise ValueError('Missing diagnostic source')
            add('histogram_quality_issues',histogram_quality(*found.groups()),diagnostic=rule['labels']['diagnostic'])

    # Every rule reads raw input. No cross-rule visibility/scheduling assumptions.
    groups={}
    for rule in rules:
        category='quality' if 'quality' in rule['record'] or '_instances' in rule['record'] else 'backend' if 'backend_' in rule['record'] else 'host' if 'host_' in rule['record'] or 'device_' in rule['record'] else 'cache'
        groups.setdefault(category,[]).append(rule)
    # Leave one scrape interval for delayed ingestion before sealing a tick.
    # Readers also allow independent group scheduling and remote-write visibility.
    delay = str(EVAL_DELAY_SECONDS) + 's'
    config = {'groups': [{'name': 'native_'+category, 'interval':'5s',
                         'eval_delay':delay, 'concurrency':1, 'eval_alignment':True,
                         'params': {'step':['5s'], 'latency_offset':[delay], 'deny_partial_response':['1']},
                         'rules':selected} for category,selected in groups.items()]}
    audit = {'status':'approved_native_v1', 'rule_count':len(rules),
             'candidate_max_expression_chars':max(len(r['expr']) for r in rules),
             'interval_seconds':5,'window_seconds':60}
    return config, audit


def build(catalog=None):
    """Bind reviewed metric templates to the applied directory.

    Existing source label sets remain byte-for-byte stable for one-node roles.
    Multi-node roles additionally record pooled role summaries; quantiles are
    recomputed from pooled raw buckets, never averaged from node quantiles.
    """
    from . import source_registry as registry
    if catalog is not None:
        with registry.use_catalog(catalog):
            return build()
    template, audit = _template_build()
    if not registry.configured():
        return template, audit
    groups = []
    definitions = registry.sources()
    for group in template['groups']:
        rules = []
        for environment, definition in definitions.items():
            baseline = definition['baseline']
            for original in group['rules']:
                labels = dict(original['labels'])
                if ('environment=' + json.dumps(baseline) not in original['expr']
                        and labels.get('environment') != baseline):
                    continue
                role = labels.get('role')
                if role and role not in definition['nodes']:
                    continue
                expr = registry.rewrite_query(original['expr'], environment)
                if 'environment' in labels:
                    labels['environment'] = environment
                name = original['record'].removeprefix(PREFIX)
                if name in ('backend_available_instances', 'backend_expected_instances'):
                    # One record per node preserves existing identities and
                    # provides accurate expected counts when endpoints change.
                    for node in definition['nodes'].get(role, []):
                        scoped = _node_scope(expr, environment, 'node=' + json.dumps(node))
                        if name == 'backend_expected_instances':
                            scoped = 'vector(' + str(registry.expected_instances(environment, role, node)) + ')'
                        rules.append({**original, 'expr': scoped,
                                      'labels': {**labels, 'node': node}})
                else:
                    rules.append({**original, 'expr': expr, 'labels': labels})

                # Only fields used by the compact P/D view need pooled copies.
                scalar = (name.startswith(('backend_', 'host_cpu_', 'prefix_', 'prefill_'))
                          or name in ('native_prefix_hit_ratio', 'cache_quality_issues', 'histogram_quality_issues'))
                if not scalar or 'diagnostic' in labels:
                    continue
                roles = [role] if role else list(definition['nodes'])
                for summary_role in roles:
                    if len(definition['nodes'].get(summary_role, [])) <= 1:
                        continue
                    pooled = _node_scope(expr, environment, registry.node_matcher(environment, summary_role))
                    # Remove only node from grouping, retaining le/engine/rank
                    # groups that define the source population.
                    pooled = re.sub(r'by\(([^)]*)\)', lambda m: 'by(' + ','.join(
                        x for x in m[1].split(',') if x.strip() != 'node') + ')', pooled)
                    if name == 'backend_expected_instances':
                        pooled = 'vector(' + str(registry.expected_instances(environment, summary_role)) + ')'
                    summary_labels = {k: v for k, v in labels.items() if k != 'node'}
                    summary_labels.update(environment=environment, role=summary_role, monitoring_scope='role')
                    rules.append({**original, 'expr': pooled, 'labels': summary_labels})
        if rules:
            groups.append({**group, 'rules': rules})
    all_rules = [rule for group in groups for rule in group['rules']]
    if not groups:
        # Official vmalert rejects a configuration without rules. This empty
        # vector keeps reload/removal operational without emitting samples or
        # retaining any retired foundation's recording rules.
        rule={'record':PREFIX+'catalog_empty','expr':'vector(0) < 0','labels':{}}
        groups=[{'name':'native_catalog','interval':'5s','rules':[rule]}]
        all_rules=[rule]
    return {'groups': groups}, {**audit, 'rule_count': len(all_rules),
        'candidate_max_expression_chars': max((len(rule['expr']) for rule in all_rules), default=0),
        'catalog_revision': registry.catalog().get('revision', 0)}


def _node_scope(expression, environment, matcher):
    """Constrain source selectors, replacing an existing node condition safely."""
    def replace(found):
        labels = found[1]
        if 'environment=' + json.dumps(environment) not in labels:
            return found[0]
        pattern = r'\bnode(?:=~|!~|!=|=)"(?:\\.|[^"\\])*"'
        labels = re.sub(pattern, lambda _: matcher, labels) if re.search(pattern, labels) else labels + ',' + matcher
        return '{' + labels + '}'
    return re.sub(r'\{([^{}]*)\}', replace, expression)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    config, audit = build()
    (args.output / 'rules.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in audit.items() if k != 'existing_panels'}, ensure_ascii=False))
