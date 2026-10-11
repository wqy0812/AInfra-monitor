"""Summary reads native aggregates without raw or legacy fallback."""
import pytest
from fastapi import HTTPException
from monitoring.api import Service,history_expression
from monitoring.native_rules import PREFIX
from monitoring.native_schema import NODES

@pytest.mark.parametrize('environment',NODES)
@pytest.mark.asyncio
async def test_summary_preserves_zero_and_missing_values_without_latest_fallback(environment):
    service=Service(environment);calls=[]
    service.latest_point={'ts':110,'nodes':{'decode':{'decode_tokens':42}}}
    async def query(expr,start,end,step):
        calls.append(expr)
        if PREFIX not in expr:return []
        labels={'environment':environment,'node':NODES[environment]['decode'],'role':'decode'}
        rows=[{'metric':dict(labels,__name__=PREFIX+'backend_decode_tokens_per_second'),'values':[[100,'0']]},
              {'metric':dict(labels,__name__=PREFIX+'backend_expected_instances'),'values':[[100,'1'],[105,'1']]}]
        if expr==history_expression(environment):
            rows.append({'metric':dict(labels,__name__=PREFIX+'scheduler_gauge',field='running',tp_rank='0'),'values':[[100,'7']]})
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
