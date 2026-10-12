"""Approved semantics, read-only API boundary and direct-VM publication."""
import asyncio
import json
from pathlib import Path
import sys
import time
import httpx
import pytest
from monitoring import api,native_rules,native_schema,raw_gauges

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'perses'))


def row(metric,value,ts=100,**labels):
    prefix=raw_gauges.PREFIX if metric in raw_gauges.FAMILIES else native_rules.PREFIX
    return {'metric':{'__name__':prefix+metric,'environment':'a3-vllm','node':'a3-1','role':'prefill',**labels},'values':[[ts,str(value)]]}


def test_partial_sources_keep_values_and_report_coverage():
    rows=[row('backend_requests_per_second',12),row('backend_available_instances',3),row('backend_expected_instances',4),
          row('backend_latency_seconds',2,kind='ttft',quantile='0.95'),row('cache_quality_issues',1,kind='effective')]
    point=native_schema.points(rows,'a3-vllm',100,100,5)[0]
    node=point['nodes']['prefill']
    assert node['requests']==12 and node['percentiles']['ttft']['p95']==2
    assert node['coverage']=={'available':3,'expected':4,'status':'partial'}
    assert '不一致' in node['cache_60s']['effective']['reason']
    snapshot=native_schema.snapshot(point)
    assert snapshot['nodes']['prefill']['metrics']['data']['rates']['requests']==12
    assert snapshot['metric_policy']['window_seconds']==60


def test_replicated_stores_are_not_silently_summed_in_single_store_json():
    rows=[row('store_used_bytes',10,tier='memory',instance='s1'),row('store_used_bytes',20,tier='memory',instance='s2')]
    point=native_schema.points(rows,'a3-vllm',100,100,5)[0]
    assert point['mooncake']['capacity']['used'] is None


@pytest.mark.parametrize('role,node',[('prefill','a3-1'),('decode','a3-2')])
@pytest.mark.parametrize('value',[0,12])
@pytest.mark.parametrize('reverse',[False,True])
def test_a3_shared_token_record_preserves_both_fields_with_retired_records(role,node,value,reverse):
    rows=[row('backend_output_tokens_per_second',value,role=role,node=node),
          row('backend_decode_tokens_per_second',99,role=role,node=node)]
    point=native_schema.points(rows[::-1] if reverse else rows,'a3-vllm',100,100,5)[0]
    assert point['nodes'][role]['output_tokens']==point['nodes'][role]['decode_tokens']==value
    rates=native_schema.snapshot(point)['nodes'][role]['metrics']['data']['rates']
    assert rates['output_tokens']==rates['decode_tokens']==value


def test_a3_missing_shared_record_does_not_fall_back_to_retired_alias():
    rows=[row('backend_expected_instances',4),row('backend_decode_tokens_per_second',99)]
    node=native_schema.points(rows,'a3-vllm',100,100,5)[0]['nodes']['prefill']
    assert node['output_tokens'] is None and node['decode_tokens'] is None
    assert 'output_tokens' in node['gap_before'] and 'decode_tokens' in node['gap_before']


@pytest.mark.parametrize('env,node',[('dcu-pd','dcu2'),('xpu-pd','xpu-1')])
def test_other_backends_keep_distinct_output_and_decode_rates(env,node):
    rows=[row('backend_output_tokens_per_second',3,environment=env,node=node,role='decode'),
          row('backend_decode_tokens_per_second',8,environment=env,node=node,role='decode')]
    data=native_schema.points(rows,env,100,100,5)[0]['nodes']['decode']
    assert data['output_tokens']==3 and data['decode_tokens']==8


def test_rules_read_raw_only_and_have_one_fixed_interval():
    config,meta=native_rules.build()
    assert meta['window_seconds']==60 and meta['rule_count']>87
    for group in config['groups']:
        assert group['interval']=='5s'
        assert group['eval_delay']==group['params']['latency_offset'][0]=='5s'
        for rule in group['rules']:
            assert native_rules.PREFIX not in rule['expr']
            assert 'monitoring_chart_' not in rule['expr'] and '$' not in rule['expr']
            assert len(rule['expr'])<1200
    rules=[rule for group in config['groups'] for rule in group['rules']]
    assert len({(rule['expr'],json.dumps(rule['labels'],sort_keys=True)) for rule in rules})==len(rules)
    source=(ROOT/'monitoring/recording_runtime.py').read_text()
    assert 'os.execv' in source and 'generation' not in source and 'publication' not in source


@pytest.mark.asyncio
async def test_cycle_reads_native_records_without_import_or_export(monkeypatch):
    calls=[];now=1000
    monkeypatch.setattr(api.time,'time',lambda:now)
    async def handler(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/api/v1/query_range':
            assert 'monitoring_native_' in request.content.decode()
            return httpx.Response(200,json={'status':'success','data':{'result':[row('backend_requests_per_second',2,985),row('backend_available_instances',4,985),row('backend_expected_instances',4,985)]}})
        return httpx.Response(200,json={'status':'success','data':{'result':[]}})
    service=api.Service('a3-vllm');await service.client.aclose()
    from monitoring.query_client import QueryClient
    service.client=QueryClient(transport=httpx.MockTransport(handler))
    try:
        await service.cycle()
        assert service.watermark==985 and service.latest['nodes']['prefill']['metrics']['data']['rates']['requests']==2
        assert all(path in ('/api/v1/query','/api/v1/query_range') for _,path in calls)
        assert not hasattr(service,'raw') and not hasattr(service,'replay')
    finally:await service.close()


def test_published_resources_are_direct_vm_and_regeneration_is_idempotent():
    from native_records import apply
    from project_split import read_resources,validate
    from dashboard_columns import apply as columns
    resources=read_resources(ROOT/'perses/projects')
    candidate=apply(resources)
    validate(candidate);validate(columns(candidate))
    assert apply(candidate)==candidate
    assert all(d['metadata']['name']!='perses-accelerated' for d in candidate['datasources'])
    text=json.dumps(candidate)
    assert 'monitoring_chart_' not in text and '/internal/perses' not in text
    for document in candidate['dashboards']:
        for panel in document['spec']['panels'].values():
            for item in panel['spec']['queries']:
                q=item['spec']['plugin']['spec']
                if 'monitoring_native_' in q['query']:
                    assert q['datasource']['name']=='victoriametrics'
                    if any(name in q['query'] for name in ('backend_latency_seconds','backend_diagnostic_quantile')) and '（ms）' in panel['spec']['display']['name']:
                        assert q['query'].endswith('* 1000')
                    elif 'backend_latency_seconds' in q['query']:
                        assert not q['query'].endswith('* 1000')
                    if document['metadata']['project']=='a3-monitoring':
                        assert 'monitoring_native_backend_decode_tokens_per_second' not in q['query']


def test_a3_dashboard_migration_and_legacy_generation_use_shared_record():
    from native_records import apply,derived_query
    from project_split import read_resources
    resources=read_resources(ROOT/'perses/projects')
    migrated=0
    for document in resources['dashboards']:
        if document['metadata']['project']!='a3-monitoring':continue
        for panel in document['spec']['panels'].values():
            for item in panel['spec']['queries']:
                q=item['spec']['plugin']['spec']
                if 'monitoring_native_backend_output_tokens_per_second' in q['query']:
                    q['query']=q['query'].replace('backend_output_tokens_per_second','backend_decode_tokens_per_second')
                    migrated+=1
    assert migrated==2
    assert apply(resources)==read_resources(ROOT/'perses/projects')
    query,_=derived_query('nodes.decode.decode_tokens','a3-vllm','')
    assert query=='last_over_time(monitoring_native_backend_output_tokens_per_second{environment="a3-vllm",role="decode"}[5s])'


@pytest.mark.skipif(not __import__('os').environ.get('NATIVE_RECORDING_TEST_VM'),reason='Disposable VM required')
def test_actual_vm_a3_shared_token_record_preserves_role_queries_and_api():
    import uuid
    from native_records import read
    url=__import__('os').environ['NATIVE_RECORDING_TEST_VM']
    assert url=='http://127.0.0.1:18543'
    end=int(time.time()//5)*5-100
    fixture='a3-shared-'+uuid.uuid4().hex
    expected={'prefill':5,'decode':18}
    lines=[]
    for role,rates in [('prefill',(2,3)),('decode',(7,11))]:
        node=native_schema.NODES['a3-vllm'][role]
        for engine,rate in enumerate(rates):
            tags=f'environment="a3-vllm",job="vllm-a3",node="{node}",instance="{fixture}-{engine}",engine="{engine}"'
            for offset in range(-65,1,5):
                stamp=(end+offset)*1000
                lines.extend([f'up{{{tags}}} 1 {stamp}',f'vllm:generation_tokens_total{{{tags}}} {1000+(offset+65)*rate} {stamp}'])
    with httpx.Client(base_url=url,trust_env=False) as client:
        client.post('/api/v1/import/prometheus',content='\n'.join(lines)+'\n').raise_for_status()
        client.get('/internal/force_flush').raise_for_status()
        def query(expr):
            response=client.post('/api/v1/query',data={'query':expr,'time':end,'step':'5s','nocache':1,'latency_offset':'1ms'})
            response.raise_for_status()
            data=response.json();assert data['status']=='success'
            return data['data']['result']
        records=[];tested=[]
        for group in native_rules.build()[0]['groups']:
            for rule in group['rules']:
                if rule['record']!=native_rules.PREFIX+'backend_output_tokens_per_second' or 'vllm:' not in rule['expr']:continue
                rows=query(rule['expr'].replace('{environment=',f'{{instance=~"{fixture}-.*",environment='))
                role=rule['labels']['role'];tested.append(role)
                assert len(rows)==1 and float(rows[0]['value'][1])==expected[role]
                tags={**rows[0]['metric'],**rule['labels']};tags.pop('__name__',None)
                labels=','.join(k+'='+json.dumps(v) for k,v in tags.items())
                records.append(f'{rule["record"]}{{{labels}}} {rows[0]["value"][1]} {end*1000}')
        assert sorted(tested)==['decode','prefill']
        client.post('/api/v1/import/prometheus',content='\n'.join(records)+'\n').raise_for_status()
        client.get('/internal/force_flush').raise_for_status()
        for role,value in expected.items():
            stored=query(read('backend_decode_tokens_per_second','a3-vllm',f'role="{role}"'))
            assert len(stored)==1 and float(stored[0]['value'][1])==value
        response=client.post('/api/v1/query_range',data={'query':api.history_expression('a3-vllm'),
            'start':end,'end':end,'step':'5s','nocache':1,'latency_offset':'1ms'})
        response.raise_for_status()
        point=native_schema.points(response.json()['data']['result'],'a3-vllm',end,end,5)[0]
        for role,value in expected.items():
            assert point['nodes'][role]['output_tokens']==point['nodes'][role]['decode_tokens']==value


@pytest.mark.skipif(not __import__('os').environ.get('NATIVE_RECORDING_TEST_VM'),reason='Disposable VM required')
def test_actual_vm_preserves_all_eight_independent_decode_groups():
    url=__import__('os').environ['NATIVE_RECORDING_TEST_VM']
    assert url=='http://127.0.0.1:18543'
    end=int(time.time()//5)*5-100
    client=httpx.Client(base_url=url,trust_env=False)
    lines=[]
    instance='fixture-native-dp-'+__import__('uuid').uuid4().hex
    for env in ('dcu-pd','xpu-pd'):
        scope='environment="'+env+'",job="sglang-decode",node="'+('dcu2' if env=='dcu-pd' else 'xpu-1')+'",instance="'+instance+'"'
        for offset in range(-65,1,5):
            lines.append('up{'+scope+'} 1 '+str((end+offset)*1000))
            for dp in range(8):
                extra=',mode="decode",pp_rank="0",dp_rank="'+str(dp)+'",tp_rank="'+str(dp)+'"'
                lines.append('sglang:realtime_tokens_total{'+scope+extra+'} '+str(1000+(offset+65)*10)+' '+str((end+offset)*1000))
    client.post('/api/v1/import/prometheus',content='\n'.join(lines)+'\n').raise_for_status()
    client.get('/internal/force_flush').raise_for_status()
    tested=0
    for group in native_rules.build()[0]['groups']:
        for rule in group['rules']:
            if rule['record'].endswith('backend_decode_tokens_per_second') and 'sglang:' in rule['expr']:
                expression=rule['expr'].replace('{environment=', '{instance="'+instance+'",environment=')
                result=client.post('/api/v1/query',data={'query':expression,'time':end,'step':'5s','nocache':1}).json()
                assert result['status']=='success' and len(result['data']['result'])==1
                assert float(result['data']['result'][0]['value'][1])==80
                tested+=1
    assert tested==2
    client.close()


@pytest.mark.skipif(not __import__('os').environ.get('NATIVE_RECORDING_TEST_VM'),reason='Disposable VM required')
def test_actual_vm_fresh_sources_do_not_depend_on_five_second_query_step():
    """Scrape/ingestion phase can leave a healthy latest sample 7–14s old."""
    import uuid
    url=__import__('os').environ['NATIVE_RECORDING_TEST_VM']
    assert url=='http://127.0.0.1:18543'
    scope='environment="freshness-'+uuid.uuid4().hex+'",job="fixture"'
    end=int(time.time()//5)*5-100
    lines=[]
    cases={'seven':(7,1),'fourteen':(14,1),'expired':(16,1),'down':(7,0),'metric_expired':(7,1)}
    for case,(age,up) in cases.items():
        labels=scope+',instance="'+case+'",node="'+case+'"'
        metric_age=16 if case=='metric_expired' else age
        for ago in range(66,metric_age-1,-1):
            if (ago-metric_age)%5:continue
            for metric in ('fixture_events_total','fixture_latency_count','fixture_latency_bucket'):
                tags=labels+(',le="+Inf"' if metric.endswith('_bucket') else '')
                lines.append(f'{metric}{{{tags}}} {1000-ago*2} {(end-ago)*1000}')
        # Zero gauges must remain present; presence filters must not reject them.
        lines.append(f'fixture_gauge{{{labels}}} 0 {(end-metric_age)*1000}')
        lines.append(f'up{{{labels}}} {up} {(end-age)*1000}')
    with httpx.Client(base_url=url,trust_env=False) as client:
        client.post('/api/v1/import/prometheus',content='\n'.join(lines)+'\n').raise_for_status()
        client.get('/internal/force_flush').raise_for_status()
        def query(expr):
            response=client.post('/api/v1/query',data={'query':expr,'time':end,'step':'5s','nocache':1,'latency_offset':'1ms'})
            response.raise_for_status()
            return {r['metric']['node']:float(r['value'][1]) for r in response.json()['data']['result']}
        for counter in (False,True,'increase'):
            values=query(native_rules.source_value('fixture_events_total',scope,counter=counter))
            assert set(values)=={'seven','fourteen'}
        assert query(native_rules.source_value('fixture_gauge',scope))=={'seven':0,'fourteen':0}
        quality=query(native_rules.histogram_quality('fixture_latency',scope))
        assert quality['seven']==quality['fourteen']==0
        assert quality['metric_expired']>0
        available=query('group by(node) ('+native_rules.fresh('up{'+scope+'}')+' == 1)')
        assert set(available)=={'seven','fourteen','metric_expired'}
