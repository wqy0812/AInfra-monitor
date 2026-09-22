import math
from types import SimpleNamespace
import httpx
import pytest
from monitoring.profile_counters import (
    CounterWindow, NAMES, counter_delta, epoch, histogram_valid, key,
    metric_selector, profile_values, quantile,
)

LABELS = {'request_scope':'all', 'job':'aigate', 'environment':'dcu-pd', 'instance':'gateway:18082', 'backend':'dcu', 'model':'model'}


def identity(name, **extra):
    return key(dict(LABELS, __name__='aigate_'+name, **extra))


def observations(points):
    """points = [(timestamp, <=0.5 count, total count, duration sum)]."""
    return {
        identity('request_duration_seconds_bucket', le='0.5'): [(t,a) for t,a,b,s in points],
        identity('request_duration_seconds_bucket', le='+Inf'): [(t,b) for t,a,b,s in points],
        identity('request_duration_seconds_count'): [(t,b) for t,a,b,s in points],
        identity('request_duration_seconds_sum'): [(t,s) for t,a,b,s in points],
    }


class SamplesWindow(CounterWindow):
    def __init__(self, start, end, lifetimes, samples):
        super().__init__(None, '', start, end)
        self.lifetimes = [(LABELS, NAMES, birth, stop) for birth,stop in lifetimes]
        self.samples = samples

    async def discover(self):
        pass

    async def summaries(self, labels, names, left, right):
        result = {}
        for identity, points in self.samples.items():
            chosen = [(t,v) for t,v in points if left < t <= right]
            if not chosen:
                continue
            result[identity] = {
                'last_over_time': chosen[-1][1],
                'tlast_over_time': chosen[-1][0],
                'min_over_time': min(v for _,v in chosen),
                'first_over_time': chosen[0][1],
                'increase_prometheus': sum(b[1]-a[1] if b[1]>=a[1] else b[1] for a,b in zip(chosen,chosen[1:])),
            }
        return result


def duration(values):
    return {r['labels']['le']:r['value'] for r in values['request_duration_seconds_buckets']}


@pytest.mark.asyncio
async def test_real_failure_shape_never_subtracts_previous_lifetime():
    samples = observations([(1000,1,2,1.2), (1105,2,2,.7), (1110,4,4,1.4)])
    window = await SamplesWindow(1099,1111,[(1100,1111)],samples).read()
    values = profile_values(window)
    assert duration(values) == {'0.5':4, '+Inf':4}
    assert values['request_duration_seconds_mean'][0]['value'] == pytest.approx(.35)
    assert values['request_duration_seconds_p50'][0]['value'] == pytest.approx(.25)
    assert values['request_duration_seconds_p95'][0]['value'] == pytest.approx(.475)
    assert not window.issues


@pytest.mark.asyncio
async def test_window_spanning_reset_sums_only_each_lifetimes_contribution():
    samples = observations([(990,1,2,1.2), (1000,2,3,1.5), (1010,4,4,1.4)])
    window = await SamplesWindow(995,1011,[(900,1007.999),(1008,1011)],samples).read()
    values = profile_values(window)
    assert duration(values) == {'0.5':5, '+Inf':5}
    assert values['request_duration_seconds_mean'][0]['value'] == pytest.approx(1.7/5)
    assert not window.issues


@pytest.mark.asyncio
async def test_restored_counters_keep_origin_and_do_not_double_count():
    samples = observations([(1000,5,5,2), (1005,5,5,2), (1010,9,9,3.4)])
    window = await SamplesWindow(1001,1011,[(900,1011)],samples).read()
    assert duration(profile_values(window)) == {'0.5':4, '+Inf':4}


@pytest.mark.asyncio
async def test_scrape_gap_with_preserved_counter_counts_catchup_once():
    samples = observations([(1000,1,1,.4), (1010,1,1,.4), (1100,5,5,2)])
    window = await SamplesWindow(1001,1101,[(900,1101)],samples).read()
    assert duration(profile_values(window)) == {'0.5':4, '+Inf':4}


@pytest.mark.asyncio
@pytest.mark.parametrize('points,start,end,birth,reason', [
    ([(1105,4,4,1.4)], 1101,1111,900,'missing_boundary_baseline'),
    ([(1000,5,5,2),(1010,2,2,.7)],1001,1011,900,'unexplained_counter_reset'),
    ([(1000,5,5,2)],990,1100,995,'unobserved_lifecycle_tail'),
    ([(1000,5,5,2),(1040,9,9,3)],1030,1041,900,'missing_boundary_baseline'),
])
async def test_unknown_boundaries_and_unexplained_resets_are_not_zero(points,start,end,birth,reason):
    window = await SamplesWindow(start,end,[(birth,end)],observations(points)).read()
    values = profile_values(window)
    assert all(v is None for v in duration(values).values())
    assert values['request_duration_seconds_p95'][0]['value'] is None
    assert any(r==reason for _,r in window.issues)


@pytest.mark.asyncio
async def test_history_before_first_known_lifetime_is_marked_unknown():
    window = await SamplesWindow(990,1111,[(1100,1111)],
        observations([(1000,1,2,1.2),(1105,4,4,1.4)])).read()
    assert all(v is None for v in duration(profile_values(window)).values())
    assert any(r=='missing_lifecycle' for _,r in window.issues)


@pytest.mark.asyncio
async def test_invalid_histogram_hides_distribution_mean_and_percentiles():
    window = await SamplesWindow(1000,1011,[(1001,1011)],
        observations([(1010,3,2,1.4)])).read()
    values = profile_values(window)
    assert duration(values) == {'0.5':None, '+Inf':None}
    for suffix in ['mean','p50','p95','p99']:
        assert values['request_duration_seconds_'+suffix][0]['value'] is None
    assert ('request_duration_seconds','inconsistent_histogram') in window.issues


@pytest.mark.asyncio
async def test_zero_observations_are_valid_but_have_no_percentiles():
    window = await SamplesWindow(1000,1011,[(1001,1011)],
        observations([(1010,0,0,0)])).read()
    values = profile_values(window)
    assert duration(values) == {'0.5':0, '+Inf':0}
    assert values['request_duration_seconds_p95'][0]['value'] is None
    assert not window.issues


@pytest.mark.asyncio
async def test_discovery_normalizes_preserved_epoch_and_ends_group_at_process_reset():
    calls = []
    def respond(request):
        calls.append(request)
        assert request.method == 'GET'
        assert 'count_values_over_time' in request.url.params['query']
        rows = []
        for name,labels,birth in [
            ('group',LABELS,900), ('group',LABELS,1100.0000001),
            ('group',LABELS,1100.0000002),
            ('counter',{'request_scope':'all', 'job':'aigate','environment':'dcu-pd','instance':'gateway:18082'},1050),
            ('group',dict(LABELS,backend='other'),1000),
        ]:
            rows.append({'metric':dict(labels,__name__='aigate_profile_'+name+'_start_time_seconds',profile_epoch=str(birth)), 'value':[1200,'10']})
        return httpx.Response(200,json={'status':'success','data':{'result':rows}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        window = CounterWindow(SimpleNamespace(client=client),'http://vm',990,1200,backend='dcu')
        await window.discover()
    groups = [(birth,end) for labels,_,birth,end in window.lifetimes if labels.get('backend')]
    assert groups == [(900,1049.999),(1100,1200)]
    assert len(calls)==1


def test_trends_omit_reset_boundaries_and_resume_after_clean_window():
    window = SamplesWindow(900,1300,[(900,1049.999),(1100,1300)],{})
    assert not window.trend_valid(1101,60,'requests_routed_total')
    assert not window.trend_valid(1080,60,'requests_routed_total')
    assert not window.trend_valid(1150,60,'requests_routed_total')
    assert window.trend_valid(1200,60,'requests_routed_total')


@pytest.mark.asyncio
async def test_shutdown_gap_keeps_healthy_trends_but_total_remains_unknown():
    samples = {identity('requests_routed_total'): [(900,1),(950,3),(1000,5),(1150,2),(1250,4)]}
    window = await SamplesWindow(901,1251,[(800,1099.999),(1100,1251)],samples).read()
    assert window.aggregate('requests_routed_total') == [{'labels':{},'value':None}]
    assert ('requests_routed_total','unobserved_lifecycle_tail') in window.issues
    assert window.trend_valid(980,60,'requests_routed_total')
    assert not window.trend_valid(1050,60,'requests_routed_total')
    assert not window.trend_valid(1150,60,'requests_routed_total')
    assert window.trend_valid(1200,60,'requests_routed_total')


@pytest.mark.asyncio
async def test_reset_fault_does_not_hide_later_clean_lifetime():
    samples = {identity('requests_routed_total'): [(900,5),(950,2),(1000,3),(1150,2),(1250,4)]}
    window = await SamplesWindow(901,1251,[(800,1001),(1100,1251)],samples).read()
    assert ('requests_routed_total','unexplained_counter_reset') in window.issues
    assert not window.trend_valid(980,60,'requests_routed_total')
    assert window.trend_valid(1200,60,'requests_routed_total')
    assert window.aggregate('requests_routed_total')[0]['value'] is None


def test_selector_escapes_values_and_epoch_uses_milliseconds():
    assert '\\"' in metric_selector(dict(LABELS,model='x"} or up'), NAMES)
    assert epoch(1789267347.8220465)==epoch(1789267347.8220468)
    assert epoch(float('nan')) is None


def test_unknown_contribution_propagates_across_instances():
    window=CounterWindow(None,'',0,1)
    window.totals={identity('requests_routed_total'):4,
        key(dict(LABELS,instance='second',__name__='aigate_requests_routed_total')):None}
    assert window.aggregate('requests_routed_total') == [{'labels':{},'value':None}]

@pytest.mark.asyncio
async def test_other_environment_reset_and_bad_counter_cannot_enter_lifetimes():
    def respond(request):
        assert 'environment="dcu-pd"' in request.url.params['query']
        rows=[{'metric':dict(LABELS,environment=env,__name__='aigate_profile_group_start_time_seconds',profile_epoch=str(birth)),'value':[1200,'1']}
              for env,birth in [('dcu-pd',900),('a3-vllm',1100)]]
        return httpx.Response(200,json={'status':'success','data':{'result':rows}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        window=CounterWindow(SimpleNamespace(client=client),'http://vm',990,1200)
        await window.discover()
    assert len(window.lifetimes)==1
    assert window.lifetimes[0][2]==900
    assert window.trend_valid(1150,60,'requests_routed_total')

@pytest.mark.asyncio
async def test_legacy_first_increment_before_all_request_origin_is_discarded():
    global_labels={**LABELS,'backend':'','model':''}
    group_labels=dict(LABELS)
    class PolicyWindow(CounterWindow):
        async def discover(self):
            self.lifetimes=[(global_labels,NAMES,1100,1200),(group_labels,NAMES,1110,1200)]
        async def segment(self, labels, names, birth, end):
            if labels.get('backend'):
                self.totals[identity('first_increment_seconds_count',request_scope='streaming')]=2
        async def summaries(self, labels, names, left, right):
            if left<1100:
                return {identity('first_increment_seconds_count',request_scope='streaming'):{'last_over_time':999}}
            return {}
    w=await PolicyWindow(None,'',1000,1200).read()
    assert w.aggregate('first_increment_seconds_count')==[{'labels':{},'value':2}]
    assert not w.issues
