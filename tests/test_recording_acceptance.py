import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from monitoring import recording_acceptance as acceptance


@pytest.mark.asyncio
async def test_cold_initial_parity_does_not_poison_final_coverage(tmp_path,monkeypatch):
    catalog=tmp_path/'catalog.json';catalog.write_text(json.dumps({'panels':[{'id':'p','variables':{}}],'steps':[5]}))
    calls=[]
    class Service:
        def __init__(self,*a,**k):pass
        def expression(self,*a):return 'vector(1)'
        async def vm_query(self,*a):return []
        async def stored(self,panel,step,ticks):
            calls.append(ticks)
            return {} if len(calls)==1 else {t:{} for t in ticks}
    monkeypatch.setattr(acceptance,'RecordingService',Service)
    client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
        'status':'success','data':{'result':[]}})))
    monkeypatch.setattr(acceptance.httpx,'AsyncClient',lambda **kwargs:client)
    args=SimpleNamespace(catalog=catalog,state=tmp_path,vm='http://vm',api='http://api',filters=False,require_full=False,coverage_only=False)
    result=await acceptance.check(args)
    assert result['passed'] and len(result['checks'])==1
    assert result['checks'][0]['native_ticks']==0
    assert result['coverage'][0]['native_ticks']==2


@pytest.mark.asyncio
async def test_coverage_only_never_claims_parity_and_preserves_failed_report(tmp_path,monkeypatch):
    catalog=tmp_path/'catalog.json';catalog.write_text(json.dumps({'panels':[{'id':'p','variables':{}}],'steps':[5]}))
    class Service:
        def __init__(self,*a,**k):pass
        async def stored(self,*a):return {}
    monkeypatch.setattr(acceptance,'RecordingService',Service)
    result=await acceptance.check(SimpleNamespace(catalog=catalog,state=tmp_path,vm='http://vm',api='http://api',
        filters=False,require_full=False,coverage_only=True))
    assert not result['passed'] and result['checks']==[] and result['error']

@pytest.mark.asyncio
async def test_partial_grid_is_reported_without_a_performance_gate(tmp_path,monkeypatch):
    catalog=tmp_path/'catalog.json';catalog.write_text(json.dumps({'panels':[{'id':'p','variables':{}}],'steps':[5]}))
    class Service:
        def __init__(self,*a,**k):pass
        async def stored(self,panel,step,ticks):return {ticks[-1]:{}}
    monkeypatch.setattr(acceptance,'RecordingService',Service)
    args=SimpleNamespace(catalog=catalog,state=tmp_path,vm='http://vm',api='http://api',filters=False,require_full=False,coverage_only=True)
    result=await acceptance.check(args)
    assert result['passed'] and not result['coverage_complete'] and result['short_rules_producing']==1
    args.require_full=True
    assert not (await acceptance.check(args))['passed']
