"""Run only on the designated host via SSH MCP. Preserve runtime configuration."""
import copy, hashlib, http.client, json, os, socket, subprocess, sys, time, urllib.request
from pathlib import Path
os.umask(0o077)
ROOT=Path(__file__).resolve().parent
M=json.loads((ROOT/'manifest.json').read_text())
NAME=M['name']; TAG=NAME+':history-summary-20260924'; BACKUP=NAME+'-before-history-summary-20260924'
def cmd(*args): return subprocess.check_output(args,stderr=subprocess.STDOUT).decode()
def inspect(name): return json.loads(cmd('docker','inspect',name))[0]
def save(name,value): (ROOT/name).write_text(json.dumps(value,indent=2))
def read(name): return json.loads((ROOT/name).read_text())
def protected():
 return {c['Name']:[c['Id'],c['State']['StartedAt']] for c in json.loads(cmd('docker','inspect',*cmd('docker','ps','-q').split())) if c['Name']!='/'+NAME}
def get(path):
 with urllib.request.urlopen('http://127.0.0.1:'+str(M['port'])+path,timeout=25) as r:return r.read()
def hashes(container):
 code='import hashlib,json;from pathlib import Path;print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in '+repr(list(M['before']))+'}))'
 return json.loads(cmd('docker','exec',container,'python','-c',code))
class Docker(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout);self.sock.connect('/var/run/docker.sock')
def create(cfg):
 c=Docker('localhost',timeout=30);c.request('POST','/v1.39/containers/create?name='+NAME,json.dumps(cfg),{'Content-Type':'application/json'})
 r=c.getresponse();body=r.read();c.close();assert r.status==201, r.status
 return json.loads(body)['Id']
def prepare():
 assert not (ROOT/'prepared.json').exists()
 old=inspect(NAME);assert old['State']['Running'];assert hashes(NAME)==M['before'],'Live source changed'
 assert old['HostConfig']['NetworkMode']=='host'
 assert not any(any(dest.startswith(m['Destination'].rstrip('/')+'/') for dest in M['after']) for m in old['Mounts'])
 assert not any(any(line[2:]==dest for dest in M['before']) for line in cmd('docker','diff',NAME).splitlines()),'Modified container source'
 save('container-before.json',old)
 for dest,digest in M['after'].items():assert hashlib.sha256((ROOT/Path(dest).name).read_bytes()).hexdigest()==digest
 (ROOT/'Dockerfile').write_text('FROM '+old['Image']+'\n'+''.join('COPY '+Path(p).name+' '+p+'\n' for p in M['after']))
 with (ROOT/'build.log').open('w') as log:subprocess.run(['docker','build','--network=none','-t',TAG,str(ROOT)],stdout=log,stderr=subprocess.STDOUT,check=True)
 smoke='import monitoring.api' if NAME=='monitoring-api' else 'import app.web;from app.monitor_client import history'
 cmd('docker','run','--rm','--network','none','--entrypoint','python',TAG,'-c',smoke)
 save('prepared.json',{'old_id':old['Id'],'image':inspect(TAG)['Id'],'protected':protected()})
 print('prepared '+TAG,flush=True)
def rollback():
 old=read('container-before.json');current=inspect(NAME)
 if current['Id']!=old['Id']:
  assert current['Image']==read('prepared.json')['image'];assert inspect(BACKUP)['Id']==old['Id']
  cmd('docker','stop','--time','10',NAME);cmd('docker','rename',NAME,NAME+'-failed-history-summary-20260924');cmd('docker','rename',BACKUP,NAME)
 if not inspect(NAME)['State']['Running']:cmd('docker','start',NAME)
 save('rollback.json',{'id':inspect(NAME)['Id'],'at':time.time()})
def verify():
 assert hashes(NAME)==M['after'];assert protected()==read('prepared.json')['protected']
 results={}
 for env in ('dcu-pd','a3-vllm','xpu-pd'):
  path='/api/monitoring/history?hours=1&environment='+env+'&view=summary'
  started=time.monotonic();raw=get(path);value=json.loads(raw)
  assert value['environment']==env and value['points']
  assert not any('resources' in p.get('mooncake',{}) or any('resources' in n for n in p['nodes'].values()) for p in value['points'])
  results[env]={'bytes':len(raw),'seconds':round(time.monotonic()-started,3),'points':len(value['points'])}
 if NAME=='code-eval-web':assert b'&view=summary' in get('/static/app.js')
 return results
def switch():
 old=read('container-before.json');prepared=read('prepared.json')
 assert inspect(NAME)['Id']==old['Id'];assert hashes(NAME)==M['before'];assert protected()==prepared['protected']
 fields=('User','ExposedPorts','Env','Cmd','Healthcheck','Volumes','WorkingDir','Entrypoint','Labels','StopSignal','StopTimeout','Hostname')
 cfg=copy.deepcopy({k:old['Config'][k] for k in fields if k in old['Config']});cfg.update(Image=prepared['image'],HostConfig=old['HostConfig'])
 save('transaction.json',{'old_id':old['Id'],'new_image':prepared['image'],'backup':BACKUP})
 renamed=False;identity=None
 try:
  cmd('docker','stop','--time','30',NAME);cmd('docker','rename',NAME,BACKUP);renamed=True
  identity=create(cfg);cmd('docker','start',identity)
  for i in range(45):
   try:
    health=json.loads(get('/health' if NAME=='monitoring-api' else '/api/health'))
    assert health.get('status')=='ok';break
   except Exception:
    if i==44:raise
    time.sleep(1)
  results=verify();save('complete.json',{'passed':True,'image':prepared['image'],'results':results,'rollback_container':BACKUP,'other_containers_unchanged':True});print(json.dumps(results),flush=True)
 except BaseException as error:
  error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
  raise
{'prepare':prepare,'switch':switch,'verify':lambda:print(json.dumps(verify())),'rollback':rollback}[sys.argv[1]]()
