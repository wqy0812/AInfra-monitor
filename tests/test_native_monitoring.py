"""Approved semantics, read-only API boundary and direct-VM publication."""
import asyncio
import json
from pathlib import Path
import sys
import time
import httpx
import pytest
from monitoring import api,native_rules,native_schema

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'perses'))


def row(metric,value,ts=100,**labels):
    return {'metric':{'__name__':native_rules.PREFIX+metric,'environment':'a3-vllm','node':'a3-1','role':'prefill',**labels},'values':[[ts,str(value)]]}


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


def test_rules_read_raw_only_and_have_one_fixed_interval():
    config,meta=native_rules.build()
    assert meta['window_seconds']==60 and meta['rule_count']>87
    for group in config['groups']:
        assert group['interval']=='5s'
        for rule in group['rules']:
            assert native_rules.PREFIX not in rule['expr']
            assert 'monitoring_chart_' not in rule['expr'] and '$' not in rule['expr']
            assert len(rule['expr'])<1200
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
            return httpx.Response(200,json={'status':'success','data':{'result':[row('backend_requests_per_second',2,990),row('backend_available_instances',4,990),row('backend_expected_instances',4,990)]}})
        return httpx.Response(200,json={'status':'success','data':{'result':[]}})
    service=api.Service('a3-vllm');await service.client.aclose()
    from monitoring.query_client import QueryClient
    service.client=QueryClient(transport=httpx.MockTransport(handler))
    try:
        await service.cycle()
        assert service.watermark==990 and service.latest['nodes']['prefill']['metrics']['data']['rates']['requests']==2
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


@pytest.mark.skipif(not __import__('os').environ.get('NATIVE_RECORDING_TEST_VM'),reason='Disposable VM required')
def test_actual_vm_preserves_all_eight_independent_decode_groups():
    url=__import__('os').environ['NATIVE_RECORDING_TEST_VM']
    assert url=='http://127.0.0.1:18543'
    end=int(time.time()//5)*5-100
    client=httpx.Client(base_url=url,trust_env=False)
    lines=[]
    for env in ('dcu-pd','xpu-pd'):
        scope='environment="'+env+'",job="sglang-decode",node="'+('dcu2' if env=='dcu-pd' else 'xpu-1')+'",instance="fixture-native-dp"'
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
                result=client.post('/api/v1/query',data={'query':rule['expr'],'time':end,'step':'5s','nocache':1}).json()
                assert result['status']=='success' and len(result['data']['result'])==1
                assert float(result['data']['result'][0]['value'][1])==80
                tested+=1
    assert tested==2
    client.close()
