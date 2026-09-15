"""Isolated real-VM fixtures; never write test series into production VM."""
import json,time,urllib.request,urllib.parse,subprocess,tempfile,shutil
from pathlib import Path
from generate import derived,gateway_rate
ROOT=Path('/data2/monitoring/perses');NAME='perses-fixture-vm';URL='http://127.0.0.1:18528'
def run(*a):return subprocess.check_output(a,text=True)
def query(expr,ts):
 with urllib.request.urlopen(URL+'/api/v1/query?'+urllib.parse.urlencode({'query':expr.replace('$__interval','15s'),'time':ts})) as r:return json.load(r)['data']['result']
assert not subprocess.run(['docker','inspect',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
folder=tempfile.mkdtemp(prefix='perses-fixture-',dir='/tmp');report={}
try:
 run('docker','run','-d','--name',NAME,'--network','host','--cpus','.5','--memory','256m','-v',folder+':/storage','--entrypoint','/vm','monitoring-vm:1.151.0','-storageDataPath=/storage','-httpListenAddr=127.0.0.1:18528','-memory.allowedBytes=128MiB')
 for _ in range(30):
  try:urllib.request.urlopen(URL+'/health',timeout=1);break
  except OSError:time.sleep(.2)
 t=int(time.time()//5)*5-600;lines=[]
 for name in ['valid','zero','invalid','gap','stale']:
  for i in range(61):
   if name=='gap' and i==59:continue
   if name=='stale' and i>54:continue
   labels='{environment="dcu-pd",schema="v1",path="'+name+'"}'
   lines.extend([f'monitoring_chart_value{labels} {0 if name in ["zero","invalid"] else 7} {(t+i*5)*1000}',f'monitoring_chart_valid{labels} {0 if name=="invalid" and i==59 else 1} {(t+i*5)*1000}'])
 for case in ['stable','no-traffic','lifecycle','scrape-gap']:
  for i in range(61):
   if case=='scrape-gap' and i==55:continue
   labels='{environment="dcu-pd",job="aigate",instance="'+case+'"}'
   marker=t-60 if case!='lifecycle' or i<55 else t+275
   lines.extend([f'aigate_requests_started_total{labels} {5 if case=="no-traffic" else i} {(t+i*5)*1000}',f'aigate_profile_counter_start_time_seconds{labels} {marker} {(t+i*5)*1000}',f'up{labels} 1 {(t+i*5)*1000}'])
 req=urllib.request.Request(URL+'/api/v1/import/prometheus',data=('\n'.join(lines)+'\n').encode(),method='POST')
 urllib.request.urlopen(req).read();urllib.request.urlopen(URL+'/internal/force_flush').read()
 for name in ['valid','zero','invalid','gap','stale']:
  rows=query(derived(name),t+300);report[name]=[r['value'][1] for r in rows]
 assert report['valid']==['7'] and report['zero']==['0']
 assert all(not report[k] for k in ['invalid','gap','stale'])
 rows=query(gateway_rate('aigate_requests_started_total'),t+300)
 report['gateway']={r['metric']['instance']:float(r['value'][1]) for r in rows}
 print(json.dumps(report))
 assert set(report['gateway'])=={'stable','no-traffic'} and report['gateway']['no-traffic']==0
 (ROOT/'evidence/semantics.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
finally:
 subprocess.run(['docker','rm','-f',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);shutil.rmtree(folder)
