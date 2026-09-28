"""Independent VM-backed query API and bounded chart materialization."""
import asyncio,contextlib,json,math,os,time
from collections import OrderedDict
from pathlib import Path
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI,HTTPException,Request
from .replay import decode_export,replay
from .resource_series import resource_paths
from . import a3, gateway_live, host_cpu, xpu, xpu_cache
from .latency import PATHS as LATENCY_PATHS
from .request_scope import PATH_REGEX as REQUEST_PATH_REGEX, SCHEMA as REQUEST_SCHEMA, is_request_path
from .perses_acceleration import AccelerationService, install_routes as install_perses_routes
ENVIRONMENTS=("dcu-pd", "a3-vllm", "xpu-pd")
HISTORY_CACHE_SIZE=8
HISTORY_CACHE_TTL=5
HISTORY_TIMEOUT=8
def validate_environment(environment):
 if environment not in ENVIRONMENTS:raise HTTPException(400,"未知监控环境")
 return environment
VM=os.environ.get('VM_URL','http://127.0.0.1:18428')
STATE=Path(os.environ.get('STATE_DIR','/state'))
ALLOWED=set(os.environ.get('ALLOWED_CLIENTS','127.0.0.1').split(','))
PATHS=[]
for role in ('prefill','decode'):
 for field in ['cpu','requests','output_tokens','decode_tokens','rate_interval_seconds','latency_window_seconds']+['percentiles.'+kind+'.'+q for kind in ('ttft','itl','e2e') for q in ('p50','p95','p99','samples')]+['cache_60s.'+k for k in ('ratio','device','host','storage','window_seconds','input_tokens','hit_tokens')]+['hicache.representative.'+k for k in ('used','total','ratio')]:PATHS.append('nodes.'+role+'.'+field)
PATHS += ['mooncake.capacity.'+k for k in ('used','total','ratio')]+['mooncake.query_60s.'+k for k in ('ratio','window_seconds','valid','total')]
PATHS += ['mooncake.ssd_capacity.'+k for k in ('used','total','ratio')]+['mooncake.tier_query_60s.'+k for k in ('memory','ssd','memory_hits','ssd_hits','total','window_seconds')]
STORE_GAPS={'capacity':'capacity.ratio','ssd_capacity':'ssd_capacity.ratio','memory_query':'tier_query_60s.memory','ssd_query':'tier_query_60s.ssd','query_60s.ratio':'query_60s.ratio'}
def get(obj,path):
 for key in path.split('.'):
  if not isinstance(obj,dict):return None
  obj=obj.get(key)
 return obj

def put(obj,path,value):
 keys=path.split('.')
 for key in keys[:-1]:obj=obj.setdefault(key,{})
 obj[keys[-1]]=value

def encode(points,environment="dcu-pd",latency_only=False):
 validate_environment(environment)
 lines=[]
 paths=sorted(set(PATHS+(["nodes."+r+".cache_60s.external_ratio" for r in ("prefill","decode")]+list(host_cpu.PATHS) if environment=="a3-vllm" else [])).union(*(resource_paths(p) for p in points)))
 if latency_only:
  assert environment=="dcu-pd"
  paths=LATENCY_PATHS
 for p in points:
  for path in paths:
   value=get(p,path);valid=isinstance(value,(int,float)) and math.isfinite(value)
   cpu=environment=='a3-vllm' and path in host_cpu.PATHS
   schema=host_cpu.SCHEMA if cpu else REQUEST_SCHEMA if is_request_path(path) else 'v1'
   label='{path='+json.dumps(path)+',environment='+json.dumps(environment)+',schema='+json.dumps(schema)
   if cpu:label+=',node='+json.dumps(host_cpu.PATHS[path])
   label+='}'
   lines.append('monitoring_chart_value'+label+' '+str(value if valid else 0)+' '+str(int(p['ts']*1000)))
   lines.append('monitoring_chart_valid'+label+' '+str(int(valid))+' '+str(int(p['ts']*1000)))
 return '\n'.join(lines)+'\n'

def validate_view(view):
 if view not in ('full','summary'):raise HTTPException(400,'未知监控视图')
 return view

def summary_point(point):
 """Keep materialized aggregates and gap semantics, excluding resource detail."""
 for root in ('nodes.prefill','nodes.decode','mooncake'):
  obj=get(point,root)
  if isinstance(obj,dict):
   obj.pop('resources',None)
   if 'gap_before' in obj:obj['gap_before']=[key for key in obj['gap_before'] if not key.startswith('resources.')]
 return point

def history_expression(environment,kind,step,view='full'):
 validate_view(view)
 scope='environment='+json.dumps(environment)
 bases=['{'+scope+',schema="v1",path!~'+json.dumps(REQUEST_PATH_REGEX)+'}',
        '{'+scope+',schema='+json.dumps(REQUEST_SCHEMA)+',path=~'+json.dumps(REQUEST_PATH_REGEX)+'}']
 if environment=='a3-vllm':
  bases[0]=bases[0][:-1]+',path!~'+json.dumps(host_cpu.PATH_REGEX)+'}'
  bases.append('{'+scope+',schema='+json.dumps(host_cpu.SCHEMA)+',path=~'+json.dumps(host_cpu.PATH_REGEX)+'}')
 def expression(base):
  if view=='summary':base=base[:-1]+',path!~".*\\\\.resources\\\\..*"}'
  metric='monitoring_chart_'+('value' if kind=='value' else 'valid')+base
  if kind!='gaps':return 'default_rollup('+metric+'[5s])'
  return 'min_over_time('+metric+'['+str(step)+'s]) * (count_over_time('+metric+'['+str(step)+'s]) >= bool '+str(step//5)+')'
 return ' or '.join('('+expression(base)+')' for base in bases)

class Service:
 def __init__(self,environment="dcu-pd"):
  self.environment=validate_environment(environment)
  self.paths=sorted(set(PATHS+(["nodes."+role+".cache_60s.external_ratio" for role in ("prefill","decode")]+list(host_cpu.PATHS) if environment=="a3-vllm" else [])))
  self.watermark_file=STATE/("watermark-"+REQUEST_SCHEMA+"-"+environment+".json")
  self.replay=a3.replay if environment=="a3-vllm" else xpu.replay if environment=="xpu-pd" else replay
  self.client=httpx.AsyncClient(trust_env=False,timeout=10,limits=httpx.Limits(max_connections=8))
  self.latest={'environment':self.environment,'ts':None,'nodes':{},'enabled':True,'source':'victoriametrics'};self.error=None;self.slots=asyncio.Semaphore(2)
  self.latest_point=None;self.watermark=0;self.started=time.time();self.cache=OrderedDict();self.history_tasks={};self.host_cpu_status=None
  if self.watermark_file.exists():self.watermark=json.loads(self.watermark_file.read_text())['ts']
 async def raw(self,start,end):
  selector='{job=~"sglang-prefill|sglang-decode|node-prefill|node-decode|dcu-prefill|dcu-decode|mooncake"}'
  selector=selector[:-1]+',environment="dcu-pd"}' if self.environment=='dcu-pd' else '{environment="a3-vllm",job="vllm-a3",__name__=~"up|vllm:(request_success_total|generation_tokens_total|prefix_cache_(hits|queries)_total|external_prefix_cache_(hits|queries)_total|num_requests_(running|waiting)|kv_cache_usage_perc|(time_to_first_token|inter_token_latency|e2e_request_latency)_seconds_(bucket|count))"}'
  if self.environment=='xpu-pd':selector='{environment="xpu-pd",job=~"sglang-prefill|sglang-decode"}'
  r=await self.client.get(VM+'/api/v1/export',params={'match[]':selector,'start':start,'end':end,'reduce_mem_usage':1});r.raise_for_status()
  return (a3.decode_export if self.environment=='a3-vllm' else decode_export)(json.loads(line) for line in r.text.splitlines() if line)
 async def cycle(self):
  end=int((time.time()-3)//5)*5
  if not self.watermark:self.watermark=end-5
  start=max(self.watermark-80,end-30*86400)
  until=min(end,self.watermark+300)
  groups=await self.raw(start,until)
  replay_options={"emit_start":self.watermark+5} if self.environment=="a3-vllm" else {}
  replay_task=asyncio.to_thread(self.replay,groups,start,until,**replay_options)
  if self.environment=='a3-vllm':
   (snaps,points),(cpu_values,cpu_status)=await asyncio.gather(replay_task,host_cpu.collect(self.client,VM,self.watermark+5,until))
   host_cpu.attach(snaps,points,cpu_values,cpu_status)
   self.host_cpu_status=cpu_status
  else:snaps,points=await replay_task
  selected=[p for p in points if p['ts']>self.watermark]
  if selected:
   r=await self.client.post(VM+'/api/v1/import/prometheus',content=encode(selected,self.environment));r.raise_for_status()
   self.watermark=selected[-1]['ts'];STATE.mkdir(parents=True,exist_ok=True)
   temp=self.watermark_file.with_suffix('.tmp');temp.write_text(json.dumps({'ts':self.watermark}));temp.replace(self.watermark_file)
  if snaps:self.latest={**snaps[-1],'environment':self.environment};self.latest_point=points[-1]
  try:
   r=await self.client.get(VM+'/api/v1/query',params={'query':'{__name__=~"vm_free_disk_space_bytes|vmagent_remotewrite_pending_data_bytes"}'})
   r.raise_for_status();values=r.json()['data']['result']
   found={}
   for item in values:
    name=item['metric']['__name__'];found.setdefault(name,[]).append(float(item['value'][1]))
   free=min(found['vm_free_disk_space_bytes']) if found.get('vm_free_disk_space_bytes') else None
   pending=sum(found['vmagent_remotewrite_pending_data_bytes']) if found.get('vmagent_remotewrite_pending_data_bytes') else None
   self.latest['infrastructure']={'disk_free_bytes':free,'pending_bytes':pending,'observed_at':time.time(),'status':'ok' if free is not None and free>20*1024**3 and pending==0 else 'warning'}
  except (httpx.HTTPError,ValueError,KeyError):self.latest['infrastructure']={'status':'unavailable'}
  self.error=None
 async def run(self):
  while True:
   started=time.monotonic()
   try:await self.cycle()
   except Exception as e:self.error=type(e).__name__+': '+str(e)[:200]
   # Five seconds between cycle starts, not five seconds after expensive work.
   interval=1 if self.watermark<time.time()-20 else 5
   await asyncio.sleep(5 if self.error else max(.1,interval-(time.monotonic()-started)))
 async def query(self,expr,start,end,step):
  r=await self.client.get(VM+'/api/v1/query_range',params={'query':expr,'start':start,'end':end,'step':step});r.raise_for_status();data=r.json()
  if data.get('status')!='success':raise ValueError('VM query failed')
  return data['data']['result']
 async def history(self,hours,start=None,end=None,view='full'):
  validate_view(view)
  end=int((end or time.time())//5)*5;start=start if start is not None else end-hours*3600
  step=max(5,math.ceil((end-start)/719/5)*5)
  key=(start,end,step,hours,view)
  now=time.monotonic()
  for old_key,(created,_) in list(self.cache.items()):
   if now-created>=HISTORY_CACHE_TTL:del self.cache[old_key]
  cached=self.cache.get(key)
  if cached:
   self.cache.move_to_end(key);return cached[1]
  task=self.history_tasks.get(key)
  if task is None:
   task=asyncio.create_task(self._cached_history(key))
   self.history_tasks[key]=task
   # Retrieve failures even if every HTTP caller has disconnected.
   task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
  return await asyncio.shield(task)

 async def _cached_history(self,key):
  start,end,step,hours,view=key
  try:
   async with asyncio.timeout(HISTORY_TIMEOUT):
    value=await self._history(hours,start,end,step,view)
   self.cache[key]=(time.monotonic(),value)
   self.cache.move_to_end(key)
   while len(self.cache)>HISTORY_CACHE_SIZE:self.cache.popitem(last=False)
   return value
  finally:
   self.history_tasks.pop(key,None)

 async def close(self):
  tasks=list(self.history_tasks.values())
  for task in tasks:task.cancel()
  await asyncio.gather(*tasks,return_exceptions=True)
  self.history_tasks.clear();self.cache.clear()
  await self.client.aclose()

 async def _history(self,hours,start,end,step,view):
  async with self.slots:
   queries=[asyncio.create_task(self.query(history_expression(self.environment,kind,step,view),start,end,step)) for kind in ('value','valid','gaps')]
   queries.append(asyncio.create_task(gateway_live.history(self.query,self.environment,start,end,step)))
   try:
    values,valid,gaps,gateway_result=await asyncio.gather(*queries)
   finally:
    for query in queries:
     if not query.done():query.cancel()
    await asyncio.gather(*queries,return_exceptions=True)
   xpu_cache_result=await xpu_cache.history(self.query,start,end,step) if self.environment=='xpu-pd' else None
  gateway,gateway_status=gateway_result
  def index(series):return {(s['metric']['path'],float(ts)):float(v) for s in series for ts,v in s['values']}
  vals,oks,mins=index(values),index(valid),index(gaps);points=[]
  backend_timestamps={ts for path,ts in vals}
  timestamps=sorted(backend_timestamps|set(gateway))
  paths=sorted(set(self.paths)|{path for path,ts in vals if any(path.startswith(root+'.resources.') for root in ('nodes.prefill','nodes.decode','mooncake'))})
  for ts in timestamps:
   p={'environment':self.environment,'ts':ts,'nodes':{'prefill':{},'decode':{}},'source':'victoriametrics'}
   for path in paths:put(p,path,vals.get((path,ts)) if oks.get((path,ts))==1 else None)
   for role in ('prefill','decode'):
    node=p['nodes'][role];node['cache_60s']['semantics']='vllm-prefix-token-v1' if self.environment=='a3-vllm' else (xpu_cache.SCHEMA if self.environment=='xpu-pd' and role=='prefill' else 'unavailable' if self.environment=='xpu-pd' else 'prefill-effective-v1' if role=='prefill' else 'request-accounting-v1')
    fields={'requests':'requests','output_tokens':'output_tokens','decode_tokens':'decode_tokens','cpu':'cpu','cache':'cache_60s.ratio','hicache':'hicache.representative.ratio',**{k:'percentiles.'+k+'.p95' for k in ('ttft','itl','e2e')}}
    if self.environment=='a3-vllm':fields['cpu_iowait']='cpu_iowait'
    node['gap_before']=[k for k,path in fields.items() if mins.get(('nodes.'+role+'.'+path,ts))!=1]
   p['mooncake']['gap_before']=[key for key,path in STORE_GAPS.items() if mins.get(('mooncake.'+path,ts))!=1]
   p['mooncake']['tier_query_60s']['semantics']='store-replica-query-v1'
   for root in ('nodes.prefill','nodes.decode','mooncake'):
    obj=get(p,root)
    obj['gap_before'] += [path[len(root)+1:] for path in paths if path.startswith(root+'.resources.') and mins.get((path,ts))!=1]
   points.append(p)
  if self.latest_point and start<=self.latest_point['ts']<=end and (not backend_timestamps or self.latest_point['ts']>max(backend_timestamps)):
   import copy
   p=copy.deepcopy(self.latest_point);p['source']='victoriametrics';p['environment']=self.environment
   for node in p['nodes'].values():node['gap_before']=['requests','output_tokens','decode_tokens','cpu','cache','hicache','ttft','itl','e2e']+(['cpu_iowait'] if self.environment=='a3-vllm' else [])
   p.setdefault('mooncake',{})['gap_before']=list(STORE_GAPS)
   for root in ('nodes.prefill','nodes.decode','mooncake'):
    get(p,root)['gap_before'] += [path[len(root)+1:] for path in resource_paths(p) if path.startswith(root+'.')]
   points=[x for x in points if x['ts']!=p['ts']]+[p]
   points.sort(key=lambda x:x['ts'])
  if xpu_cache_result is not None:xpu_cache.attach(points,xpu_cache_result)
  gateway_live.attach(points,gateway,step)
  if view=='summary':points=[summary_point(p) for p in points]
  value={'environment':self.environment,'hours':hours,'stride':step//5,'points':points,'source':'victoriametrics','retention_hours':720,'gateway_status':gateway_status}
  return value

@asynccontextmanager
async def lifespan(app):
 app.state.services={env:Service(env) for env in ENVIRONMENTS}
 app.state.service=app.state.services['dcu-pd']
 tasks=[asyncio.create_task(s.run()) for s in app.state.services.values()]
 app.state.perses_acceleration=None
 app.state.perses_acceleration_error=None
 catalog_path=os.environ.get('PERSES_ACCELERATION_CATALOG')
 if catalog_path:
  try:
   catalog=json.loads(Path(catalog_path).read_text())
   ready=lambda:all(v.error is None and v.watermark>time.time()-20 for v in app.state.services.values())
   app.state.perses_acceleration=AccelerationService(VM,STATE,catalog,source_ready=ready)
   tasks.append(asyncio.create_task(app.state.perses_acceleration.run()))
  except (OSError,ValueError,KeyError,TypeError) as error:
   app.state.perses_acceleration_error=type(error).__name__+': '+str(error)[:160]
 try:yield
 finally:
  for task in tasks:task.cancel()
  await asyncio.gather(*tasks,return_exceptions=True)
  for s in app.state.services.values():await s.close()
  if app.state.perses_acceleration:await app.state.perses_acceleration.close()
app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None)
install_perses_routes(app)
@app.middleware('http')
async def restrict(request,call_next):
 from fastapi.responses import JSONResponse
 if '*' not in ALLOWED and request.client.host not in ALLOWED:return JSONResponse({'detail':'Forbidden'},403)
 return await call_next(request)
@app.get('/health')
async def health(request:Request):
 s=request.app.state.service
 sources={r: n.get('metrics',{}).get('status')=='ok' and n.get('telemetry',{}).get('status')=='ok' and len(n.get('telemetry',{}).get('data',{}).get('gpus',[]))==8 for r,n in s.latest.get('nodes',{}).items()}
 good=len(sources)==2 and all(sources.values()) and s.latest.get('mooncake',{}).get('status')=='ok'
 environments={env:{'error':v.error,'processed_at':v.watermark,'sources':{role:n.get('metrics',{}).get('status') for role,n in v.latest.get('nodes',{}).items()}} for env,v in request.app.state.services.items()}
 a3_service=request.app.state.services.get('a3-vllm')
 if a3_service:
  environments['a3-vllm']['host_cpu']={'query_status':a3_service.host_cpu_status,'sources':{role:n.get('host_cpu',{}).get('status') for role,n in a3_service.latest.get('nodes',{}).items()}}
 accelerator=getattr(request.app.state,'perses_acceleration',None)
 acceleration=accelerator.status() if accelerator else {'enabled':False,'error':getattr(request.app.state,'perses_acceleration_error',None)}
 return {'status':'ok' if good and not s.error and s.watermark>time.time()-20 else 'degraded','error':s.error,'sources':sources,'processed_at':s.watermark,'started_at':s.started,'environments':environments,'perses_acceleration':acceleration}
def selected_service(request,environment):
 validate_environment(environment)
 return request.app.state.services[environment]

@app.get('/api/monitoring/latest')
async def latest(request:Request,environment:str='dcu-pd'):
 s=selected_service(request,environment)
 if s.error or not s.latest.get('ts') or time.time()-s.latest['ts']>=20:raise HTTPException(503,'监控数据暂不可用或过期')
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
