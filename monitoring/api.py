"""Read-only JSON adapter over VM records, source gauges and request profiles."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import math
import os
from pathlib import Path
import time
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from .access import client_allowlist
from .query_client import QueryClient
from . import gateway_live, native_schema, raw_gauges
from .native_rules import PREFIX, build, READ_DELAY_SECONDS, MAX_RECORD_AGE_SECONDS

ENVIRONMENTS=tuple(native_schema.NODES)
VM=os.environ.get('VM_URL','http://127.0.0.1:18428')
STATE=Path(os.environ.get('STATE_DIR','/state'))
ALLOWED=set(client_allowlist(os.environ.get('ALLOWED_CLIENTS','127.0.0.1,::1')))
HISTORY_TIMEOUT=8
HISTORY_CACHE_TTL=5
HISTORY_CACHE_SIZE=8
get=native_schema.get
put=native_schema.put


def validate_environment(environment):
    if environment not in ENVIRONMENTS:raise HTTPException(400,'未知监控环境')
    return environment


def validate_view(view):
    if view not in ('full','summary'):raise HTTPException(400,'未知监控视图')
    return view


def history_expression(environment,view='full'):
    validate_environment(environment);validate_view(view)
    # Computed metrics keep their bounded record read, with no raw fallback.
    # Instantaneous gauges always read sources, excluding all retired copies.
    names=PREFIX+'.*' if view=='full' else PREFIX+'(backend_(requests_per_second|output_tokens_per_second|decode_tokens_per_second|latency_seconds|latency_samples|cache_hit_ratio|available_instances|expected_instances)|host_cpu_.*|prefix_.*|prefill_.*|native_prefix_hit_ratio|store_(used_bytes|total_bytes|usage_ratio|query_.*|quality_issues)|histogram_quality_issues|cache_quality_issues)'
    retired=PREFIX+'('+'|'.join(sorted(raw_gauges.FAMILIES))+')'
    records='last_over_time({__name__=~"'+names+'",__name__!~"'+retired+'",environment="'+environment+'"}[5s])'
    return 'union('+records+', '+raw_gauges.query(environment,summary=view=='summary')+')'


class Service:
    def __init__(self,environment='dcu-pd'):
        self.environment=validate_environment(environment)
        self.client=QueryClient();self.slots=asyncio.Semaphore(2)
        self.latest={'environment':environment,'ts':None,'nodes':{},'source':'victoriametrics'}
        self.latest_point=None;self.error=None;self.watermark=0;self.started=time.time()
        self.cache=OrderedDict();self.history_tasks={}

    async def query(self,expr,start,end,step):
        r=await self.client.post(VM+'/api/v1/query_range',data={'query':expr,'start':start,'end':end,'step':step,'latency_offset':'1ms'})
        r.raise_for_status();data=r.json()
        if data.get('status')!='success' or data.get('isPartial'):raise ValueError('VM query failed')
        return data['data']['result']

    async def cycle(self):
        async with self.client.scope():
            # Wait for source ingestion, independent rule groups and VM writes.
            end=int((time.time()-READ_DELAY_SECONDS)//5)*5
            rows=await self.query(history_expression(self.environment),end-10,end,5)
            points=native_schema.points(rows,self.environment,end-10,end,5)
            if not points:raise ValueError('Monitoring data unavailable')
            point=points[-1]
            latest=native_schema.snapshot(point)
            try:
                r=await self.client.get(VM+'/api/v1/query',params={'query':'{__name__=~"vm_free_disk_space_bytes|vmagent_remotewrite_pending_data_bytes"}'})
                r.raise_for_status();values={}
                for row in r.json()['data']['result']:values.setdefault(row['metric']['__name__'],[]).append(float(row['value'][1]))
                free=min(values['vm_free_disk_space_bytes']) if values.get('vm_free_disk_space_bytes') else None
                pending=sum(values['vmagent_remotewrite_pending_data_bytes']) if values.get('vmagent_remotewrite_pending_data_bytes') else None
                latest['infrastructure']={'disk_free_bytes':free,'pending_bytes':pending,'observed_at':time.time(),'status':'ok' if free is not None and free>20*1024**3 and pending==0 else 'warning'}
            except (httpx.HTTPError,ValueError,KeyError):latest['infrastructure']={'status':'unavailable'}
            self.latest_point=point;self.latest=latest
            self.watermark=point['ts'];self.error=None

    async def run(self):
        while True:
            started=time.monotonic()
            try:await self.cycle()
            except Exception as error:self.error=type(error).__name__+': '+str(error)[:200]
            await asyncio.sleep(max(.1,5-(time.monotonic()-started)))

    async def history(self,hours,start=None,end=None,view='full'):
        validate_view(view)
        end=int((end or time.time())//5)*5;start=start if start is not None else end-hours*3600
        step=max(5,math.ceil((end-start)/719/5)*5)
        key=(start,end,step,hours,view);now=time.monotonic()
        for old_key,(created,_) in list(self.cache.items()):
            if now-created>=HISTORY_CACHE_TTL:del self.cache[old_key]
        cached=self.cache.get(key)
        if cached:self.cache.move_to_end(key);return cached[1]
        task=self.history_tasks.get(key)
        if task is None:
            task=asyncio.create_task(self._cached_history(key));self.history_tasks[key]=task
            task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        return await asyncio.shield(task)

    async def _cached_history(self,key):
        start,end,step,hours,view=key
        try:
            async with asyncio.timeout(HISTORY_TIMEOUT),self.client.scope():value=await self._history(hours,start,end,step,view)
            self.cache[key]=(time.monotonic(),value);self.cache.move_to_end(key)
            while len(self.cache)>HISTORY_CACHE_SIZE:self.cache.popitem(last=False)
            return value
        finally:self.history_tasks.pop(key,None)

    async def _history(self,hours,start,end,step,view):
        async with self.slots:
            tasks=[asyncio.create_task(self.query(history_expression(self.environment,view),start,end,step)),
                   asyncio.create_task(gateway_live.history(self.query,self.environment,start,end,step))]
            try:rows,gateway=await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        points=native_schema.points(rows,self.environment,start,end,step,summary=view=='summary')
        by_time={p['ts']:p for p in points}
        for ts in gateway[0]:by_time.setdefault(ts,native_schema.empty_point(self.environment,ts))
        points=[by_time[t] for t in sorted(by_time)]
        gateway_live.attach(points,gateway[0],step)
        return {'environment':self.environment,'hours':hours,'stride':step//5,'points':points,
            'source':'victoriametrics','retention_hours':720,'gateway_status':gateway[1],'metric_policy':native_schema.POLICY}

    async def close(self):
        tasks=list(self.history_tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.history_tasks.clear();self.cache.clear();await self.client.aclose()


@asynccontextmanager
async def lifespan(app):
    app.state.services={env:Service(env) for env in ENVIRONMENTS}
    app.state.service=app.state.services['dcu-pd']
    tasks=[asyncio.create_task(s.run()) for s in app.state.services.values()]
    try:yield
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for service in app.state.services.values():await service.close()


app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None)
app.add_middleware(GZipMiddleware,minimum_size=1024,compresslevel=1)


@app.middleware('http')
async def restrict(request,call_next):
    if request.client.host not in ALLOWED:return JSONResponse({'detail':'Forbidden'},403)
    return await call_next(request)


@app.get('/health')
async def health(request:Request):
    environments={}
    for env,service in request.app.state.services.items():
        ready=(not service.error and 0<=time.time()-service.watermark<MAX_RECORD_AGE_SECONDS and
               all(service.latest.get('nodes',{}).get(role,{}).get('metrics',{}).get('status')=='ok'
                   for role in ('prefill','decode')))
        environments[env]={'status':'ok' if ready else 'degraded','error':service.error,'processed_at':service.watermark,
            'coverage':{r:n.get('coverage') for r,n in service.latest.get('nodes',{}).items()}}
    return {'status':'ok' if all(e['status']=='ok' for e in environments.values()) else 'degraded',
        'environments':environments,'metric_policy':native_schema.POLICY,'native_recording':build()[1],
        'metric_writer':False,'perses_proxy':False}

def selected_service(request,environment):
 validate_environment(environment)
 return request.app.state.services[environment]

@app.get('/api/monitoring/latest')
async def latest(request:Request,environment:str='dcu-pd'):
 s=selected_service(request,environment)
 if s.error or not s.latest.get('ts') or time.time()-s.latest['ts']>=MAX_RECORD_AGE_SECONDS:raise HTTPException(503,'监控数据暂不可用或过期')
 return s.latest
@app.get('/api/monitoring/history')
async def history(request:Request,hours:float=1,start:float|None=None,end:float|None=None,environment:str='dcu-pd',view:str='full'):
 validate_view(view)
 s=selected_service(request,environment)
 now=time.time()
 if not math.isfinite(hours) or not 0<hours<=720:raise HTTPException(400,'时间范围必须为0到720小时')
 if start is not None or end is not None:
  if start is None or end is None or not all(math.isfinite(x) for x in (start,end)) or not now-720*3600-120<=start<end<=now+5:raise HTTPException(400,'时间范围无效')
 try:
  async with asyncio.timeout(HISTORY_TIMEOUT):return await s.history(hours,start,end,view=view)
 except (TimeoutError,httpx.HTTPError,ValueError):raise HTTPException(503,'监控历史查询暂不可用')

from .request_profile import router as request_profile_router
app.include_router(request_profile_router)
