"""Compare Perses proxy, direct VM and monitoring-api at identical timestamps."""
import json,time,urllib.request,urllib.parse,urllib.error,math
from pathlib import Path
from generate import derived
ROOT=Path('/data2/monitoring/perses/evidence')
VM='http://127.0.0.1:18428';PROXY='http://122.247.53.162:18431/proxy/projects/dcu-monitoring/datasources/victoriametrics'
def get(base,path,params={}):
 with urllib.request.urlopen(base+path+'?'+urllib.parse.urlencode(params),timeout=15) as r:return json.load(r)
def field(o,path):
 for k in path.split('.'):
  if not isinstance(o,dict):return None
  o=o.get(k)
 return o
end=int(time.time()//5)*5-20;h=get('http://127.0.0.1:18430','/api/monitoring/history',{'start':end-300,'end':end,'hours':1})
paths=['nodes.decode.requests','nodes.decode.decode_tokens','nodes.prefill.cpu','nodes.decode.percentiles.ttft.p95','nodes.prefill.cache_60s.ratio','mooncake.capacity.used','mooncake.ssd_capacity.used','nodes.decode.resources.gpu_utilization.card0']
report={'matches':[],'null_matches':[]}
for path in paths:
 point=next((p for p in reversed(h['points']) if field(p,path) is not None),h['points'][-1])
 params={'query':derived(path).replace('$__interval','5s'),'time':point['ts']}
 a=get(VM,'/api/v1/query',params)['data'];b=get(PROXY,'/api/v1/query',params)['data'];assert a==b
 rows=a['result'];expected=field(point,path)
 if expected is None:assert not rows;report['null_matches'].append(path)
 else:
  assert len(rows)==1 and math.isclose(float(rows[0]['value'][1]),expected,rel_tol=1e-8,abs_tol=1e-8)
  report['matches'].append({'path':path,'ts':point['ts'],'value':expected})
try:get(PROXY,'/api/v1/status/config');raise AssertionError('non-query endpoint was exposed')
except urllib.error.HTTPError as e:assert e.code in (403,404);report['non_query_endpoint_denied']=e.code
(ROOT/'comparison.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
