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
    operation = (str(counter) + '(s[1m])') if isinstance(counter, str) else 'rate(s[1m])' if counter else 's'
    return (f'WITH (s = {s}, u = {u}) '
            f'({operation} and (time() - timestamp(s) < 15)) '
            f'and on({SOURCE}) ((u == 1) and (time() - timestamp(u) < 15))')


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
    b=selector(name+'_bucket',scope,','.join(x for x in (extra,'le="+Inf"') if x))
    c=selector(name+'_count',scope,extra)
    u=selector('up',scope)
    return (f'WITH (b={b}, c={c}, u=({u} == 1)) '
            f'(count by(environment,node) ((b != ignoring(le) c) or '
            f'(u unless on({SOURCE}) c) or (u unless on({SOURCE}) b)) '
            f'or (0 * group by(environment,node) (u)))')


def cache_mode_quality(scope,extra):
    s=selector('sglang:prefill_effective_tokens_total',scope,extra)
    return f'count by(environment,node) (count without(mode) ({s}) != 4) or (0 * group by(environment,node) ({s}))'


def store_quality(scope):
    names=['mem_cache_hit_nums_','file_cache_hit_nums_','valid_get_nums_','total_get_nums_']
    definitions=', '.join(k+'=rate('+selector(n,scope)+'[1m])' for k,n in zip(('m','s','v','t'),names))
    return f'WITH ({definitions}) ((m+s > bool v) + (v > bool t))'


def resource_rules(add):
    """Labels retain device/rank/segment identity; no encoded paths in storage."""
    for env in ('dcu-pd','xpu-pd','a3-vllm'):
        scope='environment="'+env+'",'+HOSTS[env]
        for field,name in [('memory_total','node_memory_MemTotal_bytes'),('memory_available','node_memory_MemAvailable_bytes'),
                           ('swap_total','node_memory_SwapTotal_bytes'),('swap_free','node_memory_SwapFree_bytes'),
                           ('load1','node_load1'),('load5','node_load5'),('load15','node_load15')]:
            add('host_gauge',source_value(name,scope),field=field)
        for field,name in [('used','node_filesystem_size_bytes'),('available','node_filesystem_avail_bytes')]:
            add('host_filesystem_bytes',source_value(name,scope),field='total' if field=='used' else field)
        for family,fields in [('network', [('rx','receive_bytes_total'),('tx','transmit_bytes_total')]),
                              ('disk',[('read','read_bytes_total'),('write','written_bytes_total')])]:
            for field,suffix in fields:
                add('host_'+family+'_bytes_per_second',source_value('node_'+family+'_'+suffix,scope,counter=True),field=field)
        for role in ('prefill','decode'):
            if env=='a3-vllm':
                scope='environment="a3-vllm",job="vllm-a3",node="'+('a3-1' if role=='prefill' else 'a3-2')+'"'
                for field,name in [('running','num_requests_running'),('waiting','num_requests_waiting'),('kv_usage','kv_cache_usage_perc')]:
                    add('scheduler_gauge',source_value('vllm:'+name,scope),field=field,role=role)
            else:
                scope='environment="'+env+'",job="sglang-'+role+'"'
                for field,name in [('running','num_running_reqs'),('waiting','num_queue_reqs'),('kv_usage','token_usage'),
                                   ('cache_hit','cache_hit_rate'),('hicache_used','hicache_host_used_tokens'),('hicache_total','hicache_host_total_tokens')]:
                    add('scheduler_gauge',source_value('sglang:'+name,scope),field=field,role=role)
                u=source_value('sglang:hicache_host_used_tokens',scope)
                t=source_value('sglang:hicache_host_total_tokens',scope)
                add('hicache_usage_ratio',ratio(u,t),role=role)
                if role=='prefill' and env=='dcu-pd':
                    # One configured representative, no sum of copied pools.
                    extra='tp_rank="0",pp_rank="0",moe_ep_rank="0"'
                    for field,name in [('used','hicache_host_used_tokens'),('total','hicache_host_total_tokens')]:
                        add('hicache_representative_tokens',source_value('sglang:'+name,scope,extra),field=field,role=role)
                    add('hicache_representative_ratio',ratio(source_value('sglang:hicache_host_used_tokens',scope,extra),source_value('sglang:hicache_host_total_tokens',scope,extra)),role=role)
    for env,job in [('dcu-pd','mooncake'),('a3-vllm','mooncake-a3')]:
        scope='environment="'+env+'",job="'+job+'"'
        for field,name in [('used','segment_allocated_bytes'),('total','segment_total_capacity_bytes')]:
            add('store_segment_bytes',source_value(name,scope),field=field)
        add('store_segment_usage_ratio',ratio(source_value('segment_allocated_bytes',scope),source_value('segment_total_capacity_bytes',scope)))
    for role in ('prefill','decode'):
        scope='environment="dcu-pd",job="dcu-'+role+'"'
        for field,metric in [('utilization','utilization_percent'),('memory_used','memory_used_bytes'),('memory_total','memory_total_bytes'),('temperature','temperature_celsius'),('power','power_watts')]:
            value=source_value('dcu_'+metric,scope)
            stamp=selector('dcu_sample_timestamp_seconds',scope)
            success=selector('dcu_sample_success',scope)
            add('device_gauge',f'({value}) and on({SOURCE}) (({success} == 1) and (time()-{stamp} < 15))',field=field,role=role)
        value=ratio(source_value('dcu_memory_used_bytes',scope),source_value('dcu_memory_total_bytes',scope))
        add('device_memory_ratio',f'({value}) and on({SOURCE}) (({success} == 1) and (time()-{stamp} < 15))',role=role)


def build():
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
            if a3:
                add('backend_decode_tokens_per_second',aggregate(prefix+'generation_tokens_total',scope),role=role)
            elif role=='decode':
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
            up = selector('up', scope)
            expected = (4 if role == 'prefill' else 16) if a3 else 1
            node = ('a3-1' if role == 'prefill' else 'a3-2') if a3 else ({'dcu-pd': {'prefill':'dcu1','decode':'dcu2'}, 'xpu-pd': {'prefill':'xpu-2','decode':'xpu-1'}}[env][role])
            add('backend_available_instances',f'count(({up} == 1) and (time() - timestamp({up}) < 15)) or vector(0)',environment=env,node=node,role=role)
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
    config = {'groups': [{'name': 'native_'+category, 'interval':'5s',
                         'eval_delay':'3s', 'concurrency':1, 'eval_alignment':True,
                         'params': {'step':['5s'], 'latency_offset':['1ms'], 'deny_partial_response':['1']},
                         'rules':selected} for category,selected in groups.items()]}
    audit = {'status':'approved_native_v1', 'rule_count':len(rules),
             'candidate_max_expression_chars':max(len(r['expr']) for r in rules),
             'interval_seconds':5,'window_seconds':60}
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
