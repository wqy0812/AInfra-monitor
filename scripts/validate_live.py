"""Read-only live checks; emits measured coverage, not a fabricated 24-hour pass."""
import json,math,pathlib,re,statistics,time,urllib.request,urllib.parse
import yaml
ROOT=pathlib.Path('/data2/monitoring/evidence')
CONFIG=pathlib.Path(__file__).resolve().parents[1]/'deploy/scrape.yml'
IDENTITY=('job','instance','environment')

def expected_targets(config):
 document=yaml.safe_load(config.read_text())
 expected=set()
 for job in document['scrape_configs']:
  if any(k.endswith('_sd_configs') for k in job) or job.get('honor_labels'):
   raise ValueError('Unsupported dynamic targets or honor_labels: '+job['job_name'])
  for group in job.get('static_configs',[]):
   for address in group['targets']:
    labels={'job':job['job_name'],**group.get('labels',{}),'__address__':address}
    for rule in job.get('relabel_configs',[]):
     if rule.get('action','replace')!='replace':
      raise ValueError('Unsupported target relabel action: '+rule['action'])
     source=rule.get('separator',';').join(labels.get(k,'') for k in rule.get('source_labels',[]))
     match=re.fullmatch(rule.get('regex','(.*)'),source)
     if match:
      replacement=rule.get('replacement','$1')
      replacement=re.sub(r'\$\{(\d+)\}|\$(\d+)',lambda m:match.group(int(m[1] or m[2])) or '',replacement)
      labels[rule['target_label']]=replacement
    labels.setdefault('instance',labels['__address__'])
    identity=tuple(labels.get(k,'') for k in IDENTITY)
    if not all(identity) or identity in expected:raise ValueError('Missing labels or duplicate configured target: '+str(identity))
    expected.add(identity)
 if not expected:raise ValueError('No configured targets')
 return expected

def validate_targets(up,timestamps,expected,at):
 def index(rows):
  result={}
  for row in rows:
   identity=tuple(row['metric'].get(k,'') for k in IDENTITY)
   if identity in result:raise ValueError('Duplicate observed target: '+str(identity))
   result[identity]=float(row['value'][1])
  return result
 values,stamps=index(up),index(timestamps)
 for kind,data in [('up',values),('timestamp',stamps)]:
  if set(data)!=expected:raise ValueError(kind+' targets differ: missing='+str(sorted(expected-set(data)))+' unexpected='+str(sorted(set(data)-expected)))
 for identity,value in values.items():
  if value!=1:raise ValueError('Scrape failed: '+str(identity))
  if not math.isfinite(stamps[identity]) or not 0<=at-stamps[identity]<15:raise ValueError('Scrape stale: '+str(identity))
 return [dict(zip(IDENTITY,key),value=values[key],observed_at=stamps[key]) for key in sorted(expected)]

def get(url):return json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=12))
def main():
 report={'observed_at':time.time(),'acceptance_duration':'short-session; not a 24-hour stability test','checks':{}}
 at=time.time()-10
 def query(expression):
  data=get('http://127.0.0.1:18428/api/v1/query?'+urllib.parse.urlencode({'query':expression,'nocache':1,'time':at}))
  if data.get('status')!='success':raise ValueError('Target query failed')
  return data['data']['result']
 report['targets']=validate_targets(query('up'),query('timestamp(up)'),expected_targets(CONFIG),at)
 latest=get('http://127.0.0.1:18430/api/monitoring/latest');report['latest_age_seconds']=time.time()-latest['ts'];assert report['latest_age_seconds']<15
 report['devices']={r:len(n['telemetry']['data']['gpus']) for r,n in latest['nodes'].items()};assert report['devices']=={'prefill':8,'decode':8}
 report['queries']={}
 for hours in (1,6,24,720):
  timings=[];count=0
  for i in range(10):
   start=time.monotonic();h=get('http://127.0.0.1:18430/api/monitoring/history?hours='+str(hours));timings.append(time.monotonic()-start);count=len(h['points']);assert count and count<=721
  p95=sorted(timings)[math.ceil(.95*len(timings))-1];report['queries'][str(hours)]={'p95_seconds':p95,'points':count,'measured_requests':10};assert p95<2
 report['checks']['metrics_and_queries']=True;ROOT.mkdir(exist_ok=True);(ROOT/'live-validation.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
if __name__=='__main__':main()
