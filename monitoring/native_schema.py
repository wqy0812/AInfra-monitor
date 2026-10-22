"""Presentation mapping for VM records, not a second metric calculator.

Storage uses metric names and ordinary labels. JSON paths exist only at the
code-eval API boundary; Perses builds selectors from these same field meanings.
"""
import math
from urllib.parse import quote
from .native_rules import PREFIX, EVAL_DELAY_SECONDS, READ_DELAY_SECONDS, MAX_RECORD_AGE_SECONDS
from . import raw_gauges, source_registry

NODES=source_registry.NODES
POLICY={'schema':'native-1m-v1','window_seconds':60,'record_interval_seconds':5,
        'evaluation_delay_seconds':EVAL_DELAY_SECONDS,'snapshot_delay_seconds':READ_DELAY_SECONDS,
        'snapshot_max_age_seconds':MAX_RECORD_AGE_SECONDS,
        'partial_aggregates':True,'counter_resets':'native','history_fallback':False,
        'gauge_source':'raw','gauge_freshness_seconds':15,
        'description':'固定 1 分钟窗口；按可用来源汇总，覆盖和质量单独展示；计算记录缺口不回源，瞬时值始终直读原始采样。'}


def key(value):
    return quote(str(value),safe='').replace('.','%2E')


def put(obj,path,value):
    keys=path.split('.')
    for k in keys[:-1]:obj=obj.setdefault(k,{})
    obj[keys[-1]]=value


def get(obj,path):
    for k in path.split('.'):
        if not isinstance(obj,dict):return None
        obj=obj.get(k)
    return obj


def role_of(labels):
    role=labels.get('role')
    return role if role in ('prefill','decode') else source_registry.role_of(labels.get('environment'), labels.get('node'))


def mappings(labels):
    """Map a sample into display fields. Factors only convert display units."""
    metric=labels.get('__name__','')
    if metric.startswith(PREFIX) and metric.removeprefix(PREFIX) in raw_gauges.FAMILIES:return []
    name=metric.removeprefix(PREFIX).removeprefix(raw_gauges.PREFIX)
    env=labels.get('environment');role=role_of(labels)
    baseline=source_registry.adapter_environment(env)
    # Old A3 decode records may still exist in VM. The shared output record is
    # authoritative, including for history; never merge or fall back to its alias.
    if baseline=='a3-vllm' and name=='backend_decode_tokens_per_second':return []
    root='nodes.'+role+'.' if role else ('node_details.'+key(labels['node'])+'.'
        if labels.get('node') in source_registry.source(env)['all_nodes'] else None)
    field=labels.get('field');kind=labels.get('kind');tier=labels.get('tier')
    result=[]
    def add(path,factor=1):result.append((path,factor))
    if root:
        if name.startswith('backend_') and name.endswith('_per_second'):
            add(root+name[len('backend_'):-len('_per_second')])
            if baseline=='a3-vllm' and name=='backend_output_tokens_per_second':add(root+'decode_tokens')
        elif name in ('backend_requests_total','backend_input_tokens_total','backend_output_tokens_total'):
            add(root+'counters.'+name[len('backend_'):-len('_total')])
        elif name=='backend_latency_seconds':
            q={'0.5':'p50','0.95':'p95','0.99':'p99'}.get(labels.get('quantile'))
            if q:add(root+'percentiles.'+kind+'.'+q)
        elif name=='backend_latency_samples':add(root+'percentiles.'+kind+'.samples')
        elif name=='host_cpu_busy_percent':add(root+'cpu')
        elif name=='host_cpu_iowait_percent':add(root+'cpu_iowait')
        elif name in ('backend_available_instances','backend_expected_instances'):add(root+'coverage.'+('available' if 'available' in name else 'expected'))
        elif name=='histogram_quality_issues' and kind:add(root+'quality.histogram.'+kind)
        elif name=='cache_quality_issues' and kind:add(root+'quality.cache.'+kind)
        elif name=='prefix_query_hit_ratio':add(root+'cache_60s.'+('ratio' if kind=='local' else 'external_ratio'))
        elif name=='backend_cache_hit_ratio':add(root+'cache_60s.ratio')
        elif name in ('prefix_query_tokens','prefix_hit_tokens') and kind=='local':add(root+'cache_60s.'+('input_tokens' if name=='prefix_query_tokens' else 'hit_tokens'))
        elif name=='hicache_representative_tokens':add(root+'hicache.representative.'+field)
        elif name=='hicache_representative_ratio':add(root+'hicache.representative.ratio')
        elif name=='scheduler_gauge':
            identity='engine'+labels['engine'] if 'engine' in labels else ' / '.join(k+'='+labels[k] for k in ('engine_type','dp_rank','pp_rank','tp_rank','moe_ep_rank','priority') if k in labels)
            if field in ('running','waiting'):add(root+'resources.queue.'+key(identity+' · '+('运行' if field=='running' else '排队')))
            elif field in ('kv_usage','cache_hit'):add(root+'resources.'+('token_usage' if field=='kv_usage' else 'rank_cache')+'.'+key(identity),100)
            else:add(root+'resources.hicache_tokens.'+key(identity+' · '+('已用' if field=='hicache_used' else '总量')))
        elif name=='hicache_usage_ratio':
            identity=' / '.join(k+'='+labels[k] for k in ('engine_type','dp_rank','pp_rank','tp_rank','moe_ep_rank') if k in labels)
            add(root+'resources.hicache_rank_ratio.'+key(identity),100)
        elif name=='device_gauge':
            device=labels.get('device','unknown')
            if field in ('memory_used','memory_total'):add(root+'resources.gpu_memory.'+key(device+' · '+('已用' if field=='memory_used' else '总量')),1/2**30)
            else:add(root+'resources.gpu_'+{'utilization':'utilization','temperature':'temperature','power':'power'}[field]+'.'+key(device))
        elif name=='device_memory_ratio':add(root+'resources.gpu_memory_ratio.'+key(labels.get('device','unknown')),100)
        elif name=='host_gauge':add(root+'host.'+field)
        elif name=='host_filesystem_bytes':add(root+'host.disks.'+key(labels.get('mountpoint',''))+'.'+field)
        elif name.startswith('host_') and name.endswith('_bytes_per_second'):
            add(root+'host.'+name[len('host_'):-len('_bytes_per_second')]+'_rates.'+key(labels.get('device',''))+'.'+field)
    pre='nodes.prefill.cache_60s.'+('effective.' if baseline=='a3-vllm' else '')
    if name=='prefill_cache_tier_ratio':add(pre+tier)
    elif name=='prefill_cache_hit_ratio':add(pre+'ratio')
    elif name=='prefill_effective_tokens':
        mode=labels['mode'];field={'total':'input_tokens','hit':'hit_tokens','local_compute':'computed_tokens','local_cache_hit':'device_hit_tokens','external_kv_transfer':'storage_hit_tokens'}.get(mode)
        if field:add(pre+field)
        else:add(pre+'tier_tokens.'+mode)
    elif name=='native_prefix_hit_ratio':add('nodes.prefill.cache_60s.ratio')
    elif name in ('store_used_bytes','store_total_bytes','store_usage_ratio'):
        add('mooncake.'+('capacity' if tier=='memory' else 'ssd_capacity')+'.'+{'store_used_bytes':'used','store_total_bytes':'total','store_usage_ratio':'ratio'}[name])
    elif name=='store_query_hit_ratio':add('mooncake.'+('query_60s.ratio' if kind=='valid' else 'tier_query_60s.'+kind))
    elif name=='store_query_count':
        if kind in ('valid','total'):add('mooncake.query_60s.'+kind)
        if kind in ('memory','ssd','total'):add('mooncake.tier_query_60s.'+(kind+'_hits' if kind!='total' else kind))
    elif name=='store_quality_issues':add('mooncake.quality.issues')
    elif name=='store_segment_bytes':add('mooncake.resources.segments.'+key(labels.get('segment','')+' · '+('已用' if field=='used' else '总量')),1/2**30)
    elif name=='store_segment_usage_ratio':add('mooncake.resources.segment_ratio.'+key(labels.get('segment','')),100)
    return result


def empty_point(environment,ts):
    return {'environment':environment,'ts':ts,'source':'victoriametrics','metric_policy':POLICY,
            'node_details':{key(node):{'node':node,'role':role} for node,role in source_registry.source(environment)['all_nodes'].items()},
            'nodes':{role:{'node':node,'requests':None,'output_tokens':None,'decode_tokens':None,'cpu':None,
              'cpu_iowait':None,'rate_interval_seconds':60,'latency_window_seconds':60,
              'percentiles':{k:dict.fromkeys(('p50','p95','p99','samples')) for k in ('ttft','itl','e2e')},
              'cache_60s':{'ratio':None,'device':None,'host':None,'storage':None,'window_seconds':60,
                  'effective':{'ratio':None,'device':None,'storage':None,'window_seconds':60,'semantics':'native-1m-v1'}},
              'hicache':{'representative':dict.fromkeys(('used','total','ratio'))},'resources':{},'coverage':{},'quality':{}}
              for role,node in NODES[environment].items()},
            'mooncake':{'capacity':dict.fromkeys(('used','total','ratio')),'ssd_capacity':dict.fromkeys(('used','total','ratio')),
                'query_60s':{'ratio':None,'window_seconds':60},'tier_query_60s':{'memory':None,'ssd':None,'window_seconds':60},'resources':{}}}


def points(rows,environment,start,end,step,summary=False):
    result={};seen=set()
    directory=source_registry.nodes(environment)
    multi={role for role,names in directory.items() if len(names)>1}
    for row in rows:
        labels=row['metric']
        if labels.get('environment')!=environment:continue
        paths=mappings(labels)
        if not paths:continue
        for ts,value in row['values']:
            ts=float(ts);value=float(value)
            if not start<=ts<=end or not math.isfinite(value):continue
            point=result.setdefault(ts,empty_point(environment,ts))
            for path,factor in paths:
                if summary and ('.resources.' in path or '.host.' in path or '.counters.' in path):continue
                role=role_of(labels)
                node=labels.get('node')
                if role in multi and path.startswith('nodes.'+role+'.') and labels.get('monitoring_scope')!='role':
                    # Keep per-node values separately. Role percentiles and
                    # ratios must come from pooled raw records, not a Python
                    # average or the last node encountered.
                    if node in directory[role]:
                        path='node_details.'+key(node)+'.'+path.removeprefix('nodes.'+role+'.')
                    else:
                        continue
                identity=(ts,path)
                # An old single-store JSON field cannot represent two stores.
                # Never silently choose one or add replicated capacities.
                put(point,path,None if identity in seen else value*factor)
                seen.add(identity)
    for point in result.values():
        for role,node in point['nodes'].items():
            coverage=node['coverage'];expected=coverage.get('expected');available=coverage.get('available')
            coverage['status']='unknown' if expected is None or available is None else 'complete' if available==expected else 'partial'
            node['cache_60s']['semantics']='xpu-native-prefix-v1' if source_registry.adapter_environment(environment)=='xpu-pd' else 'native-1m-v1'
            note='仅汇总当前可用实例；覆盖不完整' if coverage['status']!='complete' else None
            issues=node['quality'].get('cache',{})
            if any(v for v in issues.values() if v is not None):note='缓存计数存在不一致；曲线显示原生计算结果'
            node['cache_60s']['reason']=note
            node['cache_60s']['effective']['reason']=note
            node['gap_before']=[k for k in ('requests','output_tokens','decode_tokens','cpu') if node.get(k) is None]
            node['gap_before'] += [k for k in ('ttft','itl','e2e') if node['percentiles'][k]['p95'] is None]
            if node['cache_60s']['ratio'] is None:node['gap_before'].append('cache')
            if node['cache_60s']['effective']['ratio'] is None:node['gap_before'].append('effective_cache')
        point['mooncake']['gap_before']=[key for key in ('capacity','ssd_capacity') if point['mooncake'][key]['ratio'] is None]
    return [result[t] for t in sorted(result)]


def snapshot(point):
    result={**point,'enabled':True,'interval_seconds':5,'retention_hours':720,'nodes':{}}
    for role,node in point['nodes'].items():
        data={**node,'rates':{k:node.get(k) for k in ('requests','output_tokens','decode_tokens')},
              'request_scope':'all','request_schema':'native-1m-v1'}
        data.update(node.get('counters',{}))
        available=node['coverage'].get('available',0)
        result['nodes'][role]={'node':node['node'],'metrics':{'status':'ok' if available>0 else 'error','observed_at':point['ts'],
            'data':data},'coverage':node['coverage'],'quality':node['quality'],
            'host_cpu':{'percent':node.get('cpu'),'iowait_percent':node.get('cpu_iowait'),'window_seconds':60}}
    result['mooncake']={'status':'ok' if point['mooncake']['capacity']['total'] is not None else 'unavailable','observed_at':point['ts'],'data':point['mooncake']}
    return result
