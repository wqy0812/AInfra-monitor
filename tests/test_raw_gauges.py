"""Direct gauge reads retain source validity, identity and presentation fields."""
import json
import os
from pathlib import Path
import time
import uuid

import httpx
import pytest

from monitoring import api, native_rules, native_schema, raw_gauges

ROOT=Path(__file__).resolve().parents[1]


def test_gauges_are_not_recorded_and_retired_copies_cannot_fill_gaps():
    config,_=native_rules.build()
    assert not any(r['record'].removeprefix(native_rules.PREFIX) in raw_gauges.FAMILIES
                   for g in config['groups'] for r in g['rules'])
    labels={'environment':'dcu-pd','node':'dcu1','role':'prefill','field':'running'}
    rows=[{'metric':dict(labels,__name__=native_rules.PREFIX+'scheduler_gauge'),'values':[[100,'99']]},
          {'metric':dict(labels,__name__=raw_gauges.PREFIX+'scheduler_gauge'),'values':[[100,'0']]}]
    points=native_schema.points(rows,'dcu-pd',100,100,5)
    assert list(points[0]['nodes']['prefill']['resources']['queue'].values())==[0]
    assert native_schema.points(rows[:1],'dcu-pd',100,100,5)==[]


def test_summary_reads_only_summary_gauges_and_dashboard_migration_is_idempotent():
    import sys
    sys.path.insert(0,str(ROOT/'perses'))
    from native_records import apply
    from project_split import read_resources
    for env in api.ENVIRONMENTS:
        query=api.history_expression(env,'summary')
        assert 'node_memory_' not in query and 'dcu_temperature_' not in query
    current=read_resources(ROOT/'perses/projects')
    assert apply(current)==current
    for dashboard in current['dashboards']:
        for panel in dashboard['spec']['panels'].values():
            for item in panel['spec']['queries']:
                query=item['spec']['plugin']['spec']['query']
                assert not any(native_rules.PREFIX+name+'{' in query for name in raw_gauges.FAMILIES)


@pytest.mark.skipif(not os.environ.get('NATIVE_RECORDING_TEST_VM'),reason='Disposable VM required')
@pytest.mark.parametrize('step',[5,60])
def test_actual_vm_direct_gauges_preserve_freshness_zero_and_source_guards(step):
    url=os.environ['NATIVE_RECORDING_TEST_VM'];assert url=='http://127.0.0.1:18543'
    end=int(time.time()//60)*60-300
    token='direct-'+uuid.uuid4().hex
    lines=[]
    def emit(metric,value,labels,age=7):
        tags=','.join(k+'='+json.dumps(v) for k,v in labels.items())
        lines.append(f'{metric}{{{tags}}} {value} {(end-age)*1000}')
    def source(env,job,node,case='ok',up=1,age=7):
        labels=dict(environment=env,job=job,node=node,instance=token+'-'+case)
        emit('up',up,labels,age);return labels
    host=source('dcu-pd','node-prefill','dcu1')
    emit('node_memory_MemTotal_bytes',100,host);emit('node_memory_MemAvailable_bytes',0,host)
    emit('node_filesystem_size_bytes',100,dict(host,mountpoint='/data'))
    emit('node_filesystem_avail_bytes',0,dict(host,mountpoint='/data'))
    for case,up,up_age,metric_age in [('down',0,7,7),('stale-up',1,16,7),('stale-metric',1,7,16)]:
        bad=source('dcu-pd','node-prefill','dcu1',case,up,up_age)
        emit('node_memory_MemTotal_bytes',999,bad,metric_age)
    scheduler=source('dcu-pd','sglang-prefill','dcu1')
    for rank,value in [('0',10),('1',20)]:
        labels=dict(scheduler,tp_rank=rank,pp_rank='0',moe_ep_rank='0')
        emit('sglang:num_running_reqs',0,labels)
        emit('sglang:num_queue_reqs',4,labels)
        emit('sglang:hicache_host_used_tokens',value,labels)
        emit('sglang:hicache_host_total_tokens',40,labels)
    for role,node,value in [('prefill','a3-1',3),('decode','a3-2',7)]:
        labels=source('a3-vllm','vllm-a3',node,role)
        emit('vllm:num_requests_running',value,dict(labels,engine='0'))
        emit('vllm:num_requests_waiting',0,dict(labels,engine='0'))
    xpu=source('xpu-pd','sglang-prefill','xpu-2')
    emit('sglang:cache_hit_rate',.4,dict(xpu,tp_rank='0',pp_rank='0'))
    emit('sglang:cache_hit_rate',.9,dict(xpu,tp_rank='1',pp_rank='0'))
    for case,success,age in [('ok',1,7),('sample-failed',0,7),('sample-stale',1,16)]:
        device=source('dcu-pd','dcu-prefill','dcu1',case)
        emit('dcu_sample_success',success,device)
        emit('dcu_sample_timestamp_seconds',end-age,device)
        emit('dcu_temperature_celsius',0,dict(device,device='card0'))
    store=source('dcu-pd','mooncake','dcu1')
    for metric,value in [('master_allocated_bytes',0),('master_total_capacity_bytes',100),
            ('master_allocated_file_size_bytes',-1),('master_total_file_capacity_bytes',200)]:emit(metric,value,store)
    emit('segment_allocated_bytes',0,dict(store,segment='seg1'))
    emit('segment_total_capacity_bytes',100,dict(store,segment='seg1'))
    with httpx.Client(base_url=url,trust_env=False) as client:
        client.post('/api/v1/import/prometheus',content='\n'.join(lines)+'\n').raise_for_status()
        client.get('/internal/force_flush').raise_for_status()
        def query(expr):
            response=client.post('/api/v1/query_range',data={'query':expr,'start':end,'end':end,'step':step,'nocache':1,'latency_offset':'1ms'})
            response.raise_for_status();data=response.json();assert data['status']=='success'
            return [r for r in data['data']['result'] if r['metric'].get('instance','').startswith(token)]
        full=query(api.history_expression('dcu-pd'))
        assert full and all('999' not in [v for t,v in row['values']] for row in full)
        point=native_schema.points(full,'dcu-pd',end,end,step)[0]
        node=point['nodes']['prefill']
        assert node['host']['memory_total']==100 and node['host']['memory_available']==0
        assert node['host']['disks'][native_schema.key('/data')]=={'total':100,'available':0}
        assert sorted(node['resources']['queue'].values())==[0,0,4,4]
        assert node['hicache']['representative']['used']==10
        assert node['resources']['gpu_temperature']=={'card0':0}
        assert point['mooncake']['capacity']['used']==0 and point['mooncake']['capacity']['total']==100
        assert point['mooncake']['ssd_capacity']['used'] is None
        assert all('sample-failed' not in r['metric']['instance'] and 'sample-stale' not in r['metric']['instance'] for r in full)
        summary=native_schema.points(query(api.history_expression('dcu-pd','summary')),'dcu-pd',end,end,step,summary=True)[0]
        assert summary['mooncake']['capacity']==point['mooncake']['capacity']
        assert not summary['nodes']['prefill']['resources'] and 'host' not in summary['nodes']['prefill']
        xpu_point=native_schema.points(query(api.history_expression('xpu-pd')),'xpu-pd',end,end,step)[0]
        assert xpu_point['nodes']['prefill']['cache_60s']['ratio']==.4
        for role,value in [('prefill',3),('decode',7)]:
            selected=query(raw_gauges.query('a3-vllm','scheduler_gauge',f'role=~"{role}",field=~"running|waiting"'))
            assert len(selected)==2 and {r['metric']['role'] for r in selected}=={role}
            assert {r['metric']['field']:float(r['values'][0][1]) for r in selected}=={'running':value,'waiting':0}
