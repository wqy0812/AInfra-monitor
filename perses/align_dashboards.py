"""Shared core catalog with environment-specific queries; cache is opt-in Mooncake only."""
import argparse
import copy
import json
import re
from pathlib import Path
from generate import panel, variable
from project_queries import Queries
from dashboard_reorg import ordered, present, write_resources

NODES = {'a3-monitoring': ('a3-1','a3-2'), 'dcu-monitoring': ('dcu1','dcu2'), 'xpu-monitoring': ('xpu-2','xpu-1')}
ENV = {'a3-monitoring':'a3-vllm','dcu-monitoring':'dcu-pd','xpu-monitoring':'xpu-pd'}
HOST = [('p0','CPU 使用率（%）'),('p1','主机内存（GiB）'),('p2','文件系统容量使用率（%）'),('p3','磁盘吞吐（MiB/秒）'),('p4','网络吞吐（MiB/秒）'),('extra-load','主机负载（任务数）'),('extra-iowait','CPU I/O 等待（%）'),('extra-memory-ratio','内存占用比例（%）'),('extra-swap','Swap 已用与总量（GiB）'),('extra-fs-free','文件系统剩余容量（GiB）')]
PERFORMANCE = [('requests','后端请求速率（请求/秒）'),('output','后端输出 Token 吞吐（Token/秒）'),('ttft','首 Token 延迟 TTFT（秒）'),('itl','Token 间延迟 ITL（ms）'),('e2e','端到端延迟 E2E（秒）')]
HARDWARE = [('utilization','加速卡利用率（%）'),('memory-used','加速卡显存已用（GiB）'),('temperature','加速卡温度（°C）'),('power','加速卡功耗（W）'),('memory-total','加速卡显存总量（GiB）'),('memory-ratio','加速卡显存占用比例（%）')]
OPS = ['exist_key','put_start','put_end','put_revoke','get_replica_list','batch_exist_key','batch_put_start','batch_put_end','batch_put_revoke','batch_get_replica_list']
GAUGES = ['master_allocated_bytes','master_total_capacity_bytes','master_allocated_file_size_bytes','master_total_file_capacity_bytes','master_key_count','master_active_clients']
MOONCAKE_METRICS = GAUGES + ['master_'+op+'_'+suffix+'_total' for op in OPS for suffix in ['requests','failures']]
MOONCAKE_METRICS += ['master_attempted_evictions_total','master_successful_evictions_total','master_evicted_key_count','master_evicted_size_bytes']


def exprs(p): return [q['spec']['plugin']['spec']['query'] for q in p['spec']['queries']]


def node_filter(expr, value):
    """Intersect existing selectors, never replace their environment or role scope."""
    def sub(m):
        body=m[1]
        if 'environment=' not in body: return m[0]
        return '{'+body+',node=~'+json.dumps(value)+'}'
    return re.sub(r'\{([^{}]*)\}',sub,expr)


def transform(p, project, role=None, selectable=False):
    p=copy.deepcopy(p)
    for q in p['spec']['queries']:
        spec=q['spec']['plugin']['spec'];expr=spec['query']
        if role:
            expr=expr.replace('$role',role)
            # Request-derived role paths have their own provenance and may lack node.
            if 'monitoring_chart_' not in expr:expr=node_filter(expr,NODES[project][0 if role=='prefill' else 1])
        elif selectable:
            if '$role' not in expr:
                if 'monitoring_chart_' in expr and 'nodes.(prefill|decode)' in expr:
                    expr=expr.replace('nodes.(prefill|decode)', 'nodes.$role')
                else:expr=node_filter(expr,'$role')
        spec['query']=expr
    # Units are already normalized by the previous migration; only normalize spellings.
    p['spec']['display']['name']=p['spec']['display']['name'].replace('MiB/s','MiB/秒')
    return p


def role_variable(project):
    a,b=NODES[project]
    # A single regex works both for derived nodes.$role paths and raw node selectors.
    return variable('role','角色',[{'value':'.*','label':'全部'}, {'value':'(prefill|'+a+')','label':'Prefill'}, {'value':'(decode|'+b+')','label':'Decode'}])


def layout(d, groups):
    d['spec']['layouts']=[]
    for title, keys in groups:
        if not keys:continue
        d['spec']['layouts'].append({'kind':'Grid','spec':{'display':{'title':title},'items':[
            {'x':i%2*12,'y':i//2*8,'width':12,'height':8,'content':{'$ref':'#/spec/panels/'+key}} for i,key in enumerate(keys)]}})


def new_dashboard(source,name,title):
    d=copy.deepcopy(source);d['metadata']={'project':source['metadata']['project'],'name':name}
    d['spec']['display']={'name':title,'description':'统一核心图表；来源特有指标列于专属诊断。单位见标题，统计差异见图表提示。'}
    d['spec']['panels']={};d['spec']['layouts']=[]
    return d


def align(resources):
    from xpu_topology import configure as configure_xpu
    resources = copy.deepcopy(resources)
    resources['dashboards'] = [configure_xpu(d) for d in resources['dashboards']]
    projects={d['metadata']['project'] for d in resources['dashboards']} & set(NODES)
    if all(any(d['metadata']['project']==p and 'core-requests' in d['spec']['panels'] for d in resources['dashboards']) for p in projects):
        return copy.deepcopy(resources), []
    result=copy.deepcopy(resources);result['dashboards']=[];manifest=[]
    def put(d,key,p,source,old,role=None,selectable=False,title=None):
        project=d['metadata']['project'];p=transform(p,project,role,selectable)
        if title:p['spec']['display']['name']=title
        assert key not in d['spec']['panels'];d['spec']['panels'][key]=p
        manifest.append({'project':project,'source':source['metadata']['name'],'panel':old,
                         'target':d['metadata']['name'],'target_panel':key,'role':role,'selectable':selectable})
    for source in resources['dashboards']:
        project=source['metadata']['project'];name=source['metadata']['name']
        if project not in NODES:result['dashboards'].append(copy.deepcopy(source));continue
        if name in ('cache-store','a3-cache'):
            result['dashboards'].append(copy.deepcopy(source));continue
        if project=='a3-monitoring' and name=='backend-diagnostics':
            for role in ['prefill','decode']:
                d=new_dashboard(source,'backend-'+role,role.capitalize()+' 诊断')
                put(d,'core-queue',source['spec']['panels']['overview-p6'],source,'overview-p6',role,title=role.capitalize()+' 运行与排队请求（请求）')
                q=Queries('a3-vllm','vllm-a3',',node="'+NODES[project][0 if role=='prefill' else 1]+'"')
                p=panel(role.capitalize()+' KV 池占用',[(q.gauge('vllm:kv_cache_usage_perc')+' * 100','{{node}} · engine {{engine}}')],'%',
                    'vLLM 原生 KV-cache 使用比例乘 100；按实例及 engine 展示，不是整卡显存占用。源缺失或采集失败留空。')
                present(p,d['metadata']['name']);d['spec']['panels']['core-kv']=p
                for key in ordered(source):
                    if key in ('overview-p6','extra-request_inference_time_seconds','extra-request_time_per_output_token_seconds'):continue
                    p=source['spec']['panels'][key]
                    title=p['spec']['display']['name']
                    if key=='extra-request_prefill_time_seconds':title='本实例 Prefill 阶段耗时（秒）'
                    if key=='extra-request_decode_time_seconds':title='本实例 Decode 阶段耗时（秒）'
                    put(d,key,p,source,key,role,title=title)
                layout(d,[('核心指标',['core-queue','core-kv']),('专属诊断',[k for k in d['spec']['panels'] if not k.startswith('core-')])])
                d['spec']['variables']=[];result['dashboards'].append(d)
            continue
        d=new_dashboard(source,name,source['spec']['display']['name']);core=[];extensions=[]
        if name=='backend-performance':
            oldkeys=['overview-p1','overview-p2','overview-p3','overview-p4','overview-p5'] if project=='a3-monitoring' else ['overview-p1','overview-p2','overview-p4','overview-p5','overview-p6']
            mapping={old:('core-'+item[0],item[1]) for old,item in zip(oldkeys,PERFORMANCE)}
        elif name in ('hosts-dcu','hosts-xpu','a3-hosts'):
            mapping={k:('core-'+k,title) for k,title in HOST};d['spec']['display']['name']='主机资源 · Node Exporter'
        elif name=='accelerator-resources':
            oldkeys=['npu-'+k for k,_ in HARDWARE] if project=='a3-monitoring' else ['p5','p6','p7','p8','extra-vram-total','extra-vram-ratio']
            mapping={old:('core-'+item[0],item[1]) for old,item in zip(oldkeys,HARDWARE)}
            d['spec']['display']['name']='加速卡资源'
        elif name in ('backend-prefill','backend-decode'):
            role=name.split('-')[1]
            mapping={'extra-queue-'+role:('core-queue',role.capitalize()+' 运行与排队请求（请求）'),
                     'extra-kv-'+role:('core-kv',role.capitalize()+' KV 池占用（%）')}
        else:mapping={}
        selectable=name in ('backend-performance','accelerator-resources','hosts-dcu','hosts-xpu','a3-hosts')
        for old,(key,title) in mapping.items():
            put(d,key,source['spec']['panels'][old],source,old,selectable=selectable,title=title);core.append(key)
        for old in ordered(source):
            if old in mapping:continue
            key='live-stages' if old.startswith('live-stages-') else old
            title='网关在途处理阶段（请求）' if key=='live-stages' else ('采集目标状态（1 = 正常）' if name=='monitoring-health' and old=='overview-p0' else None)
            put(d,key,source['spec']['panels'][old],source,old,selectable=selectable,title=title);extensions.append(key)
        if project=='a3-monitoring' and name=='backend-performance':
            extra=next(x for x in resources['dashboards'] if x['metadata']['project']==project and x['metadata']['name']=='backend-diagnostics')
            for key in ['extra-request_inference_time_seconds','extra-request_time_per_output_token_seconds']:
                put(d,key,extra['spec']['panels'][key],extra,key,selectable=True);extensions.append(key)
        if core:layout(d,[('核心指标',core),('专属诊断',extensions)])
        else:
            # Preserve gateway/health subgroups; fix environment-specific stage ID.
            d['spec']['layouts']=copy.deepcopy(source['spec']['layouts'])
            for l in d['spec']['layouts']:
                for i in l['spec']['items']:
                    ref=i['content']['$ref'];key=ref.split('/')[-1]
                    if key.startswith('live-stages-'):i['content']['$ref']='#/spec/panels/live-stages'
        if selectable:
            d['spec']['variables']=[role_variable(project)]+[copy.deepcopy(v) for v in source['spec'].get('variables',[]) if v['spec']['name']!='role']
        # Keep only used variables. Stage-specific dashboards never expose role switching.
        used=set(re.findall(r'\$([A-Za-z_]\w*)','\n'.join(q for p in d['spec']['panels'].values() for q in exprs(p))))-{'__interval'}
        d['spec']['variables']=[v for v in d['spec'].get('variables',[]) if v['spec']['name'] in used]
        result['dashboards'].append(d)
    return result,manifest


def mooncake(d):
    d=copy.deepcopy(d)
    if 'mooncake-capacity' in d['spec']['panels']:return d
    q=Queries('a3-vllm','mooncake-a3')
    defs=[('capacity','Mooncake 内存容量','GiB',[(q.gauge(m)+' / 1024^3',t) for m,t in [('master_allocated_bytes','已分配'),('master_total_capacity_bytes','总容量')]],'Master 管理的内存 segment 总容量及分配量，不是 NPU 显存。'),
          ('memory-ratio','Mooncake 内存分配比例','%', [('100 * ('+q.gauge('master_allocated_bytes')+') / (('+q.gauge('master_total_capacity_bytes')+') > 0)','已分配比例')],'分配量 / 总容量；零容量时留空。'),
          ('file-capacity','Mooncake 文件层容量','GiB',[(q.gauge(m)+' / 1024^3',t) for m,t in [('master_allocated_file_size_bytes','已分配'),('master_total_file_capacity_bytes','总容量')]],'源定义为 3fs/nfs 文件存储容量；零容量不表示已启用 SSD offload，也不代表物理磁盘 I/O。'),
          ('keys','Mooncake Key 数','个',[(q.gauge('master_key_count'),'Key')],'Master 管理的 Key 数，不是模型请求数。'),
          ('clients','Mooncake 活跃客户端','个',[(q.gauge('master_active_clients'),'客户端')],'Master 报告的活跃客户端数。'),
          ('requests','Mooncake 操作请求速率','次/秒',[(q.rate('master_'+op+'_requests_total'),op) for op in OPS],'分别展示单次及批量 RPC 调用；不将批量 RPC 次数解释为 Key 数或缓存命中。'),
          ('failures','Mooncake 操作失败速率','次/秒',[(q.rate('master_'+op+'_failures_total'),op) for op in OPS],'Master 操作失败计数速率；不是模型生成失败率或缓存未命中率。'),
          ('evictions','Mooncake 驱逐操作速率','次/秒',[(q.rate(m),t) for m,t in [('master_attempted_evictions_total','尝试'),('master_successful_evictions_total','成功')]],'驱逐操作尝试和成功次数，保留源计数定义。'),
          ('evicted-keys','Mooncake 驱逐 Key 速率','个/秒',[(q.rate('master_evicted_key_count'),'Key')],'被驱逐对象数量的增长速率。'),
          ('evicted-bytes','Mooncake 驱逐数据速率','MiB/秒',[(q.rate('master_evicted_size_bytes')+' / 1024^2','数据')],'被驱逐对象字节计数，不代表物理 SSD 吞吐。')]
    keys=[]
    for suffix,title,unit,qs,desc in defs:
        key='mooncake-'+suffix;p=panel(title,qs,unit,desc+'\n\n仅 A3 Mooncake Master。保留有效零值，采集失败、过期、计数重置或缺样留空；不推断缓存命中率。')
        present(p,'a3-cache');d['spec']['panels'][key]=p;keys.append(key)
    existing=copy.deepcopy(d['spec']['layouts']);layout(d,[('Mooncake Master',keys)]);d['spec']['layouts']=existing+d['spec']['layouts']
    return d


def scrape(text):
    if '- job_name: mooncake-a3\n' in text:raise ValueError('mooncake-a3 already exists; review live config')
    return text.rstrip()+'\n\n- job_name: mooncake-a3\n  static_configs:\n  - targets: [\'122.209.21.24:9003\']\n    labels: {environment: a3-vllm, node: a3-1, service: mooncake}\n  metric_relabel_configs:\n  - source_labels: [__name__]\n    regex: \''+'('+'|'.join(MOONCAKE_METRICS)+')'+'\'\n    action: keep\n'


def verify(before,after,manifest):
    idx=lambda r:{(d['metadata']['project'],d['metadata']['name']):d for d in r['dashboards']}
    old,new=idx(before),idx(after);seen=set()
    for m in manifest:
        src=old[m['project'],m['source']]['spec']['panels'][m['panel']]
        dst=new[m['project'],m['target']]['spec']['panels'][m['target_panel']]
        expected=transform(src,m['project'],m['role'],m['selectable'])
        assert exprs(expected)==exprs(dst),m
        seen.add((m['project'],m['source'],m['panel']))
    expected={(p,n,k) for (p,n),d in old.items() for k in d['spec']['panels'] if n not in ('cache-store','a3-cache')}
    assert seen==expected,expected-seen
    for p in NODES:
        name='a3-cache' if p=='a3-monitoring' else 'cache-store'
        for key,panel_ in old[p,name]['spec']['panels'].items():assert new[p,name]['spec']['panels'][key]==panel_
        if p!='a3-monitoring':assert old[p,name]==new[p,name]
        assert (p,'backend-diagnostics') not in new
    for family in ['backend-performance','backend-prefill','backend-decode','accelerator-resources']:
        cores=[[ (k,d['spec']['panels'][k]['spec']['display']['name']) for k in ordered(d) if k.startswith('core-')] for (p,n),d in new.items() if n==family]
        assert len(cores)==3 and cores[0]==cores[1]==cores[2],family
    return {'passed':True,'original_noncache_panels':len(expected),'migrations':len(manifest),'cache_original_panels_unchanged':True,'dashboards':len(new),'panels':sum(len(d['spec']['panels']) for d in new.values())}


def main():
    p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    before=json.loads((a.evidence/'before.json').read_text());after,manifest=align(before)
    after['dashboards']=[mooncake(d) if d['metadata']['project']=='a3-monitoring' and d['metadata']['name']=='a3-cache' else d for d in after['dashboards']]
    report=verify(before,after,manifest)
    from project_split import validate
    validate(after)
    write_resources(after,a.output)
    for name,data in [('migration.json',manifest),('candidate.json',after),('semantics.json',report)]:
        (a.evidence/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    (a.evidence/'scrape-candidate.yml').write_text(scrape((a.evidence/'scrape-before.yml').read_text()))
    print(json.dumps(report))

if __name__=='__main__':main()
