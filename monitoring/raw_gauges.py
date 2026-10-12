"""Read instantaneous source gauges without recording a second copy in VM.

The direct_* names are query-result aliases only. Group metric selectors by
source and guard so API snapshots do not issue one request per field.
"""
from collections import defaultdict
from functools import lru_cache
import json
import re

from .native_rules import HOSTS, SOURCE, fresh, selector, source_value

PREFIX = 'monitoring_direct_'
FAMILIES = frozenset(('host_gauge', 'host_filesystem_bytes', 'scheduler_gauge',
    'device_gauge', 'store_used_bytes', 'store_total_bytes', 'store_segment_bytes',
    'hicache_representative_tokens', 'native_prefix_hit_ratio'))
SUMMARY = frozenset(('store_used_bytes', 'store_total_bytes', 'native_prefix_hit_ratio'))
MATCHER = re.compile(r'\s*(\w+)\s*(=~|!~|!=|=)\s*("(?:\\.|[^"\\])*")\s*(?:,|$)')


def matchers(text):
    result=[];position=0
    while position<len(text):
        found=MATCHER.match(text,position)
        if not found:raise ValueError('Invalid gauge label filter: '+text)
        name,op,value=found.groups()
        result.append((name,op,json.loads(value)));position=found.end()
    return result


@lru_cache(maxsize=1)
def definitions():
    result=[]
    def add(name,metric,scope,extra='',guard='',**labels):
        environment=next(value for key,op,value in matchers(scope) if key=='environment' and op=='=')
        result.append(dict(name=name,metric=metric,scope=scope,extra=extra,
                           guard=guard,environment=environment,labels=labels))
    for env in HOSTS:
        scope=f'environment="{env}",'+HOSTS[env]
        for field,metric in [('memory_total','node_memory_MemTotal_bytes'),('memory_available','node_memory_MemAvailable_bytes'),
                ('swap_total','node_memory_SwapTotal_bytes'),('swap_free','node_memory_SwapFree_bytes'),
                ('load1','node_load1'),('load5','node_load5'),('load15','node_load15')]:
            add('host_gauge',metric,scope,field=field)
        for field,metric in [('total','node_filesystem_size_bytes'),('available','node_filesystem_avail_bytes')]:
            add('host_filesystem_bytes',metric,scope,field=field)
        for role in ('prefill','decode'):
            if env=='a3-vllm':
                scope='environment="a3-vllm",job="vllm-a3",node="'+('a3-1' if role=='prefill' else 'a3-2')+'"'
                fields=[('running','num_requests_running'),('waiting','num_requests_waiting'),('kv_usage','kv_cache_usage_perc')]
                prefix='vllm:'
            else:
                scope=f'environment="{env}",job="sglang-{role}"'
                fields=[('running','num_running_reqs'),('waiting','num_queue_reqs'),('kv_usage','token_usage'),
                        ('cache_hit','cache_hit_rate'),('hicache_used','hicache_host_used_tokens'),('hicache_total','hicache_host_total_tokens')]
                prefix='sglang:'
            for field,metric in fields:add('scheduler_gauge',prefix+metric,scope,field=field,role=role)
            if env=='dcu-pd' and role=='prefill':
                for field,metric in [('used','hicache_host_used_tokens'),('total','hicache_host_total_tokens')]:
                    add('hicache_representative_tokens',prefix+metric,scope,
                        'tp_rank="0",pp_rank="0",moe_ep_rank="0"',field=field,role=role)
    for env,job in [('dcu-pd','mooncake'),('a3-vllm','mooncake-a3')]:
        scope=f'environment="{env}",job="{job}"'
        for field,metric in [('used','segment_allocated_bytes'),('total','segment_total_capacity_bytes')]:
            add('store_segment_bytes',metric,scope,field=field)
        for tier,used,total in [('memory','master_allocated_bytes','master_total_capacity_bytes'),
                ('ssd','master_allocated_file_size_bytes','master_total_file_capacity_bytes')]:
            add('store_used_bytes',used,scope,guard='nonnegative',tier=tier)
            add('store_total_bytes',total,scope,guard='nonnegative',tier=tier)
    for role in ('prefill','decode'):
        scope=f'environment="dcu-pd",job="dcu-{role}"'
        for field,metric in [('utilization','utilization_percent'),('memory_used','memory_used_bytes'),
                ('memory_total','memory_total_bytes'),('temperature','temperature_celsius'),('power','power_watts')]:
            add('device_gauge','dcu_'+metric,scope,guard='device',field=field,role=role)
    add('native_prefix_hit_ratio','sglang:cache_hit_rate','environment="xpu-pd",job="sglang-prefill"',
        'tp_rank="0",pp_rank="0"',guard='ratio',role='prefill')
    return tuple(result)


def matches(value,op,expected):
    found=value==expected if op in ('=','!=') else re.fullmatch(expected,value) is not None
    return not found if op in ('!=','!~') else found


def group_expression(items):
    first=items[0];scope=first['scope']
    names='|'.join(re.escape(item['metric']) for item in items)
    extra=','.join(part for part in ('__name__=~'+json.dumps(names),first['extra']) if part)
    value=source_value('',scope,extra)
    if first['guard']=='nonnegative':value=f'(({value}) >= 0) < Inf'
    elif first['guard']=='ratio':value=f'(({value}) >= 0) <= 1'
    elif first['guard']=='device':
        stamp=fresh(selector('dcu_sample_timestamp_seconds',scope))
        success=fresh(selector('dcu_sample_success',scope))
        value=f'({value}) and on({SOURCE}) (({success} == 1) and (time()-{stamp} < 15))'
    labels={k:v for k,v in first['labels'].items() if k!='field'}
    if 'field' in first['labels']:
        pairs=', '.join(json.dumps(value) for item in items for value in (item['metric'],item['labels']['field']))
        value=f'label_map(label_copy(({value}), "__name__", "field"), "field", {pairs})'
    labels['__name__']=PREFIX+first['name']
    pairs=', '.join(json.dumps(value) for pair in labels.items() for value in pair)
    return f'label_set(({value}), {pairs})'


@lru_cache(maxsize=128)
def query(environment,name=None,extra='',summary=False):
    if environment not in HOSTS:raise ValueError('Unknown gauge environment')
    filters=matchers(extra);groups=defaultdict(list)
    for item in definitions():
        if item['environment']!=environment or (name is not None and item['name']!=name):continue
        if summary and item['name'] not in SUMMARY:continue
        labels=item['labels']
        if any(key in labels and '$' not in value and not matches(labels[key],op,value) for key,op,value in filters):continue
        key=(item['name'],item['scope'],item['extra'],item['guard'],tuple((k,v) for k,v in labels.items() if k!='field'))
        groups[key].append(item)
    expressions=[group_expression(items) for items in groups.values()]
    value='union('+', '.join(expressions)+')' if len(expressions)>1 else expressions[0] if expressions else 'vector(0) < 0'
    for key,op,pattern in filters:
        if op in ('=','!='):pattern=re.escape(pattern)
        function='label_mismatch' if op in ('!=','!~') else 'label_match'
        value=f'{function}(({value}), {json.dumps(key)}, {json.dumps("^(?:"+pattern+")$")})'
    return value


def rewrite(query_text,environment):
    """Migrate only the exact direct-gauge selectors emitted by native_records."""
    names='|'.join(sorted(FAMILIES))
    pattern=r'last_over_time\(monitoring_native_('+names+r')\{([^{}]*)\}\[5s\]\)'
    def replace(found):
        filters=matchers(found[2])
        if ('environment','=',environment) not in filters:raise ValueError('Gauge environment mismatch')
        extra=','.join(key+op+json.dumps(value) for key,op,value in filters if key!='environment')
        return '('+query(environment,found[1],extra)+')'
    return re.sub(pattern,replace,query_text)
