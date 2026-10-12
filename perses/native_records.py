"""Publish approved native metrics directly through the VM datasource.

The path reader is a one-time resource migration for old editable dashboards;
the produced queries contain only ordinary metric names and labels.
"""
import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monitoring import raw_gauges

PREFIX='monitoring_native_'
ENV={'dcu-monitoring':'dcu-pd','a3-monitoring':'a3-vllm','xpu-monitoring':'xpu-pd'}
NOTE='速率和分位数采用固定 1 分钟窗口，原生 gauge 保持当前值；每 5 秒记录；计数器重置采用原生处理。按可用来源汇总，缺采或质量异常请同时查看覆盖与质量图表。新记录启用前及记录缺口留空。'
DIRECT_NOTE='瞬时值直接读取 VM 原始采样，不重复写入记录；指标及 up 须在查询时刻的 15 秒内新鲜且 up=1，保留来源有效性检查。缺失留空，历史显示仍保留的原始数据。'


def read(name,env,extra='',scale=1):
    if env=='a3-vllm' and name=='backend_decode_tokens_per_second':name='backend_output_tokens_per_second'
    selector=PREFIX+name+'{environment="'+env+'"'+(','+extra if extra else '')+'}'
    value=raw_gauges.query(env,name,extra) if name in raw_gauges.FAMILIES else 'last_over_time('+selector+'[5s])'
    return value if scale==1 else '('+value+') * '+str(scale)


def derived_query(path,env,original):
    path=path.replace('\\','')
    role='role=~"$role"' if '$role' in path else 'role="prefill"' if path.startswith('nodes.prefill.') else 'role="decode"' if path.startswith('nodes.decode.') else ''
    tags=lambda *xs:','.join(x for x in xs if x)
    legend='{{role}}'
    if '.percentiles.' in path:
        kind=next(k for k in ('ttft','itl','e2e') if '.percentiles.'+k+'.' in path)
        q='quantile="0.95"' if path.endswith('.p95') else ''
        value=read('backend_latency_seconds',env,tags(role,'kind="'+kind+'"',q))
        return 'label_map(('+value+'), "quantile", "0.5", "P50", "0.95", "P95", "0.99", "P99")',legend+' · {{quantile}}'
    for field in ('requests','output_tokens','decode_tokens'):
        if path.endswith('.'+field):return read('backend_'+field+'_per_second',env,role),legend
    if path.endswith(('.cpu','.cpu_iowait')):
        filters=','.join(dict.fromkeys(re.findall(r'node=~"[^"{}]+"',original)))
        return read('host_cpu_'+('iowait' if path.endswith('cpu_iowait') else 'busy')+'_percent',env,filters),'{{node}}'
    if '.cache_60s.' in path:
        if path.endswith('.(device|host|storage)'):return read('prefill_cache_tier_ratio',env,role,100),'{{role}} · {{tier}}'
        if env=='a3-vllm':return read('prefix_query_hit_ratio',env,tags(role,'kind="'+('external' if path.endswith('.external_ratio') else 'local')+'"'),100),legend
        if env=='xpu-pd':return read('native_prefix_hit_ratio',env,role,100),legend
        return '('+read('prefill_cache_hit_ratio',env,role,100)+') or ('+read('backend_cache_hit_ratio',env,role,100)+')',legend
    if '.hicache.representative.' in path:return read('hicache_representative_tokens',env,role),'{{role}} · {{field}}'
    if '.resources.hicache_rank_ratio.' in path:return read('hicache_usage_ratio',env,role,100),'{{role}} · DP{{dp_rank}} TP{{tp_rank}} PP{{pp_rank}}'
    if '.resources.queue.' in path:return read('scheduler_gauge',env,tags(role,'field=~"running|waiting"')),'{{role}} · {{field}} · 引擎{{engine}} DP{{dp_rank}} TP{{tp_rank}}'
    if path.startswith(('mooncake.capacity.','mooncake.ssd_capacity.')):
        tier='memory' if path.startswith('mooncake.capacity.') else 'ssd'
        used='label_set(('+read('store_used_bytes',env,'tier="'+tier+'"',1/2**30)+'), "field", "已用")'
        total='label_set(('+read('store_total_bytes',env,'tier="'+tier+'"',1/2**30)+'), "field", "总量")'
        return '('+used+') or ('+total+')','{{instance}} · {{field}}'
    if path.startswith('mooncake.query_60s.'):return read('store_query_hit_ratio',env,'kind="valid"',100),'{{instance}} · 有效查询'
    if path.startswith('mooncake.tier_query_60s.'):return read('store_query_hit_ratio',env,'kind=~"memory|ssd"',100),'{{instance}} · {{kind}}'
    if path.startswith('mooncake.resources.segments.'):
        return read('store_segment_bytes',env,scale=1/2**30),'{{instance}} · {{segment}} · {{field}}'
    raise ValueError('Native field mapping is missing: '+path)


def policy_description(old):
    # Old text commonly contains obsolete warming-up and completeness contracts.
    # Keep only the subject-level description, then state the approved policy.
    return old.split('\n\n')[0].replace('当前约 5 秒','最近 1 分钟').replace('最近约 60 秒','固定 1 分钟')+'\n\n'+NOTE


def quality_panels(document,env):
    if document['metadata']['name'] not in ('summary','backend-performance'):return
    panels=document['spec']['panels']
    if 'native-coverage' in panels:return
    def panel(title,queries,desc):
        return {'kind':'Panel','spec':{'display':{'name':title,'description':desc},
            'plugin':{'kind':'TimeSeriesChart','spec':{'legend':{'position':'bottom','mode':'list'},'yAxis':{'min':0},'visual':{'display':'line','lineWidth':1,'connectNulls':False}}},
            'queries':[{'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{'query':query,'seriesNameFormat':legend,'minStep':'5s','datasource':{'kind':'PrometheusDatasource','name':'victoriametrics'}}}}} for query,legend in queries]}}
    panels['native-coverage']=panel('后端采集覆盖（实例）',[(read('backend_available_instances',env),'{{role}} · 可用'),(read('backend_expected_instances',env),'{{role}} · 配置')],
        '可用实例少于配置实例时，吞吐、缓存和延迟为部分来源的统计，不代表全量。up 可用不保证每个指标存在；历史无记录时不显示为零。')
    panels['native-quality']=panel('源指标一致性问题（项）',[(read('histogram_quality_issues',env),'{{node}} · {{kind}} {{diagnostic}}'),(read('cache_quality_issues',env),'{{role}} · 缓存 {{kind}}')],
        '独立展示直方图缺失、+Inf/count 不一致和缓存计数不一致；不遮蔽原生计算结果。零仅表示这些检查未发现问题，不保证逐桶单调性或全部指标完整。')
    layouts=document['spec']['layouts']
    if layouts:
        items=layouts[-1]['spec']['items'];bottom=max((x.get('y',0)+x.get('height',8) for x in items),default=0)
        # Existing Perses layout items use x/y/width/height at the grid-item level.
        for index,key in enumerate(('native-coverage','native-quality')):
            items.append({'x':index*12,'y':bottom,'width':12,'height':8,'content':{'$ref':'#/spec/panels/'+key}})


def apply(resources):
    from dashboard_columns import source_identity
    result=copy.deepcopy(resources)
    for document in result['dashboards']:
        project=document['metadata']['project'];env=ENV[project];dashboard=document['metadata']['name']
        for key,panel in document['spec']['panels'].items():
            _,source,panel_key=source_identity(project,dashboard,key)
            spec=panel['spec'];changed=False
            for item in spec.get('queries',[]):
                query=item['spec']['plugin']['spec'];original=query['query']
                accelerated=query.get('datasource',{}).get('name')=='perses-accelerated'
                if 'monitoring_chart_value' in original:
                    found=re.findall(r'path=~?"((?:\\.|[^"\\])*)"',original)
                    paths=set(json.loads('"'+p+'"') for p in found)
                    if len(paths)!=1:raise ValueError('Ambiguous legacy chart field')
                    query['query'],query['seriesNameFormat']=derived_query(paths.pop(),env,original);changed=True
                elif accelerated or (source.startswith('hosts-') and panel_key in ('p0','extra-iowait')):
                    if source.startswith('hosts-'):
                        filters=','.join(dict.fromkeys(re.findall(r'node=~"[^"{}]+"',original)))
                        query['query']=read('host_cpu_'+('iowait' if 'iowait' in panel_key else 'busy')+'_percent',env,filters)
                        query['seriesNameFormat']='{{node}}'
                    else:
                        filters=['diagnostic="'+panel_key+'"']
                        filters+=list(dict.fromkeys(re.findall(r'node=~"[^"{}]+"',original)))
                        query['query']=read('backend_diagnostic_quantile',env,','.join(filters),.001 if panel_key.endswith('_ms') else 1)
                        query['query']='label_map(('+query['query']+'), "quantile", "0.5", "P50", "0.95", "P95", "0.99", "P99")'
                        query['seriesNameFormat']='{{node}} · 引擎{{engine}} DP{{dp_rank}} TP{{tp_rank}} PP{{pp_rank}} · {{stage}} {{cache_type}} · {{quantile}}'
                    changed=True
                elif 'monitoring_chart_valid' in original:
                    query['query']=read('backend_available_instances',env)
                    query['seriesNameFormat']='{{role}} · 可用实例';changed=True
                if env=='a3-vllm' and PREFIX+'backend_decode_tokens_per_second' in query['query']:
                    query['query']=query['query'].replace(PREFIX+'backend_decode_tokens_per_second',PREFIX+'backend_output_tokens_per_second')
                    changed=True
                query['query']=raw_gauges.rewrite(query['query'],env)
                if changed or PREFIX in query['query']:
                    if PREFIX+'host_cpu_' in query['query']:
                        query['query']=re.sub(r',job=~?"[^"]*"', '', query['query'])
                    if PREFIX+'prefix_query_hit_ratio' in query['query']:
                        query['seriesNameFormat']='{{role}} · '+('外部 KV' if 'kind="external"' in query['query'] else '本地')
                    if PREFIX+'backend_diagnostic_quantile' in query['query']:
                        query['seriesNameFormat']='{{node}} · 引擎{{engine}} DP{{dp_rank}} TP{{tp_rank}} PP{{pp_rank}} · {{stage}} {{cache_type}} · {{quantile}}'
                    if any(PREFIX+name in query['query'] for name in ('backend_latency_seconds','backend_diagnostic_quantile')) and '（ms）' in spec['display']['name'] and not query['query'].endswith('* 1000'):
                        query['query']='('+query['query']+') * 1000'
                    query['datasource']={'kind':'PrometheusDatasource','name':'victoriametrics'}
                    if 'P{{quantile}}' in query.get('seriesNameFormat',''):
                        query['query']='label_map(('+query['query']+'), "quantile", "0.5", "P50", "0.95", "P95", "0.99", "P99")'
                        query['seriesNameFormat']=query['seriesNameFormat'].replace('P{{quantile}}','{{quantile}}')
            if changed:
                spec['display']['description']=policy_description(spec['display'].get('description',''))
                if '有效字段' in spec['display']['name']:spec['display']['name']='后端可用实例'
            if any(raw_gauges.PREFIX in item['spec']['plugin']['spec']['query'] for item in spec['queries']):
                spec['display']['description']=spec['display'].get('description','').replace(NOTE,DIRECT_NOTE)
        quality_panels(document,env)
    result['datasources']=[d for d in result['datasources'] if d['metadata']['name']!='perses-accelerated']
    return result


if __name__=='__main__':
    from project_split import read_resources
    from dashboard_reorg import write_resources
    root=Path(__file__).parent/'projects'
    resources=apply(read_resources(root))
    write_resources(resources,root)
