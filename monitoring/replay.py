"""Reconstruct exact scrape observations; never interpolate raw counters."""
import bisect,collections,math,json
from .calculator import Calculator
from .cache_metrics import store_metrics,QueryWindow,StoreTierWindow
from .monitor_series import chart_point
from .resource_series import store_resources
META={'job','instance','environment','service','node','role'}
JOBS=('sglang-prefill','sglang-decode','node-prefill','node-decode','dcu-prefill','dcu-decode','mooncake')

def decode_export(lines):
 groups=collections.defaultdict(lambda:collections.defaultdict(dict))
 for obj in lines:
  source=dict(obj['metric']);name=source.pop('__name__');job=source.get('job')
  if job not in JOBS:continue
  labels={k:v for k,v in source.items() if k not in META}
  identity=(name,json.dumps(source,sort_keys=True))
  latency=name.startswith(('sglang:time_to_first_token_seconds_', 'sglang:inter_token_latency_seconds_', 'sglang:e2e_request_latency_seconds_'))
  for ts,value in zip(obj['timestamps'],obj['values']):
   valid=isinstance(value,(int,float)) and math.isfinite(value)
   if not valid and not (latency or name=='up'):continue
   row={'name':name,'labels':labels,'value':value if valid else float('nan')}
   row['source_labels']=source
   existing=groups[job][ts/1000].get(identity)
   if existing is not None and existing['value']!=row['value']:row['value']=float('nan')
   groups[job][ts/1000][identity]=row
 return {job:(sorted(values),{ts:list(rows.values()) for ts,rows in values.items()}) for job,values in groups.items()}

def latest_rows(groups,job,ts):
 times,values=groups.get(job,([],{}));i=bisect.bisect_right(times,ts)-1
 if i<0 or not 0<=ts-times[i]<15:return None,[]
 observed=times[i];rows=values[observed]
 up=[r['value'] for r in rows if r['name']=='up']
 if up and up!=[1]:return None,[]
 return observed,rows

def single(rows,name):
 found=[r['value'] for r in rows if r['name']==name]
 return found[0] if len(found)==1 else None

def host(rows,old,ts,oldts):
 def values(name,label):return {r['labels'][label]:r['value'] for r in rows if r['name']==name}
 h={'memory':{k:single(rows,'node_memory_'+k+'_bytes') for k in ('MemTotal','MemAvailable','SwapTotal','SwapFree')},'disks':{},'network_rates':{},'disk_rates':{},'cpu_percent':None,'load':[single(rows,'node_load'+n) for n in ('1','5','15')]}
 for name in ('data1','data2'):
  used=[r for r in rows if r['labels'].get('mountpoint')=='/'+name]
  h['disks'][name]={'total':single(used,'node_filesystem_size_bytes'),'available':single(used,'node_filesystem_avail_bytes')}
 if old and oldts and 0<ts-oldts<20 and single(rows,'node_boot_time_seconds')==single(old,'node_boot_time_seconds'):
  dt=ts-oldts
  def delta(name,label):
   a={r['labels'][label]:r['value'] for r in old if r['name']==name};b=values(name,label)
   return {k:(v-a[k])/dt for k,v in b.items() if k in a and v>=a[k]}
  before={(r['labels'].get('cpu'),r['labels'].get('mode')):r['value'] for r in old if r['name']=='node_cpu_seconds_total'}
  after={(r['labels'].get('cpu'),r['labels'].get('mode')):r['value'] for r in rows if r['name']=='node_cpu_seconds_total'}
  if before and before.keys()==after.keys() and all(after[k]>=before[k] for k in before):
   changes={k:after[k]-before[k] for k in before if k[1] not in ('guest','guest_nice')};total=sum(changes.values());idle=sum(v for k,v in changes.items() if k[1] in ('idle','iowait'))
   h['cpu_percent']=max(0,min(100,100*(1-idle/total))) if total>0 else None
  for target,prefix,fields in [('network_rates','node_network_',{'rx':'receive_bytes_total','tx':'transmit_bytes_total'}),('disk_rates','node_disk_',{'read':'read_bytes_total','write':'written_bytes_total'})]:
   for key,suffix in fields.items():
    for dev,value in delta(prefix+suffix,'device').items():h[target].setdefault(dev,{})[key]=value
 return h

def gpus(rows,ts):
 observed=single(rows,'dcu_sample_timestamp_seconds')
 if single(rows,'dcu_sample_success')!=1 or observed is None or not 0<=ts-observed<15:return []
 mapping={'dcu_utilization_percent':('HCU use (%)',1),'dcu_memory_used_bytes':('vram Total Used Memory (MiB)',1048576),'dcu_memory_total_bytes':('vram Total Memory (MiB)',1048576),'dcu_temperature_celsius':('Temperature (Sensor junction) (C)',1),'dcu_power_watts':('Average Graphics Package Power (W)',1)}
 out={}
 for r in rows:
  if r['name'] in mapping:
   key,scale=mapping[r['name']];dev=r['labels']['device'];out.setdefault(dev,{'device':dev})[key]=r['value']/scale
 return [out[k] for k in sorted(out)]

def replay(groups,start,end):
 calc=Calculator();store_window=QueryWindow();tier_window=StoreTierWindow();previous={};snapshots=[];points=[]
 for tick in range(int(start//5)*5,int(end//5)*5+1,5):
  snapshot={'ts':tick,'nodes':{},'enabled':True,'retention_hours':720,'interval_seconds':5,'source':'victoriametrics'}
  for role in ('prefill','decode'):
   ts,rows=latest_rows(groups,'sglang-'+role,tick);node={};snapshot['nodes'][role]=node
   if ts is not None:
    key=('metric',role)
    sources={tuple(sorted((k,v) for k,v in r.get('source_labels',{}).items() if k in META)) for r in rows}
    if previous.get(('sources',role))!=sources:
     calc.reset(role)
     previous.pop(key,None)
    previous[('sources',role)]=sources
    if previous.get(key,(None,))[0]!=ts:previous[key]=(ts,calc.metrics(role,rows,ts))
    data=previous[key][1];node['metrics']={'status':'ok','observed_at':ts,'data':data}
   else:
    calc.reset(role)
    previous.pop(('metric',role),None)
    node['metrics']={'status':'error','observed_at':tick,'error':'模型指标不可用或过期'}
   nts,nrows=latest_rows(groups,'node-'+role,tick);dts,drows=latest_rows(groups,'dcu-'+role,tick)
   if nts is not None:
    key=('host',role);oldts,oldrows,oldhost=previous.get(key,(None,[],None))
    h=oldhost if nts==oldts else host(nrows,oldrows,nts,oldts)
    previous[key]=(nts,nrows,h);devices=gpus(drows,tick)
    node['telemetry']={'status':'ok','observed_at':nts,'data':{'host':h,'gpus':devices,'gpu_error':None if devices else 'DCU 指标不可用或过期'}}
   else:node['telemetry']={'status':'error','observed_at':tick,'error':'主机指标不可用或过期'}
  ts,rows=latest_rows(groups,'mooncake',tick)
  if ts is not None:
   sources={tuple(sorted((k,v) for k,v in r.get('source_labels',{}).items() if k in META)) for r in rows}
   if previous.get('store_sources')!=sources:
    store_window=QueryWindow();tier_window=StoreTierWindow();previous.pop('store',None)
   previous['store_sources']=sources
   if previous.get('store',(None,))[0]!=ts:
    data=store_metrics(rows);data['ts']=ts;data['query_60s']=store_window.add(data);data['tier_query_60s']=tier_window.add(data);previous['store']=(ts,data)
   snapshot['mooncake']={'status':'ok','observed_at':ts,'data':previous['store'][1]}
  else:
   store_window.add(None);tier_window.add(None);previous.pop('store',None)
   snapshot['mooncake']={'status':'error','observed_at':tick,'error':'Mooncake 指标不可用或过期'}
  point=chart_point(snapshot)
  for role in ('prefill','decode'):
   m=snapshot['nodes'][role]['metrics'].get('data',{})
   point['nodes'][role].update(cache_60s=m.get('cache_60s'),hicache=m.get('hicache'))
  m=snapshot['mooncake'].get('data',{});point['mooncake']={k:m.get(k) for k in ('capacity','ssd_capacity','query_60s','tier_query_60s')}
  point['mooncake']['resources']=store_resources(m)
  snapshots.append(snapshot);points.append(point)
 return snapshots,points
