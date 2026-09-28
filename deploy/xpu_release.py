"""Run on test4 via SSH MCP. Add XPU without changing existing environments."""
import http.client as http_client
import json,os,pathlib,subprocess,time,urllib.request,urllib.parse,hashlib,shutil,http.client,socket,copy,sys
ROOT=pathlib.Path('/data2/monitoring/releases/xpu-20260921')
BASE='http://122.247.53.162:18431'
TOKEN=None

def save(name,obj):
 p=ROOT/name;p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n');p.chmod(0o600)
def cmd(*args):return subprocess.check_output(args).decode()
def http(url,method='GET',obj=None,auth=False):
 global TOKEN
 headers={}
 if auth:
  if TOKEN is None:
   TOKEN=http(BASE+'/api/auth/providers/native/login','POST',{'login':'admin','password':os.environ['PERSES_PASSWORD']})['access_token']
  headers['Authorization']='Bearer '+TOKEN
 if obj is not None:headers['Content-Type']='application/json'
 req=urllib.request.Request(url,data=json.dumps(obj).encode() if obj is not None else None,method=method,headers=headers)
 with urllib.request.urlopen(req,timeout=30) as r:
  b=r.read();return json.loads(b) if b else None

def snapshot():
 resources={}
 for project in http(BASE+'/api/v1/projects',auth=True):
  name=project['metadata']['name'];resources[name]={'project':project}
  for kind in ('datasources','dashboards'):
   resources[name][kind]=http(BASE+'/api/v1/projects/'+name+'/'+kind,auth=True)
 save('perses-before.json',resources)
 print('Saved Perses baseline:',list(resources))

def build():
 old=json.loads((ROOT/'containers-before.json').read_text())[0]
 for p in (ROOT/'monitoring').glob('*.py'):
  before=ROOT/'baseline'/p.name
  if before.exists():
   # Every uploaded existing file has a known local/production baseline.
   expected=json.loads((ROOT/'baseline-hashes.json').read_text())[p.name]
   assert hashlib.sha256(before.read_bytes()).hexdigest()==expected,p.name
 (ROOT/'Dockerfile').write_text('FROM '+old['Image']+'\nCOPY monitoring/ /monitoring/monitoring/\n')
 print(cmd('docker','build','--network=none','-t','monitoring-api:xpu-20260921',str(ROOT)))
 print(cmd('docker','run','--rm','--network=none','-v',str(ROOT/'tests')+':/tests:ro','--entrypoint','python','monitoring-api:xpu-20260921','-m','unittest','discover','-s','/tests'))

def scrape():
 path=pathlib.Path('/data2/monitoring/release/deploy/scrape.yml')
 assert path.read_bytes()==(ROOT/'scrape-before.yml').read_bytes(),'Scrape config changed concurrently'
 # Append a static target to each existing role job; preserve all live options.
 text=path.read_text()
 for role,ip,node in [('prefill','122.209.21.34','xpu-2'),('decode','122.209.21.33','xpu-1')]:
  header='- job_name: sglang-'+role+'\n'
  start=text.index(header);end=text.find('\n- job_name:',start+len(header))
  if end<0:end=len(text)
  section=text[start:end]
  marker='  metric_relabel_configs:'
  target="  - targets: ['"+ip+":8501']\n    labels: {environment: xpu-pd, role: "+role+", node: "+node+", service: sglang}\n"
  assert marker in section
  section=section.replace(marker,target+marker,1)
  text=text[:start]+section+text[end:]
 (ROOT/'scrape-candidate.yml').write_text(text)
 old=json.loads(cmd('docker','inspect','monitoring-vmagent'))[0]
 print(cmd('docker','run','--rm','--network=none','-v',str(ROOT/'scrape-candidate.yml')+':/candidate.yml:ro','--entrypoint',old['Config']['Entrypoint'][0],old['Image'],'-promscrape.config=/candidate.yml','-promscrape.config.dryRun'))
 temp=path.with_suffix('.xpu.tmp');temp.write_text(text);temp.chmod(path.stat().st_mode);temp.replace(path)
 cmd('docker','kill','--signal=HUP','monitoring-vmagent')
 print('XPU scrape configuration loaded')

class Connection(http_client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')
def switch():
 old=json.loads((ROOT/'containers-before.json').read_text())[0]
 assert json.loads(cmd('docker','inspect','monitoring-api'))[0]['Id']==old['Id']
 backup='monitoring-api-before-xpu-20260921'
 fields=('User','ExposedPorts','Env','Cmd','Healthcheck','Volumes','WorkingDir','Entrypoint','Labels','StopSignal','StopTimeout','Hostname')
 config={k:old['Config'][k] for k in fields if k in old['Config']}
 config.update(Image='monitoring-api:xpu-20260921',HostConfig=old['HostConfig'])
 save('transaction.json',{'backup':backup,'old_id':old['Id']})
 cmd('docker','stop','--time','30','monitoring-api');cmd('docker','rename','monitoring-api',backup)
 try:
  c=Connection('localhost',timeout=30);c.request('POST','/v1.39/containers/create?name=monitoring-api',json.dumps(config),{'Content-Type':'application/json'})
  r=c.getresponse();b=r.read();assert r.status==201,b
  cmd('docker','start','monitoring-api')
  for attempt in range(20):
   try:
    h=http('http://127.0.0.1:18430/health')
    assert all(h['environments'][e]['error'] is None and h['environments'][e]['processed_at']>time.time()-30 for e in ('dcu-pd','a3-vllm','xpu-pd'))
    x=http('http://127.0.0.1:18430/api/monitoring/latest?environment=xpu-pd')
    assert all(n['metrics']['status']=='ok' for n in x['nodes'].values())
    save('api-acceptance.json',{'health':h,'xpu':x});break
   except Exception:
    if attempt==19:raise
    time.sleep(2)
 except BaseException as error:
  error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
  raise
 print('API switched and three environment watermarks verified')

if __name__=='__main__':globals()[sys.argv[1]]()
