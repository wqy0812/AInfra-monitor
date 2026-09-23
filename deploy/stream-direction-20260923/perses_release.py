"""Patch only the two gateway panels in each live project via SSH MCP."""
import copy,json,pathlib,time,urllib.request,urllib.parse
ROOT=pathlib.Path('/data2/monitoring/releases/stream-direction-20260923')
BASE='http://122.247.53.162:18431'
creds=json.loads(pathlib.Path('/data2/monitoring/perses/admin-credentials.json').read_text())
r=urllib.request.Request(BASE+'/api/auth/providers/native/login',data=json.dumps(creds).encode(),headers={'Content-Type':'application/json'})
with urllib.request.urlopen(r,timeout=20) as f:token=json.load(f)['access_token']
def api(path,data=None):
 r=urllib.request.Request(BASE+path,data=json.dumps(data).encode() if data is not None else None,method='PUT' if data is not None else 'GET',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
 with urllib.request.urlopen(r,timeout=30) as f:return json.load(f)
def save(name,data):(ROOT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
def snapshots():return {p:api('/api/v1/projects/'+p+'/dashboards') for p in payload}
def path(p):return '/api/v1/projects/'+p+'/dashboards/gateway-generation'
payload=json.loads((ROOT/'panels.json').read_text());before=snapshots();save('perses-before.json',before)
candidates={};results={}
for project,panels in payload.items():
 old=next(d for d in before[project] if d['metadata']['name']=='gateway-generation');new=copy.deepcopy(old)
 for key,p in panels.items():
  dest=new['spec']['panels'][key]['spec']
  dest['display']['name']=p['spec']['display']['name']
  dest['display']['description']=('仅累计当前读取中请求等待后端有效输出的时间，排除网关写出耗时；首次输出前从读取响应体计时。' if key=='live-idle-max' else '当前一轮写入及刷新持续时间；升高表示输出路径可能存在背压。两个最大值可能来自不同请求。')+' 有效空闲为零，缺样留空。'
  dest['queries']=p['spec']['queries']
  for q in dest['queries']:
   expr=q['spec']['plugin']['spec']['query'].replace('$__interval','15s')
   url='http://127.0.0.1:18428/api/v1/query?'+urllib.parse.urlencode({'query':expr,'nocache':'1'})
   with urllib.request.urlopen(url,timeout=30) as f: result=json.load(f)
   assert result['status']=='success';results[project+'/'+key]={'series':len(result['data']['result'])}
 candidates[project]=(old,new)
save('perses-query-check.json',results)
changed=[]
try:
 for project,(old,new) in candidates.items():
  assert api(path(project))['spec']==old['spec'],'Concurrent dashboard change'
  changed.append(project);api(path(project),new)
 after=snapshots()
 assert sum(len(d['spec']['panels']) for ds in after.values() for d in ds)==292
 for project,ds in before.items():
  actual={d['metadata']['name']:d for d in after[project]}
  for d in ds:
   expected=candidates[project][1] if d['metadata']['name']=='gateway-generation' else d
   assert actual[d['metadata']['name']]['spec']==expected['spec'],'Unrelated panel changed'
 save('perses-complete.json',{'passed':True,'panels':292,'dashboards':sum(map(len,after.values())),'changed_panels':6,'queries':results})
 print(json.dumps({'passed':True,'panels':292,'dashboards':sum(map(len,after.values())),'changed_panels':6,'queries':results}),flush=True)
except BaseException:
 for project in reversed(changed):api(path(project),candidates[project][0])
 raise
