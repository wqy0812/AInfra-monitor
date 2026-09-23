"""Semantic regression checks against isolated VictoriaMetrics, never production data."""
import json,subprocess,time,urllib.request,urllib.parse,math
from dcu_bottlenecks import configure,presentation,histogram_quantiles
from project_queries import Queries

def main():
 name='dcu-bottlenecks-fixture-20260923';url='http://127.0.0.1:18539'
 assert subprocess.run(['docker','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0
 c=json.loads(subprocess.check_output(['docker','inspect','monitoring-vm']))[0];created=False
 try:
  subprocess.check_output(['docker','run','-d','--name',name,'--network','host','--cpus','.5','--memory','256m','--tmpfs','/storage','--entrypoint','/vm',c['Image'],'-storageDataPath=/storage','-httpListenAddr=127.0.0.1:18539','-memory.allowedBytes=128MiB']);created=True
  for _ in range(30):
   try:urllib.request.urlopen(url+'/health',timeout=1).close();break
   except OSError:time.sleep(.2)
  start=int(time.time()//5)*5-600;lines=[]
  cases=['normal','idle','gap','reset','stale','down','missing-bucket','no-count','missing-representative']
  for case in cases:
   for i in range(61):
    if case=='gap' and i==59 or case=='stale' and i>54:continue
    at=(start+i*5)*1000
    def emit(n,v,extra=None):
     tags={'environment':'dcu-pd','job':'sglang-prefill','instance':case,'node':'fixture','role':'prefill'};tags.update(extra or {})
     lines.append(n+'{'+','.join(k+'='+json.dumps(v) for k,v in tags.items())+'} '+str(v)+' '+str(at))
    emit('up',0 if case=='down' and i==59 else 1)
    count=0 if case=='idle' else (i-55 if case=='reset' and i>=55 else i)
    labels={'pp_rank':'0','tp_rank':'0','moe_ep_rank':'0'}
    for b,mult in [('1',1),('2',2),('+Inf',2)]:
     if case=='missing-bucket' and b=='2':continue
     emit('latency_bucket',count*mult,dict(labels,le=b))
    if case!='no-count':emit('latency_count',count*2,labels)
    emit('latency_sum',count*2,labels)
    for rank in ['0','1']:
     if case=='missing-representative' and rank=='0':continue
     emit('sglang:realtime_tokens_total',count*50,dict(labels,tp_rank=rank,moe_ep_rank=rank,mode='prefill_compute'))
    emit('used',0 if case=='idle' else 2);emit('capacity',0 if case=='idle' else 4)
  urllib.request.urlopen(urllib.request.Request(url+'/api/v1/import/prometheus',data=('\n'.join(lines)+'\n').encode()),timeout=20).close();urllib.request.urlopen(url+'/internal/force_flush').close()
  checks=0
  for step in (15,60):
   for case in cases:
    q=Queries('dcu-pd','sglang-prefill',',instance='+json.dumps(case))
    expressions=['('+histogram_quantiles(q,'latency',['1','2','+Inf'])+') and on(perses_quantile) label_replace(vector(1), "perses_quantile", "P95", "", "")',q.rate('sglang:realtime_tokens_total',',tp_rank="0",pp_rank="0",moe_ep_rank="0",dp_rank="",mode="prefill_compute"'),q.gauge('used')+' / ('+q.gauge('capacity')+' > 0)']
    values=[]
    for expr in expressions:
     params={'query':expr.replace('$__interval',str(step)+'s'),'time':start+300}
     rows=json.load(urllib.request.urlopen(url+'/api/v1/query?'+urllib.parse.urlencode(params)))['data']['result'];values.append([float(x['value'][1]) for x in rows]);checks+=1
    if case in ('gap','stale','down'):assert values==[[],[],[]],(case,step,values)
    elif case=='reset':assert values[:2]==[[],[]],values
    else:
     if case in ('idle','missing-bucket','no-count'):assert values[0]==[],(case,values)
     else:assert len(values[0])==1 and math.isclose(values[0][0],1.9),(case,values)
     if case=='missing-representative':assert values[1]==[],values
     else:assert values[1]==[0 if case=='idle' else 10],values
     assert values[2]==([] if case=='idle' else [.5]),values
  # Presentation must scale each query exactly once and not touch non-time units.
  d={'metadata':{'project':'dcu-monitoring','name':'overview'},'spec':{'panels':{'p':{'spec':{'display':{'name':'ITL','description':'Y 轴单位\n秒'},'plugin':{'spec':{'yAxis':{'label':'秒'}}},'queries':[{'spec':{'plugin':{'spec':{'query':'vector(0.02)'}}}}]}}}}}
  changed=presentation(d);assert presentation(changed)==changed
  expr=changed['spec']['panels']['p']['spec']['queries'][0]['spec']['plugin']['spec']['query']
  rows=json.load(urllib.request.urlopen(url+'/api/v1/query?'+urllib.parse.urlencode({'query':expr})))['data']['result'];assert float(rows[0]['value'][1])==20
  print(json.dumps({'semantic_checks':checks+1,'cases':cases,'steps':[15,60],'status':'passed'}))
 finally:
  if created:subprocess.check_call(['docker','rm','-f',name],stdout=subprocess.DEVNULL)
if __name__=='__main__':main()
