import json
from pathlib import Path
import pytest
from monitoring.cache_metrics import store_metrics, StoreTierWindow, QueryWindow
from monitoring.calculator import parse_prom
from monitoring.replay import replay
from monitoring.api import Service, encode, PATHS


def sample(ts, memory=6, ssd=3, total=10, valid=9):
    return {'ts': ts, 'tier_query_counters': {'memory': ts*memory, 'ssd': ts*ssd, 'total': ts*total, 'valid': ts*valid}}


@pytest.mark.parametrize('used,total,expected', [(25,100,.25),(0,100,0),(None,100,None),(25,None,None),(0,0,None),(-1,100,None),(101,100,None)])
def test_ssd_capacity_independent(used,total,expected):
    lines=['master_allocated_bytes 20','master_total_capacity_bytes 40']
    lines += [name+' '+str(value) for name,value in [('master_allocated_file_size_bytes',used),('master_total_file_capacity_bytes',total)] if value is not None]
    result=store_metrics(parse_prom('\n'.join(lines)))
    assert result['ssd_capacity']['ratio']==expected
    assert result['capacity']['ratio']==.5


@pytest.mark.parametrize('memory,ssd,total,valid,expected', [(10,0,10,10,(1,0)),(0,10,10,10,(0,1)),(6,3,10,9,(.6,.3)),(0,0,10,0,(0,0)),(0,0,0,0,(None,None)),(8,4,10,10,(None,None)),(6,3,10,8,(None,None))])
def test_window_semantics(memory,ssd,total,valid,expected):
    w=StoreTierWindow()
    for ts in range(0,61,5):result=w.add(sample(ts,memory,ssd,total,valid))
    assert (result['memory'],result['ssd'])==expected
    assert result['window_seconds']==60
    assert result['total']==total*60
    assert result['memory_hits']==memory*60


@pytest.mark.parametrize('fault',['missing','reset','gap','repeat','outage'])
def test_window_invalidates(fault):
    w=StoreTierWindow()
    for ts in range(0,61,5):assert w.add(sample(ts))['memory']==(.6 if ts>=55 else None)
    bad={'missing':{'ts':65,'tier_query_counters':{'memory':None}},'reset':sample(65,0,0,0,0),'gap':sample(90),'repeat':sample(60),'outage':None}[fault]
    assert w.add(bad)['memory'] is None


def test_old_queries_survive_missing_new_metrics():
    old=QueryWindow();new=StoreTierWindow()
    for ts in range(0,61,5):
        data=store_metrics(parse_prom(f'valid_get_nums_ {ts*9}\ntotal_get_nums_ {ts*10}'))
        data['ts']=ts;a=old.add(data);b=new.add(data)
    assert a['ratio']==.9 and b['memory'] is None


def groups(stop=140,failed=None):
    out={}
    for ts in range(0,stop+1,5):
        out[ts]=parse_prom(f'''up {0 if ts==failed else 1}
master_allocated_bytes 20
master_total_capacity_bytes 40
master_allocated_file_size_bytes 25
master_total_file_capacity_bytes 100
mem_cache_hit_nums_ {ts*6}
file_cache_hit_nums_ {ts*3}
valid_get_nums_ {ts*9}
total_get_nums_ {ts*10}''')
    return {'mooncake':(sorted(out),out)}


def test_replay_outage_and_encode():
    snaps,points=replay(groups(failed=65),0,140)
    assert points[12]['mooncake']['tier_query_60s']['ssd']==.3
    assert snaps[13]['mooncake']['status']=='error'
    assert points[24]['mooncake']['tier_query_60s']['ssd'] is None
    assert points[25]['mooncake']['tier_query_60s']['ssd']==.3
    assert points[-1]['mooncake']['tier_query_60s']==snaps[-1]['mooncake']['data']['tier_query_60s']
    encoded=encode(points)
    rows=parse_prom(encoded)
    valid=[r for r in rows if r['name']=='monitoring_chart_valid' and r['labels']['path']=='mooncake.tier_query_60s.ssd']
    assert valid[13]['value']==0 and valid[-1]['value']==1
    assert 'mooncake.ssd_capacity.used' in PATHS


@pytest.mark.asyncio
async def test_history_roundtrip_gaps_and_old_fields():
    _,points=replay(groups(failed=65),0,140)
    # Simulate VM materialized series, retaining every 5-second validity value.
    allrows={}
    for p in points:
        for row in parse_prom(encode([p])):
            allrows.setdefault((row['name'],row['labels']['path']),[]).append([p['ts'],row['value']])
    service=Service()
    async def query(expr,start,end,step):
        name='monitoring_chart_value' if 'chart_value' in expr else 'monitoring_chart_valid'
        result=[]
        for (metric,path),samples in allrows.items():
            if metric!=name:continue
            if 'mooncake.ssd_capacity' in path:continue  # pre-feature history
            values=[]
            for ts,value in samples:
                if ts%step:continue
                if expr.startswith('min_over_time'):
                    value=min(v for t,v in samples if ts-step<t<=ts)
                values.append([ts,str(value)])
            result.append({'metric':{'path':path},'values':values})
        return result
    service.query=query
    try:
        history=await service.history(1,start=0,end=140)
        last=history['points'][-1]['mooncake']
        assert last['capacity']['ratio']==.5 and last['ssd_capacity']['ratio'] is None
        assert last['tier_query_60s']['ssd']==.3
        assert 'ssd_capacity' in last['gap_before']
        assert 'ssd_query' in history['points'][13]['mooncake']['gap_before']
        assert last['query_60s']['ratio']==.9
    finally:await service.client.aclose()


def test_scrape_whitelist():
    import yaml,re
    config=yaml.safe_load((Path(__file__).parents[1]/'deploy/scrape.yml').read_text())
    job=next(j for j in config['scrape_configs'] if j['job_name']=='mooncake')
    regex=job['metric_relabel_configs'][0]['regex']
    assert all(re.fullmatch(regex,name) for name in ['master_allocated_file_size_bytes','master_total_file_capacity_bytes','mem_cache_hit_nums_','file_cache_hit_nums_'])

@pytest.mark.asyncio
async def test_downsample_preserves_hidden_fault():
    service=Service()
    # A failed five-second sample between two healthy display points must break the line.
    async def query(expr,start,end,step):
        assert step==15
        rows=[]
        for path in PATHS:
            if 'min_over_time' in expr:
                values=[[100,'0' if path=='mooncake.tier_query_60s.ssd' else '1']]
            elif 'chart_valid' in expr:values=[[100,'1']]
            else:values=[[100,'0.3']]
            rows.append({'metric':{'path':path},'values':values})
        return rows
    service.query=query
    try:
        history=await service.history(2,start=0,end=7200)
        store=history['points'][0]['mooncake']
        assert store['tier_query_60s']['ssd']==.3
        assert store['gap_before']==['ssd_query']
    finally:await service.client.aclose()
