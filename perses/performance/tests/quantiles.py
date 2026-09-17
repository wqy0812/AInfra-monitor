"""Independent synthetic equivalence checks against an isolated localhost VM."""
import json,math,sys,time,urllib.parse,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from project_queries import Queries
URL='http://127.0.0.1:18543'

def query(q,at,step):
 data=urllib.parse.urlencode({'query':q.replace('$__interval',str(step)+'s'),'time':at,'nocache':1}).encode()
 with urllib.request.urlopen(urllib.request.Request(URL+'/api/v1/query',data=data),timeout=30) as r:return json.load(r)['data']['result']

def normalized(rows,label=None):
 result={}
 for row in rows:
  labels=dict(row['metric'])
  if label is not None:
   if labels.pop('perses_quantile')!=label:continue
  result[json.dumps(labels,sort_keys=True)]=row['value']
 return result

def main():
 start=int(time.time()//5)*5-900
 cases=['active','zero','absent','gap','stale','down','restart','reset','missing-bucket','bad-buckets','bad-delta','missing-count','mixed-types','multi-instance','partial-instance','missing-infinite','extra-bucket','group-restart']
 lines=[]
 for case in cases:
  for instance in (['n1','n2'] if case in ['multi-instance','partial-instance'] else ['n1']):
   for i in range(91):
    if case=='gap' and i==89 or case=='stale' and i>=85:continue
    ts=(start+i*5)*1000
    def emit(name,val,extra=None):
     labels={'job':'fixture','instance':instance,'environment':'perf-'+case,**(extra or {})};tags=','.join(k+'='+json.dumps(v) for k,v in labels.items());lines.append(f'{name}{{{tags}}} {val} {ts}')
    emit('up',0 if case=='down' and i==89 else 1);emit('boot',start+440 if case=='restart' and i>=88 else start-1000);emit('group_boot',start+440 if case=='group-restart' and i>=88 else start-1000)
    if case=='absent':continue
    val=0 if case=='zero' else i*5
    if case=='reset' and i>=88:val-=440
    variants=[('streaming',1),('nonstreaming',2),(None,3)] if case=='mixed-types' else [(None,1)]
    for scope,factor in variants:
     extra={} if scope is None else {'request_scope':scope}
     for bound,m in [('1',1),('2',2),('+Inf',3)]+([('4',3)] if case=='extra-bucket' else []):
      if (case=='missing-bucket' or case=='partial-instance' and instance=='n2') and bound=='2' or case=='missing-infinite' and bound=='+Inf':continue
      v=val*m*factor
      if case=='bad-buckets' and bound=='1':v=val*4
      if case=='bad-delta':v=i*12 if bound=='1' else v+10000
      emit('perf_latency_bucket',v,dict(extra,le=bound))
     if case!='missing-count':emit('perf_latency_count',val*3*factor+(10000 if case=='bad-delta' else 0),extra)
 body=('\n'.join(lines)+'\n').encode();urllib.request.urlopen(urllib.request.Request(URL+'/api/v1/import/prometheus',data=body),timeout=30).close();urllib.request.urlopen(URL+'/internal/force_flush').close()
 checks=[]
 for step in (5,15,60):
  for case in cases:
   q=Queries('perf-'+case,'fixture',origin='boot');args=('perf_latency',['1','2','+Inf'],'environment');origin='group_boot'
   merged=query(q.histogram_quantiles(*args,origin),start+450,step)
   valid=case in ['active','mixed-types','multi-instance']
   assert bool(merged)==valid,(case,step,'expected validity',merged)
   for phi,label in [(.5,'P50'),(.95,'P95'),(.99,'P99')]:
    old=normalized(query(q.histogram(args[0],args[1],phi,args[2],origin),start+450,step));new=normalized(merged,label)
    assert old.keys()==new.keys(),(case,step,label,old,new)
    for key in old:
     assert old[key][0]==new[key][0]
     assert math.isclose(float(old[key][1]),float(new[key][1]),rel_tol=1e-10,abs_tol=1e-10),(case,step,label,old,new)
     assert math.isclose(float(new[key][1]),1.5 if phi==.5 else 2,rel_tol=1e-10)
    checks.append({'case':case,'step':step,'quantile':label,'passed':True,'series':len(new)})
 report={'passed':True,'checks':checks,'count':len(checks),'scope':'isolated synthetic localhost VM; no production acceptance'}
 out=Path(sys.argv[1]);out.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':True,'checks':len(checks)}))
if __name__=='__main__':main()
