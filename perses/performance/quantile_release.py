"""Snapshot-based three-quantile patch. CLI prepare is local; publish via SSH MCP."""
import argparse,copy,hashlib,json,sys,time,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from project_release import equivalent,query
PHIS=[('0.5','P50'),('0.95','P95'),('0.99','P99')]

def merge_panel(panel):
 qs=panel['spec']['queries'];assert len(qs)==3
 specs=[q['spec']['plugin']['spec'] for q in qs]
 tails=[];legends=[]
 for s,(phi,label) in zip(specs,PHIS):
  prefix='histogram_quantile('+phi+', '
  assert s['query'].startswith(prefix),'Unexpected query shape'
  assert s['seriesNameFormat'].endswith(' '+label)
  assert 'perses_quantile' not in s['query']
  tails.append(s['query'][len(prefix):]);legends.append(s['seriesNameFormat'][:-len(label)])
  assert {k:v for k,v in s.items() if k not in ['query','seriesNameFormat']}=={k:v for k,v in specs[0].items() if k not in ['query','seriesNameFormat']}
 assert len(set(tails))==len(set(legends))==1,'Quantiles do not share exact guards'
 q='histogram_quantiles("perses_quantile", 0.5, 0.95, 0.99, '+tails[0]
 for phi,label in PHIS:q=f'label_replace(({q}), "perses_quantile", "{label}", "perses_quantile", "{phi.replace(".","[.]")}")'
 assert not panel['spec']['plugin']['spec'].get('querySettings'),'Custom query settings require explicit mapping'
 result=copy.deepcopy(panel);result['spec']['queries']=copy.deepcopy(qs[:1]);s=result['spec']['queries'][0]['spec']['plugin']['spec'];s['query']=q;s['seriesNameFormat']=legends[0]+'{{perses_quantile}}'
 return result

def prepare(resources):
 result=copy.deepcopy(resources);changes=[]
 for d in result['dashboards']:
  name=d['metadata']['name'];project=d['metadata']['project']
  if name!='gateway-requests' and (name!='backend-diagnostics' or project!='a3-monitoring'):continue
  for key,p in list(d['spec']['panels'].items()):
   expected=(name=='gateway-requests' and key in ['extra-first','extra-duration','extra-input','extra-output']) or (name=='backend-diagnostics' and key in ['extra-request_'+x+'_seconds' for x in ['queue_time','prefill_time','decode_time','inference_time','time_per_output_token']])
   if not expected:continue
   if len(p['spec']['queries'])==1 and 'histogram_quantiles(' in p['spec']['queries'][0]['spec']['plugin']['spec']['query']:continue
   d['spec']['panels'][key]=merge_panel(p);changes.append({'project':project,'dashboard':name,'panel':key,'before':p,'after':d['spec']['panels'][key]})
 return result,changes

def split(rows,label):
 out=[]
 for r in rows:
  if r['metric'].get('perses_quantile')!=label:continue
  r=copy.deepcopy(r);del r['metric']['perses_quantile'];out.append(r)
 return sorted(out,key=lambda x:json.dumps(x['metric'],sort_keys=True))

def main():
 p=argparse.ArgumentParser();p.add_argument('snapshot',type=Path);p.add_argument('output',type=Path);args=p.parse_args();resources=json.loads(args.snapshot.read_text());candidate,changes=prepare(resources);assert len(changes)==13,len(changes);args.output.mkdir(exist_ok=False);(args.output/'candidate.json').write_text(json.dumps(candidate,ensure_ascii=False,indent=2)+'\n');(args.output/'changes.json').write_text(json.dumps(changes,ensure_ascii=False,indent=2)+'\n');print('prepared',len(changes),'panels')
if __name__=='__main__':main()
