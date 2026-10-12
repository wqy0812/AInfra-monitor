"""Summary reads native aggregates without raw or legacy fallback."""
import os
import time
import uuid
import httpx
import pytest
from fastapi import HTTPException
from monitoring.api import Service,history_expression
from monitoring.native_rules import PREFIX
from monitoring.native_schema import NODES
from monitoring.raw_gauges import PREFIX as DIRECT_PREFIX
from monitoring import native_schema

@pytest.mark.parametrize('environment',NODES)
@pytest.mark.asyncio
async def test_summary_preserves_zero_and_missing_values_without_latest_fallback(environment):
    service=Service(environment);calls=[]
    service.latest_point={'ts':110,'nodes':{'decode':{'decode_tokens':42}}}
    async def query(expr,start,end,step):
        calls.append(expr)
        if PREFIX not in expr:return []
        labels={'environment':environment,'node':NODES[environment]['decode'],'role':'decode'}
        metric='backend_output_tokens_per_second' if environment=='a3-vllm' else 'backend_decode_tokens_per_second'
        rows=[{'metric':dict(labels,__name__=PREFIX+metric),'values':[[100,'0']]},
              {'metric':dict(labels,__name__=PREFIX+'backend_expected_instances'),'values':[[100,'1'],[105,'1']]}]
        if expr==history_expression(environment):
            rows.append({'metric':dict(labels,__name__=DIRECT_PREFIX+'scheduler_gauge',field='running',tp_rank='0'),'values':[[100,'7']]})
        return rows
    service.query=query
    try:
        full=await service.history(1,100,110)
        summary=await service.history(1,100,110,view='summary')
        assert full['points'][0]['nodes']['decode']['resources']
        assert not summary['points'][0]['nodes']['decode'].get('resources')
        assert summary['points'][0]['nodes']['decode']['decode_tokens']==0
        assert summary['points'][1]['nodes']['decode']['decode_tokens'] is None
        assert 'decode_tokens' in summary['points'][1]['nodes']['decode']['gap_before']
        assert service.latest_point['nodes']['decode']['decode_tokens']==42
        assert all('monitoring_chart_' not in q for q in calls)
        count=len(calls)
        assert await service.history(1,100,110,view='summary') is summary
        assert len(calls)==count
    finally:await service.close()

def test_unknown_view_is_rejected():
    with pytest.raises(HTTPException) as e:history_expression('dcu-pd','typo')
    assert e.value.status_code==400


@pytest.mark.skipif(not os.environ.get('NATIVE_RECORDING_TEST_VM'), reason='Disposable VM required')
def test_actual_vm_summary_preserves_decode_cache_values_and_gaps():
    url = os.environ['NATIVE_RECORDING_TEST_VM']
    assert url == 'http://127.0.0.1:18543'
    start = int(time.time() // 5) * 5 - 500
    fixture = uuid.uuid4().hex
    tags = f'environment="dcu-pd",node="dcu2",role="decode",fixture="{fixture}"'
    samples = []
    for offset, value in [(0, 0), (10, .5), (20, None)]:
        stamp = (start + offset) * 1000
        samples.append(f'{PREFIX}backend_expected_instances{{{tags}}} 1 {stamp}')
        if value is not None:
            samples.append(f'{PREFIX}backend_cache_hit_ratio{{{tags}}} {value} {stamp}')
    with httpx.Client(base_url=url, trust_env=False) as client:
        client.post('/api/v1/import/prometheus', content='\n'.join(samples) + '\n').raise_for_status()
        client.get('/internal/force_flush').raise_for_status()
        for view in ('full', 'summary'):
            response = client.post('/api/v1/query_range', data={'query': history_expression('dcu-pd', view),
                'start': start, 'end': start + 20, 'step': 10, 'nocache': 1, 'latency_offset': '1ms'})
            response.raise_for_status()
            rows = [row for row in response.json()['data']['result'] if row['metric'].get('fixture') == fixture]
            points = native_schema.points(rows, 'dcu-pd', start, start + 20, 10, summary=view == 'summary')
            nodes = [point['nodes']['decode'] for point in points]
            assert [node['cache_60s']['ratio'] for node in nodes] == [0, .5, None]
            assert ['cache' in node['gap_before'] for node in nodes] == [False, False, True]
