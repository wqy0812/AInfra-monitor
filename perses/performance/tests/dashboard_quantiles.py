"""Seed exact dashboard histogram families into the isolated LOCAL VM and compare."""
import concurrent.futures,json,math,re,statistics,sys,time,urllib.parse,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));from quantile_release import split,equivalent
URL='http://127.0.0.1:18543'

def fetch(expr,start,end,step,nocache):
 params={'query':expr.replace('$__interval',str(step)+'s'),'start':start,'end':end,'step':step,'nocache':int(nocache)}
 t=time.monotonic()
 with urllib.request.urlopen(urllib.request.Request(URL+'/api/v1/query_range',data=urllib.parse.urlencode(params).encode()),timeout=40) as r:j=json.load(r)
 assert j['status']=='success';return sorted(j['data']['result'],key=lambda x:json.dumps(x['metric'],sort_keys=True)),time.monotonic()-t

def main():
 changes=json.loads(Path(sys.argv[1]).read_text());out=Path(sys.argv[2]);end=int(time.time()//60)*60-120;start=end-3600
 families=[];origins={};base_labels=[]
 for x in changes:
  q=x['before']['spec']['queries'][0]['spec']['plugin']['spec']['query'];name,selectors=re.search(r'rate\(([^({]+)_bucket\{([^}]+)',q).groups();labels=dict(re.findall(r'(\w+)="([^"]*)"',selectors));is_backend=name.startswith('vllm:')
  bounds=sorted(set(re.findall(re.escape(name)+r'_bucket\{[^}]*le="([^"]+)"',q)),key=lambda x:float(x));families.append((name,labels,bounds,is_backend))
 lines=[]
 def emit(name,labels,val,at):lines.append(name+'{'+','.join(k+'='+json.dumps(v) for k,v in labels.items())+'} '+str(val)+' '+str(at*1000))
 for at in range(start-120,end+1,5):
  i=(at-start+120)//5+1;seen=set()
  for name,labels,bounds,is_backend in families:
   for n in range(8 if is_backend else 2):
    tags=dict(labels,instance='synthetic-'+str(n))
    if is_backend:tags.update(node='synthetic-'+str(n//4),engine=str(n%4))
    else:tags.update(backend='synthetic-backend',model='synthetic-model')
    key=(tags['environment'],tags['job'],tags['instance'])
    if key not in seen:
     up={k:v for k,v in tags.items() if k not in ['request_scope','backend','model','node','engine']};emit('up',up,1,at);seen.add(key)
     if not is_backend:
      emit('aigate_profile_counter_start_time_seconds',dict(up,request_scope='all'),start-1000,at);emit('aigate_profile_group_start_time_seconds',dict(tags,request_scope='all'),start-1000,at)
    for bidx,bound in enumerate(bounds):emit(name+'_bucket',dict(tags,le=bound),i*(bidx+1),at)
    emit(name+'_count',tags,i*len(bounds),at)
    if is_backend:emit(name+'_created',tags,start-1000,at)
  if len(lines)>45000:
   urllib.request.urlopen(urllib.request.Request(URL+'/api/v1/import/prometheus',data=('\n'.join(lines)+'\n').encode()),timeout=30).close();lines=[]
 if lines:urllib.request.urlopen(urllib.request.Request(URL+'/api/v1/import/prometheus',data=('\n'.join(lines)+'\n').encode()),timeout=30).close()
 urllib.request.urlopen(URL+'/internal/force_flush').close();print('seeded',len(families),'families',flush=True)
 checks=[];bench=[]
 for x in changes:
  old=[q['spec']['plugin']['spec']['query'] for q in x['before']['spec']['queries']];new=x['after']['spec']['queries'][0]['spec']['plugin']['spec']['query']
  for hours,step in [(1,5),(1,15),(1,60),(24,60)]:
   rows,secs=fetch(new,end-hours*3600,end,step,True);assert rows,(x['panel'],'unexpected empty synthetic')
   for expr,label in zip(old,['P50','P95','P99']):
    baseline,_=fetch(expr,end-hours*3600,end,step,True);assert baseline and equivalent(baseline,split(rows,label)),(x['panel'],step,label)
   checks.append({'project':x['project'],'panel':x['panel'],'hours':hours,'step':step,'passed':True,'series':len(rows)})
  for nocache in (True,False):
   pairs=[]
   for iteration in range(7):
    def baseline_run():
     t=time.monotonic()
     with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda q:fetch(q,start,end,5,nocache),old))
     return time.monotonic()-t
    if iteration%2:a=baseline_run();_,b=fetch(new,start,end,5,nocache)
    else:_,b=fetch(new,start,end,5,nocache);a=baseline_run()
    pairs.append((a,b))
   before=[x[0] for x in pairs];after=[x[1] for x in pairs];gain=1-statistics.median(after)/statistics.median(before)
   bench.append({'project':x['project'],'panel':x['panel'],'nocache':nocache,'n':7,'raw_seconds':pairs,'median_gain':gain,'before_median':statistics.median(before),'after_median':statistics.median(after),'before_p95':max(before),'after_p95':max(after)})
  print(x['project'],x['panel'],'checked',flush=True)
 report={'passed':True,'environment':'local-synthetic','range_end':end,'seeded_hours':1,'checks':checks,'benchmarks':bench,'cold_performance_passed':all(x['median_gain']>=.2 and x['after_p95']<=x['before_p95']*1.05 for x in bench if x['nocache'])};out.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'checks':len(checks),'cold_performance_passed':report['cold_performance_passed']}))
if __name__=='__main__':main()
