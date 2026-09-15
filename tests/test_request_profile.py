import time
import pytest
from fastapi import HTTPException
from monitoring.request_profile import selector, validate_window, finite

def test_selector_quotes_labels_and_locks_job():
    s=selector('requests_routed_total','x"} or up{a="','model')
    assert s.startswith('aigate_requests_routed_total{job="aigate",')
    assert '\\"' in s

def test_window_and_missing_values():
    now=time.time();validate_window(now-3600,now)
    for start,end in [(now,now),(now-31*86400,now),(float('nan'),now)]:
        with pytest.raises(HTTPException):validate_window(start,end)
    assert finite('NaN') is None
    assert finite('0') == 0

@pytest.mark.asyncio
@pytest.mark.parametrize('environment', ['dcu-pd', 'a3-vllm'])
async def test_every_query_is_scoped_including_quality(environment):
    import httpx
    from types import SimpleNamespace
    from monitoring.request_profile import metrics
    queries=[]
    def respond(request):
        q=request.url.params['query'];queries.append(q)
        assert 'environment="'+environment+'"' in q
        assert 'job="aigate"' in q
        return httpx.Response(200,json={'status':'success','data':{'result':[]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        now=time.time();data=await metrics(SimpleNamespace(client=client),'http://vm',now-300,now,environment=environment)
    assert data['environment']==environment
    assert len(queries)==8
    assert data['quality']['scrape_up'] is None
    assert data['values']['prompt_tokens_total']==[]


def test_invalid_environment_rejected():
    with pytest.raises(HTTPException) as exc:
        selector('requests_routed_total',environment='all')
    assert exc.value.status_code==400
