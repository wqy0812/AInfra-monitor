from xpu_perses_release import *
import concurrent.futures

def main():
 http(BASE+'/api/v1/projects',auth=True)
 docs=http(BASE+'/api/v1/projects/xpu-monitoring/dashboards',auth=True)
 end=int((time.time()-30)//60)*60;start=end-300
 tasks={}
 for d in docs:
  for key,p in d['spec']['panels'].items():
   for q in p['spec'].get('queries',[]):
    expr=q['spec']['plugin']['spec']['query'];tasks.setdefault(expr,[]).append((d['metadata']['name'],key))
 def check(item):
  expr,panels=item;results=[]
  for step in (15,60):
   a=query('http://127.0.0.1:18428',expr,start,end,step)
   b=query(BASE+'/proxy/projects/xpu-monitoring/datasources/victoriametrics',expr,start,end,step)
   canonical=lambda rows:sorted(rows,key=lambda x:json.dumps(x['metric'],sort_keys=True))
   assert canonical(a)==canonical(b),(panels,step)
   if expr==EMPTY:assert not a
   results.append({'step':step,'series':len(a),'points':sum(len(s['values']) for s in a)})
  return {'panels':panels,'results':results}
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(check,tasks.items()))
 before=json.loads((ROOT/'perses-before.json').read_text())
 for project,resources in before.items():
  for kind in ('datasources','dashboards'):
   current=http(BASE+'/api/v1/projects/'+project+'/'+kind,auth=True)
   assert {x['metadata']['name']:x['spec'] for x in current}=={x['metadata']['name']:x['spec'] for x in resources[kind]}
 old=json.loads((ROOT/'containers-before.json').read_text())
 for c in old[1:]:
  now=json.loads(cmd('docker','inspect',c['Name']))[0];assert now['Id']==c['Id'] and now['State']['StartedAt']==c['State']['StartedAt']
 history=http('http://127.0.0.1:18430/api/monitoring/history?environment=xpu-pd&hours=0.1')
 assert history['points']
 for p in history['points']:
  for n in p['nodes'].values():
   assert n['cpu'] is None and n['hicache']['representative']['ratio'] is None and n['cache_60s']['ratio'] is None
 save('verification.json',{'passed':True,'queries':len(tasks),'comparisons':len(tasks)*2,'dashboards':len(docs),'panels':sum(len(d['spec']['panels']) for d in docs),'history_points':len(history['points']),'results':results,'old_projects_unchanged':True,'other_containers_unchanged':True})
 print('Verified',len(tasks)*2,'VM/Perses comparisons;',len(history['points']),'history points')
if __name__=='__main__':main()
