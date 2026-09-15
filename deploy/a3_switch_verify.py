"""Read-only acceptance of live APIs/VM/Perses; save bounded evidence on the host."""
import json,math,sys,time,urllib.request,urllib.parse,urllib.error
from pathlib import Path
ROOT=Path(sys.argv[1]);MODE=sys.argv[2]
VM='http://127.0.0.1:18428';PROXY='http://122.247.53.162:18431/proxy/projects/dcu-monitoring/datasources/victoriametrics'
BASE='http://127.0.0.1:'+('18430' if MODE=='monitoring' else '18080')
def get(base,path,params=None):
 url=base+path+('?' + urllib.parse.urlencode(params) if params else '')
 with urllib.request.urlopen(url,timeout=15) as r:return json.load(r)
def field(obj,path):
 for part in path.split('.'):
  if not isinstance(obj,dict):return None
  obj=obj.get(part)
 return obj
report={'passed':False,'at':time.time(),'latest':{},'histories':[]}
for env in ('dcu-pd','a3-vllm'):
 d=get(BASE,'/api/monitoring/latest',{'environment':env})
 assert d['environment']==env and time.time()-d['ts']<20
 assert all(n['metrics']['status']=='ok' for n in d['nodes'].values())
 report['latest'][env]={'ts':d['ts'],'sources':{r:n['metrics']['status'] for r,n in d['nodes'].items()}}
 for hours in (1,6,24,168,720):
  t=time.monotonic();h=get(BASE,'/api/monitoring/history',{'hours':hours,'environment':env})
  assert h['environment']==env and len(h['points'])<=722
  assert all(p['environment']==env for p in h['points'])
  report['histories'].append({'environment':env,'hours':hours,'points':len(h['points']),'seconds':round(time.monotonic()-t,3)})
assert get(BASE,'/api/monitoring/latest')['environment']=='dcu-pd'
for endpoint in ['latest','history']+(['stream'] if MODE=='web' else []):
 try:get(BASE,'/api/monitoring/'+endpoint,{'environment':'bad'});raise AssertionError('invalid environment accepted')
 except urllib.error.HTTPError as e:assert e.code==400
if MODE=='web':
 for env in ('dcu-pd','a3-vllm'):
  with urllib.request.urlopen(BASE+'/api/monitoring/stream?environment='+env,timeout=6) as r:
   assert 'text/event-stream' in r.headers['Content-Type']
   assert r.readline().decode().strip()=='event: monitoring'
   data=json.loads(r.readline().decode()[6:]);assert data['environment']==env
 report['sse_environments_verified']=True
else:
 end=int((time.time()-30)//5)*5;h=get(BASE,'/api/monitoring/history',{'start':end-120,'end':end,'environment':'a3-vllm'})
 point=next(p for p in h['points'] if p['ts']==end);matches=[]
 paths=['nodes.decode.requests','nodes.decode.decode_tokens','nodes.prefill.cache_60s.ratio','nodes.prefill.cache_60s.external_ratio','nodes.decode.percentiles.ttft.p95','nodes.decode.percentiles.itl.p95','nodes.decode.percentiles.e2e.p95','nodes.decode.resources.kv_usage.engine0_ratio']
 for path in paths:
  labels='{environment="a3-vllm",schema="v1",path='+json.dumps(path)+'}'
  expr='default_rollup(monitoring_chart_value'+labels+'[5s]) and (default_rollup(monitoring_chart_valid'+labels+'[5s]) == 1)'
  params={'query':expr,'time':end};a=get(VM,'/api/v1/query',params)['data'];b=get(PROXY,'/api/v1/query',params)['data'];assert a==b
  expected=field(point,path);rows=a['result']
  if expected is None:assert not rows
  else:assert len(rows)==1 and math.isclose(float(rows[0]['value'][1]),expected,rel_tol=1e-8,abs_tol=1e-8)
  matches.append({'path':path,'value':expected})
 report['same_timestamp']={'ts':end,'matches':matches}
 queries=[]
 for f in sorted((ROOT/'dashboards').glob('a3-*.json')):
  d=json.loads(f.read_text())
  for k,p in d['spec']['panels'].items():
   for i,q in enumerate(p['spec']['queries']):
    for step in (15,60):
     expr=q['spec']['plugin']['spec']['query'].replace('$__interval',str(step)+'s').replace('$role','.*').replace('$node','.*')
     v=get(PROXY,'/api/v1/query_range',{'query':expr,'start':end-300,'end':end,'step':step});assert v['status']=='success'
     queries.append({'dashboard':d['metadata']['name'],'panel':k,'query':i,'step':step,'series':len(v['data']['result'])})
 report['perses_queries']=queries
 report['up']=get(VM,'/api/v1/query',{'query':'up{environment="a3-vllm"}'})['data']['result'];assert len(report['up'])==10 and all(x['value'][1]=='1' for x in report['up'])
report['passed']=True;(ROOT/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report if MODE=='web' else {k:v for k,v in report.items() if k not in ('up','perses_queries')},ensure_ascii=False))
