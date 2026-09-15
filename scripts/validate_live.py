"""Read-only live checks; emits measured coverage, not a fabricated 24-hour pass."""
import json,math,pathlib,statistics,time,urllib.request,urllib.parse
ROOT=pathlib.Path('/data2/monitoring/evidence')
def get(url):return json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=12))
def main():
 report={'observed_at':time.time(),'acceptance_duration':'short-session; not a 24-hour stability test','checks':{}}
 up=get('http://127.0.0.1:18428/api/v1/query?'+urllib.parse.urlencode({'query':'up','nocache':1,'time':time.time()-10}))['data']['result'];report['targets']=[dict(s['metric'],value=float(s['value'][1])) for s in up];assert len(up)==20 and all(float(s['value'][1])==1 for s in up)
 expected={'sglang-prefill':1,'sglang-decode':1,'mooncake':1,'node-prefill':1,'node-decode':1,'dcu-prefill':1,'dcu-decode':1,'vmagent':1,'victoriametrics':1,'aigate':1,'vllm-a3':8,'node-a3':2}
 assert {job:sum(s['metric']['job']==job for s in up) for job in expected}==expected
 assert all(s['metric']['environment']==('a3-vllm' if s['metric']['job'] in ('vllm-a3','node-a3') else 'dcu-pd') for s in up)
 latest=get('http://127.0.0.1:18430/api/monitoring/latest');report['latest_age_seconds']=time.time()-latest['ts'];assert report['latest_age_seconds']<15
 report['devices']={r:len(n['telemetry']['data']['gpus']) for r,n in latest['nodes'].items()};assert report['devices']=={'prefill':8,'decode':8}
 report['queries']={}
 for hours in (1,6,24,720):
  timings=[];count=0
  for i in range(10):
   start=time.monotonic();h=get('http://127.0.0.1:18430/api/monitoring/history?hours='+str(hours));timings.append(time.monotonic()-start);count=len(h['points']);assert count and count<=721
  p95=sorted(timings)[math.ceil(.95*len(timings))-1];report['queries'][str(hours)]={'p95_seconds':p95,'points':count,'measured_requests':10};assert p95<2
 report['checks']['metrics_and_queries']=True;ROOT.mkdir(exist_ok=True);(ROOT/'live-validation.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
if __name__=='__main__':main()
