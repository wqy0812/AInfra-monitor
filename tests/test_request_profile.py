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


@pytest.mark.asyncio
@pytest.mark.parametrize('environment', ['dcu-pd', 'a3-vllm'])
@pytest.mark.parametrize('affected', ['requests_ended_total', 'requests_routed_total',
                                    'prompt_tokens_total', 'completion_tokens_total'])
@pytest.mark.parametrize('reason', ['missing_boundary_baseline', 'unexplained_counter_reset'])
async def test_trends_isolate_counter_issues_and_preserve_gaps(monkeypatch, environment, affected, reason):
    import httpx
    from types import SimpleNamespace
    from monitoring.profile_counters import CounterWindow, NAMES
    from monitoring.request_profile import metrics

    end = float(int(time.time()) - 30)
    start = end - 300
    birth = start + 100
    stamps = [start + 10, birth + 10, birth + 80, birth + 90, birth + 100]
    window = CounterWindow(None, '', start, end, environment=environment)
    window.lifetimes = [({'backend': 'test'}, NAMES, birth, end)]
    window.issues = {(affected, reason),
                     ('token_pairs_total', 'missing_boundary_baseline')}

    async def read(self):
        window.service, window.vm = self.service, self.vm
        return window

    monkeypatch.setattr(CounterWindow, 'read', read)

    def respond(request):
        if request.url.path.endswith('/query_range'):
            coverage = request.url.params['query'].startswith('min(')
            values = ['1', '1', '1', '0', '1'] if coverage else ['2', '2', '2', '2', '0']
            rows = [{'metric': {}, 'values': list(zip(stamps, values))}]
        else:
            rows = []
        return httpx.Response(200, json={'status': 'success', 'data': {'result': rows}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        data = await metrics(SimpleNamespace(client=client), 'http://vm', start, end,
                             environment=environment)

    for name, rows in data['trends'].items():
        expected = ['NaN'] * 5 if name == affected else ['NaN', 'NaN', '2', 'NaN', '0']
        assert rows[0]['values'] == [[t, v] for t, v in zip(stamps, expected)]
    assert data['quality']['counter_status'] == 'incomplete'
    assert {'metric': affected, 'reason': reason} in data['quality']['counter_issues']
