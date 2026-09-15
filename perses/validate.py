"""Read-only validation of all chart queries and protected services on test4."""
import json,time,urllib.request,urllib.parse,subprocess
from pathlib import Path
ROOT=Path('/data2/monitoring/perses');BASE='http://122.247.53.162:18431/api/v1'
def get(url):
 with urllib.request.urlopen(url,timeout=20) as r:return json.load(r)
def query(q,start,end,step):
 return get('http://127.0.0.1:18428/api/v1/query_range?'+urllib.parse.urlencode({'query':q,'start':start,'end':end,'step':step}))
end=int(time.time()//5)*5-15;report={'at':end,'queries':[],'errors':[]}
for d in get(BASE+'/projects/dcu-monitoring/dashboards'):
 for key,panel in d['spec']['panels'].items():
  for i,item in enumerate(panel['spec']['queries']):
   expr=item['spec']['plugin']['spec']['query']
   for step in [15,60]:
    q=expr.replace('$__interval',str(step)+'s').replace('$role','.*').replace('$node','.*').replace('$device','.*')
    try:
     j=query(q,end-3600,end,step);assert j['status']=='success'
     rows=j['data']['result'];entry={'dashboard':d['metadata']['name'],'panel':key,'query':i,'step':step,'series':len(rows),'points':sum(len(r['values']) for r in rows)}
     report['queries'].append(entry)
    except Exception as e:
     body=e.read().decode() if hasattr(e,'read') else str(e)
     report['errors'].append({'dashboard':d['metadata']['name'],'panel':key,'query':i,'step':step,'error':body})
before=json.loads((ROOT/'evidence/protected-before.json').read_text());after=json.loads(subprocess.check_output(['docker','inspect','monitoring-vm','monitoring-vmagent','monitoring-api']))
report['protected_unchanged']=all(a['Id']==b['Id'] and a['State']['StartedAt']==b['State']['StartedAt'] for a,b in zip(before,after))
report['up']=get('http://127.0.0.1:18428/api/v1/query?query=up')['data']['result']
report['health']=get('http://127.0.0.1:18430/health')
(ROOT/'evidence/queries.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'queries':len(report['queries']),'errors':report['errors'],'empty':[x for x in report['queries'] if not x['points']],'protected_unchanged':report['protected_unchanged'],'up':len(report['up'])},ensure_ascii=False))
assert not report['errors'] and report['protected_unchanged']
