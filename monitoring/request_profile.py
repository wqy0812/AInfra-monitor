"""Fixed VM queries for gateway request profiles. No events or profile database."""
import asyncio
import json
import math
import time
import httpx
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()
from .profile_counters import CounterWindow, profile_values, HISTOGRAMS, COUNTERS


def validate_environment(environment):
    if environment not in ('dcu-pd', 'a3-vllm'):
        raise HTTPException(400, '未知画像环境')
    return environment


def selector(metric, backend='', model='', environment='dcu-pd'):
    scope = 'streaming' if metric.startswith('first_increment_seconds') else 'all'
    labels = ['job="aigate"', 'request_scope=' + json.dumps(scope), 'environment=' + json.dumps(validate_environment(environment))]
    for key, value in (('backend', backend), ('model', model)):
        if value:
            labels.append(key + '=' + json.dumps(value, ensure_ascii=False))
    return 'aigate_' + metric + '{' + ','.join(labels) + '}'


def validate_window(start, end, backend='', model=''):
    now = time.time()
    if not all(math.isfinite(x) for x in (start, end)) or not now-30*86400 <= start < end <= now+5:
        raise HTTPException(400, '时间范围必须在最近 30 天内')
    if len(backend) > 128 or len(model) > 256:
        raise HTTPException(400, '筛选条件过长')


def finite(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def vector(data):
    return [{'labels': x.get('metric', {}), 'value': finite(x['value'][1])}
            for x in data.get('result', [])]


async def metrics(service, vm, start, end, backend='', model='', environment='dcu-pd'):
    validate_window(start, end, backend, model)
    scope = '{job="aigate",environment=' + json.dumps(validate_environment(environment)) + '}'
    metric_scope = scope[:-1] + ',request_scope="all"}'
    span = str(end-start) + 's'
    sel = lambda name: selector(name, backend, model, environment)
    window = await CounterWindow(service, vm, start, end, backend, model, environment).read()
    values = profile_values(window, backend, model)
    expressions = {
        'sources': 'group by(backend,model)(aigate_requests_routed_total' + metric_scope + ')' ,
        'scrape_up': 'min(min_over_time(up' + scope + '[' + span + ']))',
        'index_ready': 'min(min_over_time(aigate_profile_index_ready' + metric_scope + '[' + span + ']))',
    }
    async def query(key, expression):
        rows = await window.query(expression, end)
        return key, vector({'result': rows})
    values.update(dict(await asyncio.gather(*(query(k,e) for k,e in expressions.items()))))
    if not window.lifetimes and values.get('sources'):
        window.issues.add(('all', 'missing_lifecycle'))
    for field in ('prompt_tokens','completion_tokens'):
        known = next((r['value'] for r in values.get('usage_known',[]) if r['labels'].get('field')==field), None)
        if known is None or known==0:values[field+'_total']=[]
    def scalar(key):
        a = values.get(key, [])
        return a[0]['value'] if a else None
    numerator, denominator = scalar('cached_tokens_total'), scalar('cache_prompt_tokens_total')
    ratio = numerator/denominator if numerator is not None and denominator and 0 <= numerator <= denominator else None
    step = max(5, math.ceil((end-start)/719/5)*5)
    trends = {}
    for name in ('requests_routed_total', 'prompt_tokens_total', 'completion_tokens_total'):
        expression = 'sum(rate(' + sel(name) + '[' + str(max(60, step)) + 's]))'
        r = await service.client.get(vm+'/api/v1/query_range', params={'query':expression,'start':start,'end':end,'step':step,'latency_offset':'1ms','nocache':'1'})
        r.raise_for_status()
        trends[name] = r.json()['data']['result']
    # Rates remain ordinary rolling rates only where their entire lookbehind
    # belongs to a known lifetime. Reset/birth and scrape-gap windows stay blank.
    width = max(60, step)
    r = await service.client.get(vm+'/api/v1/query_range', params={
        'query':'min(min_over_time(up' + scope + '['+str(width+15)+'s]))',
        'start':start,'end':end,'step':step,'latency_offset':'1ms','nocache':'1'})
    r.raise_for_status()
    coverage = {float(t):finite(v) for row in r.json()['data']['result'] for t,v in row['values']}
    for name, rows in trends.items():
        for row in rows:
            row['values'] = [[t,v if window.trend_valid(float(t),width,name) and coverage.get(float(t))==1 else 'NaN']
                             for t,v in row['values']]
    invalid_histograms = sorted({name for name,reason in window.issues if reason=='inconsistent_histogram'})
    notes = ['请求量、画像、Token、总耗时和结果统计包含流式及非流式；无法解析的入口拒绝不计入画像。',
             '网关首增量仅统计观察到有效增量的流式请求，其样本数与总请求数不同。',
             'Token 和总耗时按完成时计入；筛选模型/后端后的请求量为已选路请求。',
             '前缀再次出现的前驱可位于所选窗口之前，最多回看一小时；不等于缓存命中。',
             '统计按计数器生命周期分段；抓取边界存在近似，跨生命周期或缺测的速率留空。']
    if window.issues:
        notes.append('部分统计不完整：缺少边界观测或计数器存在异常，相关值显示为 —。')
    if invalid_histograms:
        notes.append('统计异常：部分累计桶与总数不一致，相关分布、均值和分位数暂不可用。')
    return {'schema': 'gateway-profile-v1', 'request_scope': 'all', 'scope_policy': 'metric-v2', 'metric_scopes': {'first_increment_seconds': 'streaming'}, 'source': 'victoriametrics', 'counter_method': 'lifecycle-v2', 'start': start, 'end': end,
            'computed_at': time.time(), 'environment': environment, 'backend': backend, 'model': model, 'values': values,
            'cache_token_ratio': ratio, 'trends': trends, 'step': step,
            'time_basis': {'requests': 'arrival_or_routing', 'tokens_and_latency': 'completion'},
            'prefix_basis': 'observed_preceding_hour_messages',
            'quality': {'scrape_up': scalar('scrape_up'), 'index_ready': scalar('index_ready'),
                        'dropped_events': scalar('dropped_events'), 'write_errors': scalar('write_errors'),
                        'index_evictions': scalar('index_evictions'),
                        'counter_status': 'incomplete' if window.issues else 'ok',
                        'counter_issues': [{'metric':n,'reason':r} for n,r in sorted(window.issues)],
                        'invalid_histograms': invalid_histograms,
                        'scrape_boundary_approximation': True, 'latest_samples_may_be_incomplete': end>time.time()-30},
            'notes': notes}



@router.get('/api/request-profile/metrics')
async def profile_metrics(request: Request, start: float, end: float, backend: str = '', model: str = '', environment: str = 'dcu-pd'):
    from .api import VM
    validate_environment(environment)
    service = request.app.state.service
    validate_window(start, end, backend, model)
    # Reuse the service's global admission limit. Profile queries never materialize into VM.
    try:
        async with asyncio.timeout(20):
            async with service.slots:
                return await metrics(service, VM, start, end, backend, model, environment)
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError) as exc:
        import logging
        logging.getLogger(__name__).warning('Profile VM query failed: %s', exc)
        raise HTTPException(503, 'VM 请求画像暂不可用')
