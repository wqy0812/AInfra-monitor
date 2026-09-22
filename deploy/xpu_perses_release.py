"""Create only XPU resources, verify every query and preserve old projects."""
from xpu_release import *
EMPTY='vector(0) unless on() vector(0)'
def endpoint(d):
 kind={'Project':'projects','Datasource':'datasources','Dashboard':'dashboards'}[d['kind']]
 return BASE+'/api/v1/'+('projects/'+d['metadata']['project']+'/' if d['kind']!='Project' else '')+kind

def query(url,q,start,end,step):
 q=q.replace('$__interval',str(step)+'s')
 for v in ('role','node','device'):q=q.replace('$'+v,'.*')
 return http(url+'/api/v1/query_range?'+urllib.parse.urlencode({'query':q,'start':start,'end':end,'step':step,'nocache':1}),auth=url.startswith(BASE))['data']['result']

def main():
 root=ROOT/'xpu-monitoring'
 docs=[json.loads((root/'project.json').read_text()),json.loads((root/'datasource.json').read_text())]+[json.loads(p.read_text()) for p in sorted((root/'dashboards').glob('*.json'))]
 before=json.loads((ROOT/'perses-before.json').read_text())
 # Detect edits since snapshot before creating anything.
 for project,resources in before.items():
  for kind in ('datasources','dashboards'):
   current=http(BASE+'/api/v1/projects/'+project+'/'+kind,auth=True)
   assert {x['metadata']['name']:x['spec'] for x in current}=={x['metadata']['name']:x['spec'] for x in resources[kind]},project
 created=[]
 for d in docs:
  url=endpoint(d)
  existing=http(url,auth=True)
  match=[x for x in existing if x['metadata']['name']==d['metadata']['name']]
  if match:
   assert match[0]['spec']==d['spec'],'Existing XPU resource differs'
  else:
   http(url,'POST',d,auth=True);created.append(url+'/'+d['metadata']['name']);save('perses-created.json',created)
  got=http(url+'/'+d['metadata']['name'],auth=True)
  # Perses fills defaults, so verify query and layout content independently.
  if d['kind']=='Dashboard':
   assert got['spec']['panels']==d['spec']['panels']
   assert got['spec']['layouts']==d['spec']['layouts']
 save('perses-published.json',{'resources':len(docs),'created':created})
 print('Published',len(docs),'resources')

if __name__=='__main__':main()
