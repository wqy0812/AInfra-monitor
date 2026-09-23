"""DCU raw-metric panels and scoped scrape expansion; no network or writes on import."""
import copy
import json
import re
from pathlib import Path
from generate import panel
from project_queries import Queries

ROOT = Path(__file__).resolve().parent
HIST = ['queue_time_seconds', 'per_stage_req_latency_seconds', 'kv_transfer_latency_ms',
        'kv_transfer_total_mb', 'kv_transfer_speed_gb_s', 'kv_transfer_bootstrap_ms',
        'kv_transfer_alloc_ms', 'hicache_backup_duration_seconds', 'load_back_duration_seconds']
GAUGES = ['max_total_num_tokens', 'max_total_num_tokens_swa', 'num_used_tokens',
          'kv_available_tokens', 'kv_evictable_tokens', 'kv_used_tokens',
          'swa_available_tokens', 'swa_evictable_tokens', 'swa_used_tokens', 'swa_token_usage',
          'num_prefill_bootstrap_queue_reqs', 'num_prefill_inflight_queue_reqs',
          'num_decode_prealloc_queue_reqs', 'num_decode_transfer_queue_reqs']
COUNTERS = ['num_transfer_failed_reqs_total', 'hicache_backup_tokens_total',
            'hicache_backup_bytes_total', 'load_back_tokens_total', 'load_back_bytes_total',
            'hicache_dropped_tokens_total']
EXTRA = 'sglang:(' + '|'.join(GAUGES + COUNTERS) + '|(' + '|'.join(HIST) + ')_(bucket|sum|count))'
GROUP = 'environment,job,instance,node,role,engine_type,model_name,dp_rank,tp_rank,pp_rank,moe_ep_rank,stage,cache_type'
LEGEND = '{{role}} · DP {{dp_rank}} / TP {{tp_rank}} · {{stage}} {{cache_type}}'


def expand_scrape(text):
    """Add an environment-scoped alternative to exactly two keep rules."""
    blocks = re.split(r'(?=^- job_name: )', text, flags=re.M)
    for i, block in enumerate(blocks):
        if not any(block.startswith('- job_name: sglang-' + role + '\n') for role in ('prefill', 'decode')):
            continue
        if 'dcu-pd;' + EXTRA in block:
            continue
        line = re.search(r"    regex: '([^\n]+)'", block)
        assert line, block
        old = line[1]
        if 'source_labels: [__name__]' in block:
            block = block.replace('source_labels: [__name__]', 'source_labels: [environment, __name__]', 1)
            old = '.*;' + old
        new = '(' + old + '|dcu-pd;' + EXTRA + ')'
        block = re.sub(r"    regex: '[^\n]+'", lambda _: "    regex: '" + new + "'", block, count=1)
        blocks[i] = block
    return ''.join(blocks)


def inventory(texts):
    result = {}
    for role, text in texts.items():
        rows, bounds, names = [], {}, set()
        for line in text.splitlines():
            if not line or line.startswith('#'):
                continue
            m = re.match(r'([^ {]+)(?:\{(.*?)\})?\s+(\S+)', line)
            if not m:
                continue
            name = m[1]; names.add(name)
            labels = dict(re.findall(r'(\w+)="([^"]*)"', m[2] or ''))
            if name.endswith('_bucket'):
                bounds.setdefault(name[:-7], set()).add(labels['le'])
            if name == 'sglang:num_running_reqs':
                rows.append(labels)
        representatives = {}
        for labels in rows:
            key = labels.get('dp_rank', '')
            rank = tuple(int(labels.get(k, 0)) for k in ('pp_rank', 'tp_rank', 'moe_ep_rank'))
            if key not in representatives or rank < representatives[key][0]:
                representatives[key] = (rank, {k: labels.get(k, '') for k in ('dp_rank','pp_rank','tp_rank','moe_ep_rank')})
        result[role] = {'names': sorted(names), 'bounds': {k: sorted(v, key=float) for k,v in bounds.items()},
                        'representatives': [v[1] for _,v in sorted(representatives.items())]}
    return result


def compact_description(text):
    return '\n\n'.join(part for part in re.split(r'\n\s*\n', text.strip())
                       if part.split('\n',1)[0].strip().strip('*').strip() not in
                       ('时间口径','零值与空白','零值和空白','项目与源口径'))


def presentation(d, units=None):
    """Idempotent descriptions and unit scaling, preserving unrelated settings."""
    d = copy.deepcopy(d)
    units = units or {}
    for key,p in d['spec']['panels'].items():
        s=p['spec']; display=s['display']; display['description']=compact_description(display.get('description',''))
        axis=s['plugin']['spec'].get('yAxis',{}); old=axis.get('label','')
        if old not in ('秒','s','毫秒','ms'):
            continue
        identity=d['metadata']['project']+'/'+d['metadata']['name']+'/'+key
        target=units.get(identity, 'ms' if 'ITL' in display['name'] else '秒')
        factor=(.001 if old in ('ms','毫秒') else 1)/( .001 if target=='ms' else 1)
        if factor != 1:
            for q in s.get('queries',[]):
                spec=q['spec']['plugin']['spec'];spec['query']='('+spec['query']+') * '+str(factor)
        axis['label']=target
        desc=display['description']
        desc=re.sub(r'((?:\*\*)?Y 轴单位(?:\*\*)?\n)[^\n]*(?:\n(?!\n)[^\n]*)*',lambda m:m[1]+target,desc)
        # Replace legacy explicit conversion explanations after scaling.
        if target=='秒':
            desc=desc.replace('毫秒；1000 ms = 1 秒。','秒。').replace('毫秒（ms）','秒（s）')
        display['description']=desc
    return d


def histogram_quantiles(q, name, bounds, group=GROUP):
    """Bounded expression: complete source families, positive observations, no resets."""
    bucket=q.s(name+'_bucket');count=q.s(name+'_count')
    family=q.s('', ',__name__=~'+json.dumps(name+'_(bucket|count|sum)'))
    infinite=q.s(name+'_bucket', ',le="+Inf"')
    errors=[f'(count_over_time({family}[1m]) unless ({q.good(family,counter=True)}))',
            f'(count without(le) ({bucket}) != {len(bounds)})',
            f'({infinite} != ignoring(le) {count})',
            f'({count} unless ignoring(le) {bucket})',
            f'({bucket} unless ignoring(le) {count})',
            f'({count} unless {q.s(name+"_sum")})']
    expr=(f'histogram_quantiles("perses_quantile", 0.5, 0.95, 0.99, sum by(le,{group}) (rate({bucket}[1m])))'
          f' unless on({group}) (count by({group}) ('+' or '.join(errors)+'))'
          f' and on({group}) (sum by({group}) (rate({count}[1m])) > 0)')
    for value,label in [('0.5','P50'),('0.95','P95'),('0.99','P99')]:
        expr=f'label_replace(({expr}), "perses_quantile", "{label}", "perses_quantile", "{value.replace(".","[.]")}")'
    return expr


def append(d,key,title,queries,unit,meaning):
    full='bn-'+key
    desc='指标含义\n'+meaning+'\n\nY 轴单位\n'+unit+'\n\n曲线与范围\nDCU；按角色、实例和 rank 区分，不累加复制 rank。'
    if full in d['spec']['panels']:
        return
    d['spec']['panels'][full]=panel(title,queries,unit,desc)
    items=d['spec']['layouts'][0]['spec']['items']
    added=[x for x in items if x['content']['$ref'].split('/')[-1].startswith('bn-')]
    base=[x for x in items if x not in added]
    start=max((x['y']+x['height'] for x in base),default=0)
    keys=[x['content']['$ref'].split('/')[-1] for x in added]+[full]
    d['spec']['layouts'][0]['spec']['items']=base+[{'x':12*(i%2),'y':start+8*(i//2),'width':12,'height':8,'content':{'$ref':'#/spec/panels/'+k}} for i,k in enumerate(keys)]


def configure(document, inv, units=None):
    d=copy.deepcopy(document)
    if d['metadata']['project']!='dcu-monitoring':
        return presentation(d,units)
    name=d['metadata']['name']
    if name not in ('backend-diagnostics','cache-store'):
        return presentation(d,units)
    def histogram(role,metric,title,unit='秒',scale=1):
        full='sglang:'+metric; data=inv[role]
        if full not in data['bounds']:
            return
        q=Queries('dcu-pd','sglang-'+role)
        value=histogram_quantiles(q,full,data['bounds'][full])
        append(d,role+'-'+metric,title, [('('+value+') * '+str(scale),LEGEND+' · {{perses_quantile}}')],unit,
               '源直方图 P50/P95/P99；各阶段独立观察，可能存在包含关系，不能相加或相减分位数。')
        append(d,role+'-'+metric+'-samples',title+' · 样本数',[(q.rate(full+'_count')+' * 60',LEGEND)],'次',
               '对应计时器最近一分钟观测次数估算；阶段可能每请求记录多次，不作为去重请求数。')
    if name=='backend-diagnostics':
        q=Queries('dcu-pd','sglang-prefill')
        qs=[]
        for mode,label in [('prefill_compute','实际计算'),('prefill_cache','缓存复用')]:
            for rep in inv['prefill']['representatives']:
                selector=',mode='+json.dumps(mode)+''.join(','+k+'='+json.dumps(v) for k,v in rep.items())
                qs.append((q.rate('sglang:realtime_tokens_total',selector),label+' · DP {{dp_rank}} / TP {{tp_rank}}'))
        append(d,'prefill-throughput','Prefill 计算与缓存吞吐',qs,'Token / 秒','各独立 DP 组选择最小 PP/TP/EP 代表 rank。实际计算包含重算工作，缓存计数不代表唯一输入 token。')
        for role in ('prefill','decode'):
            q=Queries('dcu-pd','sglang-'+role)
            histogram(role,'queue_time_seconds',role.title()+' 排队耗时')
            histogram(role,'per_stage_req_latency_seconds',role.title()+' 阶段耗时')
            queues=['num_prefill_bootstrap_queue_reqs','num_prefill_inflight_queue_reqs'] if role=='prefill' else ['num_decode_prealloc_queue_reqs','num_decode_transfer_queue_reqs']
            append(d,role+'-queues',role.title()+' 分离调度队列',[(q.gauge('sglang:'+m),LEGEND+' · '+m.replace('num_','').replace('_queue_reqs','')) for m in queues],'请求','分离部署专用队列；不能用通用排队数替代。')
            for metric,title,unit,scale in [('kv_transfer_latency_ms','KV 传输耗时','秒',.001),('kv_transfer_total_mb','KV 传输大小','MB',1),('kv_transfer_speed_gb_s','KV 传输速度','GB/s',1),('kv_transfer_bootstrap_ms','KV Bootstrap 耗时','秒',.001),('kv_transfer_alloc_ms','KV 分配等待','秒',.001)]:
                histogram(role,metric,role.title()+' '+title,unit,scale)
            append(d,role+'-transfer-failed',role.title()+' KV 传输失败',[(q.rate('sglang:num_transfer_failed_reqs_total'),LEGEND)],'请求 / 秒','源端记录的传输失败请求速率，不能直接推算失败比例。')
            for pool in ('kv','swa'):
                total='max_total_num_tokens'+('_swa' if pool=='swa' else '')
                metrics=[(total,'总容量')]+[(pool+'_'+kind+'_tokens',label) for kind,label in [('used','活跃'),('available','可用'),('evictable','可回收')]]
                append(d,role+'-'+pool+'-capacity',role.title()+' '+pool.upper()+' 池容量',[(q.gauge('sglang:'+m),LEGEND+' · '+label) for m,label in metrics if 'sglang:'+m in inv[role]['names']],'Token','总容量、活跃分配、空闲和可回收槽位分别展示；可回收缓存不等于活跃请求占用。')
                metric='token_usage' if pool=='kv' else 'swa_token_usage'
                append(d,role+'-'+pool+'-usage',role.title()+' '+pool.upper()+' 池占用',[(q.gauge('sglang:'+metric)+' * 100',LEGEND)],'%','源调度器池占用比例，不是整卡显存使用率。')
    else:
        for role in ('prefill','decode'):
            q=Queries('dcu-pd','sglang-'+role)
            for suffix,title,unit,scale in [('tokens_total','搬运 Token 速率','Token / 秒',1),('bytes_total','搬运字节速率','GiB / 秒',1/2**30)]:
                pairs=[('hicache_backup_'+suffix,'设备 → 主机'),('load_back_'+suffix,'主机 → 设备')]
                qs=[(q.rate('sglang:'+m)+' * '+str(scale),LEGEND+' · {{pool}} · '+label) for m,label in pairs if 'sglang:'+m in inv[role]['names']]
                if qs:append(d,role+'-hicache-'+suffix,role.title()+' HiCache '+title,qs,unit,'设备与本机主机内存之间的搬运；不是主机到 Mooncake Store 的写入。字节计数包含侧路搬运，token 保留池维度。')
            for metric,title in [('hicache_backup_duration_seconds','备份耗时'),('load_back_duration_seconds','回载耗时')]:
                histogram(role,metric,role.title()+' HiCache '+title)
            if 'sglang:hicache_dropped_tokens_total' in inv[role]['names']:
                append(d,role+'-hicache-dropped',role.title()+' HiCache 未备份丢弃',[(q.rate('sglang:hicache_dropped_tokens_total'),LEGEND+' · {{pool}} · {{reason}}')],'Token / 秒','设备缓存未经主机备份而销毁的 token，按池和原因区分。')
    return presentation(d,units)


def default_configure(d):
    inv_path=ROOT/'dcu_bottlenecks_inventory.json'
    units_path=ROOT/'time_units.json'
    return configure(d,json.loads(inv_path.read_text()),json.loads(units_path.read_text()) if units_path.exists() else {}) if inv_path.exists() else presentation(d)
