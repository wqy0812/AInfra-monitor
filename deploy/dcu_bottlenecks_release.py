"""Bounded test4 release. Run only through SSH MCP; credentials never leave host."""
import copy,json,pathlib,subprocess,sys,time,urllib.request,urllib.parse,statistics
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'perses'))
from dcu_bottlenecks import configure,inventory,expand_scrape
ROOT=pathlib.Path('/data2/monitoring/releases/dcu-bottlenecks-20260923')
CONFIG=pathlib.Path('/data2/monitoring/release/deploy/scrape.yml')
BASE='http://122.247.53.162:18431';VM='http://127.0.0.1:18428';TOKEN=None
PROJECTS=('dcu-monitoring','a3-monitoring','xpu-monitoring')
def save(name,data): (ROOT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
def api(path,data=None):
 global TOKEN
 if TOKEN is None:
  creds=json.loads(pathlib.Path('/data2/monitoring/perses/admin-credentials.json').read_text())
  req=urllib.request.Request(BASE+'/api/auth/providers/native/login',data=json.dumps(creds).encode(),headers={'Content-Type':'application/json'})
  TOKEN=json.load(urllib.request.urlopen(req,timeout=20))['access_token']
 req=urllib.request.Request(BASE+path,data=json.dumps(data).encode() if data is not None else None,method='PUT' if data is not None else 'GET',headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=40) as r: return json.load(r)
def query(q,at=None,start=None,step=60,proxy=None):
 params={'query':q.replace('$__interval','60s').replace('$role','.*').replace('$node','.*').replace('$device','.*'),'nocache':1}
 if start is not None:params.update(start=start,end=at,step=step)
 else:params['time']=at or time.time()
 path='/api/v1/'+('query_range' if start is not None else 'query')+'?'+urllib.parse.urlencode(params)
 if proxy: return api('/proxy/projects/'+proxy+'/datasources/victoriametrics'+path)['data']['result']
 return json.load(urllib.request.urlopen(VM+path,timeout=45))['data']['result']
def snapshots():return {p:api('/api/v1/projects/'+p+'/dashboards') for p in PROJECTS}
def path(d):return '/api/v1/projects/'+d['metadata']['project']+'/dashboards/'+d['metadata']['name']
def prepare():
 before=json.loads((ROOT/'dashboards-before.json').read_text());inv=inventory({r:(ROOT/(r+'.metrics')).read_text() for r in ('prefill','decode')})
 units={};evidence={};end=int(time.time())-15
 for p,ds in before.items():
  for d in ds:
   for key,panel in d['spec']['panels'].items():
    s=panel['spec'];old=s['plugin']['spec'].get('yAxis',{}).get('label','')
    if old not in ('秒','s','ms','毫秒'):continue
    ident=p+'/'+d['metadata']['name']+'/'+key;title=s['display']['name'];unit='秒'
    if 'ITL' in title:unit='ms'
    elif not any(t in title for t in ('TTFT','E2E','排队','等待','年龄','阶段','时延','总耗时','首个有效')):
     vals=[]
     for item in s.get('queries',[]):
      q=item['spec']['plugin']['spec']['query']
      if not any(t in q for t in ('0.95','p95','P95')):continue
      for row in query(q,end,end-86400,300):
       labels=row['metric'];phi=labels.get('perses_quantile','');pth=labels.get('path','')
       if phi and phi not in ('P95','0.95'):continue
       if '.percentiles.' in pth and not pth.endswith('.p95'):continue
       vals += [float(v)*(0.001 if old in ('ms','毫秒') else 1) for _,v in row['values'] if v not in ('NaN','+Inf','-Inf')]
     if vals and statistics.median(vals)<.1:unit='ms'
     evidence[ident]={'valid_p95_samples':len(vals),'median_seconds':statistics.median(vals) if vals else None}
    units[ident]=unit
 candidate={p:[configure(d,inv,units) for d in ds] for p,ds in before.items()}
 save('inventory.json',inv);save('time_units.json',units);save('unit-evidence.json',evidence);save('dashboards-candidate.json',candidate)
 (ROOT/'scrape-candidate.yml').write_text(expand_scrape((ROOT/'scrape-before.yml').read_text()))
 print('Prepared',sum(len(d['spec']['panels']) for ds in candidate.values() for d in ds),'panels')
def write_config(content):
 tmp=CONFIG.with_suffix('.dcu-bottlenecks.tmp');tmp.write_bytes(content);tmp.chmod(CONFIG.stat().st_mode);tmp.replace(CONFIG)
 subprocess.check_call(['docker','kill','--signal=HUP','monitoring-vmagent'])
def collect():
 old=(ROOT/'scrape-before.yml').read_bytes();new=(ROOT/'scrape-candidate.yml').read_bytes();assert CONFIG.read_bytes()==old,'Concurrent scrape edit'
 c=json.loads(subprocess.check_output(['docker','inspect','monitoring-vmagent']))[0]
 subprocess.check_call(['docker','run','--rm','--network=none','-v',str(ROOT/'scrape-candidate.yml')+':/candidate.yml:ro','--entrypoint',c['Config']['Entrypoint'][0],c['Image'],'-promscrape.config=/candidate.yml','-promscrape.config.dryRun'])
 assert CONFIG.read_bytes()==old;write_config(new);save('collection.json',{'at':time.time()});print('Collection reloaded')
def publish():
 assert time.time()-json.loads((ROOT/'collection.json').read_text())['at']>=120
 up=query('min_over_time(up{environment="dcu-pd",job=~"sglang-(prefill|decode)"}[2m])')
 assert len(up)==2 and all(float(x['value'][1])==1 for x in up),up
 before=json.loads((ROOT/'dashboards-before.json').read_text());candidate=json.loads((ROOT/'dashboards-candidate.json').read_text())
 # Reject unsupported/oversized queries before any dashboard update.
 for ds in candidate.values():
  for d in ds:
   for key,panel in d['spec']['panels'].items():
    if key.startswith('bn-'):
     for item in panel['spec']['queries']:
      q=item['spec']['plugin']['spec']['query'];assert len(q.encode())<16000;query(q)
 current=snapshots()
 for p in PROJECTS:
  assert {d['metadata']['name']:d['spec'] for d in current[p]}=={d['metadata']['name']:d['spec'] for d in before[p]},'Concurrent dashboard edit '+p
 journal=[];save('journal.json',journal)
 for p in PROJECTS:
  for old,new in zip(before[p],candidate[p]):
   if old['spec']==new['spec']:continue
   now=api(path(old));assert now['spec']==old['spec'];new=copy.deepcopy(new);new['metadata']=now['metadata']
   journal.append({'before':old,'candidate':new});save('journal.json',journal)
   api(path(new),new);assert api(path(new))['spec']==new['spec']
 save('published.json',{'at':time.time(),'updated':len(journal)});print('Published',len(journal),'dashboards')
def verify():
 candidate=json.loads((ROOT/'dashboards-candidate.json').read_text());results=[];at=int(time.time())-15
 for p,ds in candidate.items():
  for d in ds:
   assert api(path(d))['spec']==d['spec']
   old=next(x for x in json.loads((ROOT/'dashboards-before.json').read_text())[p] if x['metadata']['name']==d['metadata']['name'])
   for key,panel in d['spec']['panels'].items():
    if not key.startswith('bn-') and panel['spec']['queries']==old['spec']['panels'][key]['spec']['queries']:continue
    for index,item in enumerate(panel['spec']['queries']):
     q=item['spec']['plugin']['spec']['query'];direct=query(q,at);proxied=query(q,at,proxy=p)
     norm=lambda rows: sorted(rows,key=lambda r:json.dumps(r['metric'],sort_keys=True))
     assert norm(direct)==norm(proxied),(p,key,index)
     results.append({'project':p,'dashboard':d['metadata']['name'],'panel':key,'query':index,'series':len(direct)})
 save('verification.json',{'at':at,'queries':results,'empty_queries':[r for r in results if not r['series']]})
 assert CONFIG.read_bytes()==(ROOT/'scrape-candidate.yml').read_bytes()
 old=json.loads((ROOT/'services-before.json').read_text());items=json.loads(subprocess.check_output(['docker','inspect']+[x['name'] for x in old]))
 assert old==[{'name':c['Name'],'id':c['Id'],'started':c['State']['StartedAt']} for c in items]
 print('Verified',len(results),'queries; empty:',sum(not r['series'] for r in results),'all service IDs/start times unchanged')
def rollback():
 for entry in reversed(json.loads((ROOT/'journal.json').read_text()) if (ROOT/'journal.json').exists() else []):
  now=api(path(entry['candidate']));assert now['spec'] in [entry['candidate']['spec'],entry['before']['spec']],'Concurrent dashboard edit'
  old=copy.deepcopy(entry['before']);old['metadata']=now['metadata'];api(path(old),old)
 assert CONFIG.read_bytes() in [(ROOT/'scrape-candidate.yml').read_bytes(),(ROOT/'scrape-before.yml').read_bytes()]
 write_config((ROOT/'scrape-before.yml').read_bytes());print('Rolled back; historical metrics retained')
if __name__=='__main__':globals()[sys.argv[1]]()
