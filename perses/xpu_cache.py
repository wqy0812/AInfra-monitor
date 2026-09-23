"""XPU Prefill panels based on metrics verified at its live endpoint."""
import json,copy
from pathlib import Path
def configure(d):
    template=copy.deepcopy(next(iter(d["spec"]["panels"].values())))
    s='environment="xpu-pd",job="sglang-prefill",instance="122.209.21.33:8501"'
    def metric(n,extra=''):return 'sglang:'+n+'{'+s+extra+'}'
    items=[
    ('采集状态', 'up{'+s+'}', 'up', 'Prefill 原始指标采集状态，1 表示成功。'),
    ('整体前缀缓存命中率',metric('cache_hit_rate',',tp_rank="0",pp_rank="0"')+' * 100','%', '取代表 rank 的原生前缀缓存命中率；包含整体缓存复用，不是 CPU HiCache 独立命中率。'),
    ('缓存命中 token 速率','rate('+metric('cached_tokens_total')+'[1m])','token/s','原生累计缓存 token 的一分钟速率，仅取 Prefill，不与 Decode 相加。'),
    ('CPU → XPU 回载 token 速率','rate('+metric('load_back_tokens_total')+'[1m])','token/s','原生 HiRadixCache 回载计数的速率，可能包含多个 rank；不作为唯一请求 token 数或独立命中率。'),
    ('XPU → CPU 淘汰 token 速率','rate('+metric('evicted_tokens_total')+'[1m])','token/s','原生 HiRadixCache 淘汰计数的速率，保留框架聚合口径，不与请求 token 直接比较。'),
    ('平均回载耗时','rate('+metric('load_back_duration_seconds_sum')+'[1m]) / (rate('+metric('load_back_duration_seconds_count')+'[1m]) > 0) * 1000','ms','原生计时器每次回载操作平均耗时；无操作时留空，不等同于用户请求时延。'),
    ('平均淘汰耗时','rate('+metric('eviction_duration_seconds_sum')+'[1m]) / (rate('+metric('eviction_duration_seconds_count')+'[1m]) > 0) * 1000','ms','原生计时器每次淘汰操作平均耗时；无操作时留空。'),
    ('回载操作速率','rate('+metric('load_back_duration_seconds_count')+'[1m])','次/s','原生回载计时器观测次数速率，可能为多 rank 聚合。')]
    d['spec']['display']={'name':'XPU Prefill 与 HiCache','description':'XPU Prefill 原始指标，每 5 秒采集。缓存回载和淘汰保留框架计数口径；整体前缀命中率不等于 CPU 独立命中率。缺失及无分母留空。'}
    d['spec']['variables']=[];d['spec']['panels']={};grid=[]
    for i,(name,q,unit,desc) in enumerate(items):
     panel=copy.deepcopy(template);panel['spec']['display']={'name':name,'description':desc};panel['spec']['plugin']['spec']['yAxis']={'label':unit,'min':0};panel['spec']['queries'][0]['spec']['plugin']['spec'].update(query=q,seriesNameFormat='{{instance}} {{cache_type}}');d['spec']['panels']['p'+str(i)]=panel
     grid.append({'x':12*(i%2),'y':8*(i//2),'width':12,'height':8,'content':{'$ref':'#/spec/panels/p'+str(i)}})
    d['spec']['layouts']=[{'kind':'Grid','spec':{'items':grid}}]
    return d

if __name__=="__main__":
    p=Path(__file__).parent/"projects/xpu-monitoring/dashboards/cache-store.json"
    p.write_text(json.dumps(configure(json.loads(p.read_text())),ensure_ascii=False,indent=2)+"\n")
