"""Second-batch validation/publication; run on test4 using SSH MCP only."""
import argparse,copy,hashlib,json,statistics,sys,time,concurrent.futures,urllib.request,urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));from project_release import equivalent
from quantile_release import split
BASE='http://122.247.53.162:18431';VM='http://127.0.0.1:18428'

def query(base,expression,start,end,step,nocache=True):
 expression=expression.replace('$__interval',str(step)+'s')
 params={'query':expression,'start':start,'end':end,'step':step,'nocache':int(nocache)}
 req=urllib.request.Request(base+'/api/v1/query_range',data=urllib.parse.urlencode(params).encode())
 with urllib.request.urlopen(req,timeout=45) as response:data=json.load(response)
 assert data['status']=='success'
 return sorted(data['data']['result'],key=lambda row:json.dumps(row['metric'],sort_keys=True))

def http(path,data=None):
 req=urllib.request.Request(BASE+path,data=json.dumps(data).encode() if data is not None else None,method='PUT' if data is not None else 'GET',headers={'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
def save(r,n,x):(r/n).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def docpath(x):return '/api/v1/projects/'+x['project']+'/dashboards/'+x['dashboard']
def fingerprint(changes):return hashlib.sha256(json.dumps(changes,sort_keys=True).encode()).hexdigest()
def audit(changes,r,samples=7):
 assert samples>=7
 end=int(time.time()//60)*60-180;checks=[];bench=[]
 for x in changes:
  old=[a['spec']['plugin']['spec']['query'] for a in x['before']['spec']['queries']];new=x['after']['spec']['queries'][0]['spec']['plugin']['spec']['query'];proxy=BASE+'/proxy/projects/'+x['project']+'/datasources/victoriametrics'
  for hours,step in [(1,5),(1,15),(1,60),(24,60)]:
   start=end-hours*3600;merged=query(VM,new,start,end,step);assert equivalent(merged,query(proxy,new,start,end,step))
   for expression,label in zip(old,['P50','P95','P99']):assert equivalent(query(VM,expression,start,end,step),split(merged,label)),(x['panel'],hours,step,label)
   checks.append({'project':x['project'],'panel':x['panel'],'hours':hours,'step':step,'series':len(merged)})
  assert any(c['series'] for c in checks if c['project']==x['project'] and c['panel']==x['panel']),'No actual samples to accept '+x['panel']
  pairs=[]
  for i in range(samples):
   def oldtime():
    t=time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as p:list(p.map(lambda e:query(VM,e,end-3600,end,5),old))
    return time.monotonic()-t
   def newtime():
    t=time.monotonic();query(VM,new,end-3600,end,5);return time.monotonic()-t
   if i%2:a=oldtime();b=newtime()
   else:b=newtime();a=oldtime()
   pairs.append((a,b))
  before=[a for a,b in pairs];after=[b for a,b in pairs];gain=1-statistics.median(after)/statistics.median(before)
  bench.append({'project':x['project'],'panel':x['panel'],'raw_seconds':pairs,'median_gain':gain,'passed':gain>=.2 and max(after)<=max(before)*1.05})
  warm=[]
  for i in range(samples):
   def warmed_old():
    t=time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as p:list(p.map(lambda e:query(VM,e,end-3600,end,5,False),old))
    return time.monotonic()-t
   def warmed_new():
    t=time.monotonic();query(VM,new,end-3600,end,5,False);return time.monotonic()-t
   if i%2:a=warmed_old();b=warmed_new()
   else:b=warmed_new();a=warmed_old()
   warm.append((a,b))
  bench[-1]['warm_raw_seconds']=warm
  print(x['project'],x['panel'],'gain',round(gain,3),flush=True)
 report={'passed':all(b['passed'] for b in bench),'environment':'production-readonly','samples_per_panel':samples,'candidate_digest':fingerprint(changes),'checks':checks,'benchmarks':bench,'end':end};save(r,'quantiles-audit.json',report)
 assert report['passed'],'Performance admission failed'
def rollback(r):
 journal=json.loads((r/'quantiles-journal.json').read_text())
 for x in reversed(journal):
  current=http(x['path'])
  if current['spec']==x['before']['spec']:continue
  assert current['spec']==x['after']['spec'],'Concurrent edit; refusing rollback'
  current['spec']=x['before']['spec'];http(x['path'],current);assert http(x['path'])['spec']==x['before']['spec']
 save(r,'quantiles-rollback.json',{'passed':True,'time':time.time()})
def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['audit','apply','rollback']);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--samples',type=int,default=7);a=p.parse_args();r=a.evidence
 if a.action=='rollback':rollback(r);return
 changes=json.loads((r/'changes.json').read_text());assert len(changes)==13
 if a.action=='audit':audit(changes,r,a.samples);return
 report=json.loads((r/'quantiles-audit.json').read_text());assert report['passed'] and report['environment']=='production-readonly' and report['candidate_digest']==fingerprint(changes)
 assert report.get('samples_per_panel',7)>=a.samples,'Pre-publication sample count is insufficient'
 semantics=json.loads((r/'quantile-semantics.json').read_text());assert semantics['passed'] and semantics['count']>=162
 assert json.loads((r/'image-publication.json').read_text())['passed'],'First batch must be accepted first'
 docs={docpath(x):http(docpath(x)) for x in changes}
 for x in changes:assert docs[docpath(x)]['spec']['panels'][x['panel']]==x['before'],'Concurrent panel edit'
 assert not (r/'quantiles-journal.json').exists(),'Existing publication journal; reconcile before retry'
 journal=[];save(r,'quantiles-journal.json',journal)
 try:
  for path,old in docs.items():
   assert http(path)==old,'Concurrent document edit';new=copy.deepcopy(old)
   for x in changes:
    if docpath(x)==path:new['spec']['panels'][x['panel']]=x['after']
   journal.append({'path':path,'before':old,'after':new});save(r,'quantiles-journal.json',journal);http(path,new);assert http(path)['spec']==new['spec']
  audit(changes,r,a.samples);save(r,'quantiles-publication.json',{'passed':True,'time':time.time(),'dashboards':len(docs),'panels':len(changes)})
 except Exception as error:
  error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
  raise
if __name__=='__main__':main()
