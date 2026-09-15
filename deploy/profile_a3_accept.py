"""Bounded actual A3 JSON/SSE/tool/continuation and profile check, run on test4 via SSH MCP."""
import json,time,urllib.request,urllib.parse,re
from pathlib import Path
ROOT=Path('/data2/monitoring/evidence/profile-environments-20260914')
BASE='http://122.52.5.131:9090';PROFILE='http://122.52.5.131:18082'
key=Path('/data2/monitoring/release/deploy/aigate-a3-profile-key').read_text().strip()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def fetch(base,path,body=None,auth=False):
 req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+key} if auth else {})})
 with opener.open(req,timeout=90) as r:return r.read()
def profile_metrics():
 raw=fetch(PROFILE,'/metrics',auth=True).decode();out={}
 for line in raw.splitlines():
  if line.startswith('aigate_'):
   metric,value=line.rsplit(' ',1);out[metric]=float(value)
 return out
def total(metrics,name):return sum(v for k,v in metrics.items() if k==name or k.startswith(name+'{'))
def usage_ok(u):return isinstance(u,dict) and isinstance(u.get('prompt_tokens'),int) and isinstance(u.get('completion_tokens'),int)
model=json.loads(fetch(BASE,'/v1/models'))['data'][0]['id'];before=profile_metrics();start=time.time();report={'start':start,'model':model,'cases':[]}
common=[{'role':'system','content':'You are a concise assistant.'},{'role':'user','content':'Reply with a short greeting.'}]
body={'model':model,'messages':common,'max_tokens':512,'temperature':0}
r=json.loads(fetch(BASE,'/v1/chat/completions',body));assert r['choices'][0]['message'].get('content');assert usage_ok(r.get('usage'));report['cases'].append({'name':'json','usage':r['usage']})
sse=fetch(BASE,'/v1/chat/completions',{**body,'stream':True,'stream_options':{'include_usage':True}}).decode();assert 'data: [DONE]' in sse
chunks=[json.loads(line[6:]) for line in sse.splitlines() if line.startswith('data: ') and line!='data: [DONE]'];usage=next(x['usage'] for x in reversed(chunks) if x.get('usage'));assert usage_ok(usage);assert any(c.get('choices') and any(v for k,v in c['choices'][0].get('delta',{}).items() if k in ('content','reasoning_content','tool_calls')) for c in chunks);report['cases'].append({'name':'sse','usage':usage})
tool={'type':'function','function':{'name':'get_weather','description':'Get weather for a city','parameters':{'type':'object','properties':{'city':{'type':'string'}},'required':['city']}}}
messages=[{'role':'user','content':'Call get_weather for Hangzhou. Use the tool now.'}]
toolbody={**body,'messages':messages,'tools':[tool],'tool_choice':{'type':'function','function':{'name':'get_weather'}},'max_tokens':1024}
r=json.loads(fetch(BASE,'/v1/chat/completions',toolbody));message=r['choices'][0]['message'];calls=message.get('tool_calls');assert calls and calls[0]['function']['name']=='get_weather';assert usage_ok(r.get('usage'));report['cases'].append({'name':'tool_call','usage':r['usage']})
messages += [message]+[{'role':'tool','tool_call_id':c['id'],'content':'{"city":"Hangzhou","temperature_c":24,"condition":"sunny"}'} for c in calls]
r=json.loads(fetch(BASE,'/v1/chat/completions',{**body,'messages':messages,'tools':[tool],'tool_choice':'none'}));assert r['choices'][0]['message'].get('content');assert usage_ok(r.get('usage'));report['cases'].append({'name':'tool_result','usage':r['usage']})
time.sleep(2);end=time.time();after=profile_metrics();report['end']=end
report['deltas']={n:total(after,n)-total(before,n) for n in ['aigate_requests_started_total','aigate_requests_routed_total','aigate_requests_ended_total','aigate_prompt_tokens_total','aigate_completion_tokens_total','aigate_profile_dropped_events_total','aigate_profile_write_errors_total']}
assert report['deltas']['aigate_requests_routed_total']>=4 and report['deltas']['aigate_requests_ended_total']>=4
assert report['deltas']['aigate_profile_dropped_events_total']==report['deltas']['aigate_profile_write_errors_total']==0
status=json.loads(fetch(PROFILE,'/profile/v1/status',auth=True));q={'start':max(start,status['retained_start']),'end':end};j=json.loads(fetch(PROFILE,'/profile/v1/analyses',q,True))
for _ in range(45):
 j=json.loads(fetch(PROFILE,'/profile/v1/analyses/'+j['id'],auth=True))
 if j['state'] in ('completed','failed'):break
 time.sleep(1)
assert j['state']=='completed' and j['result']['requests']>=4;report['analysis']=j;report['source_status']=status;report['passed']=True
(ROOT/'a3-protocol-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'passed':True,'cases':[c['name'] for c in report['cases']],'deltas':report['deltas'],'detail_requests':j['result']['requests']}))
