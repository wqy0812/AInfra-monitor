"""Run via SSH MCP. Minimal source overlay and rollback-preserving container switch."""
import hashlib,http.client,json,os,socket,subprocess,sys,time,urllib.request
from pathlib import Path
ROLE,MODE=sys.argv[1:3];ROOT=Path('/data2/monitoring/releases' if ROLE=='monitoring' else '/data2/code-eval/releases')/'xpu-cache-20260923'
NAME='monitoring-api' if ROLE=='monitoring' else 'code-eval-web';PORT=18430 if ROLE=='monitoring' else 18080
TAG=NAME+':xpu-cache-20260923';BACKUP=NAME+'-before-xpu-cache-20260923';os.umask(0o077)
def cmd(*a):return subprocess.check_output(a,stderr=subprocess.STDOUT).decode()
def inspect(n):return json.loads(cmd('docker','inspect',n))[0]
def save(n,d):(ROOT/n).write_text(json.dumps(d,indent=2))
def get(path):
 with urllib.request.urlopen('http://127.0.0.1:'+str(PORT)+path,timeout=20) as r:return r.read().decode()
def protected():return {c['Name']:[c['Id'],c['State']['StartedAt']] for c in json.loads(cmd('docker','inspect',*cmd('docker','ps','-q').split())) if c['Name']!='/'+NAME}
class Docker(http.client.HTTPConnection):
 def connect(self):self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')
def api(method,path,body):
 c=Docker('localhost',timeout=60);c.request(method,'/v1.39'+path,json.dumps(body),{'Content-Type':'application/json'});r=c.getresponse();raw=r.read();c.close();assert r.status<400, r.status
if MODE=='prepare':
 assert not (ROOT/'prepared.json').exists()
 old=inspect(NAME);save('container-before.json',old);manifest=json.loads((ROOT/(ROLE+'-manifest.json')).read_text())
 code='import hashlib,json;from pathlib import Path;print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in '+repr(list(manifest['before']))+'}))'
 assert json.loads(cmd('docker','exec',NAME,'python','-c',code))==manifest['before'],'Live files changed'
 for path,digest in manifest['after'].items():assert hashlib.sha256((ROOT/Path(path).name).read_bytes()).hexdigest()==digest
 (ROOT/'Dockerfile').write_text('FROM '+old['Image']+'\n'+''.join('COPY '+Path(p).name+' '+p+'\n' for p in manifest['after']))
 with (ROOT/'build.log').open('w') as log:subprocess.run(['docker','build','--network=none','-t',TAG,str(ROOT)],stdout=log,stderr=subprocess.STDOUT,check=True)
 if ROLE=='monitoring':
  check="""import asyncio,time,json
from monitoring.api import Service
async def run():
 s=Service('xpu-pd')
 try:
  before=time.monotonic();h=await s.history(3)
  values=[p['nodes']['prefill']['cache_60s']['ratio'] for p in h['points']]
  assert any(v is not None for v in values), 'No valid historical cache data'
  assert time.monotonic()-before<8
  groups=await s.raw(time.time()-30,time.time()-3);snaps,points=s.replay(groups,time.time()-25,time.time()-3)
  assert snaps[-1]['nodes']['prefill']['metrics']['data']['cache_60s']['ratio'] is not None
  print(json.dumps({'history_points':len(values),'valid':sum(v is not None for v in values),'max':max(v for v in values if v is not None),'seconds':time.monotonic()-before}))
 finally:await s.client.aclose()
asyncio.run(run())
"""
  print(cmd('docker','run','--rm','--network','host','--entrypoint','python',TAG,'-c',check),flush=True)
 else:
  check="from pathlib import Path;s=Path('/app/frontend/static/charts.js').read_text();assert 'xpu-native-prefix-v1' in s and '整体前缀缓存' in s;print('Web candidate passed')"
  print(cmd('docker','run','--rm','--network','none','--entrypoint','python',TAG,'-c',check),flush=True)
 save('prepared.json',{'old_id':old['Id'],'image':inspect(TAG)['Id'],'protected':protected()});print('prepared',flush=True)
else:
 old=json.loads((ROOT/'container-before.json').read_text());prepared=json.loads((ROOT/'prepared.json').read_text())
 assert inspect(NAME)['Id']==prepared['old_id'] and protected()==prepared['protected']
 fields=('User','ExposedPorts','Env','Cmd','Healthcheck','Volumes','WorkingDir','Entrypoint','Labels','StopSignal','StopTimeout','Hostname')
 cfg={k:old['Config'][k] for k in fields if k in old['Config']};cfg.update(Image=prepared['image'],HostConfig=old['HostConfig'])
 try:
  cmd('docker','stop','--time','30',NAME);cmd('docker','rename',NAME,BACKUP)
  api('POST','/containers/create?name='+NAME,cfg);cmd('docker','start',NAME)
  for i in range(45):
   try:
    health=json.loads(get('/health' if ROLE=='monitoring' else '/api/health'))
    assert inspect(NAME)['State'].get('Health',{}).get('Status','healthy')=='healthy'
    latest=json.loads(get('/api/monitoring/latest?environment=xpu-pd'))
    assert latest['nodes']['prefill']['metrics']['data']['cache_60s']['ratio'] is not None
    break
   except Exception:
    if i==44:raise
    time.sleep(1)
  history=json.loads(get('/api/monitoring/history?hours=3&environment=xpu-pd'))
  values=[p['nodes']['prefill']['cache_60s']['ratio'] for p in history['points']]
  assert any(v is not None for v in values)
  assert all(p['nodes']['prefill']['cache_60s']['semantics']=='xpu-native-prefix-v1' for p in history['points'])
  if ROLE=='web':
   assert 'xpu-cache-20260923-v1' in get('/')
   assert '整体前缀缓存' in get('/static/charts.js')
  for env in ('dcu-pd','a3-vllm'):
   h=json.loads(get('/api/monitoring/history?hours=3&environment='+env));assert h['environment']==env and h['points']
  assert protected()==prepared['protected']
  result={'passed':True,'image':prepared['image'],'rollback_container':BACKUP,'other_containers_unchanged':True,'latest_ratio':latest['nodes']['prefill']['metrics']['data']['cache_60s']['ratio'],'history_points':len(values),'valid':sum(v is not None for v in values),'max':max(v for v in values if v is not None)}
  save('complete.json',result);print(json.dumps(result),flush=True)
 except BaseException:
  if subprocess.run(['docker','inspect',BACKUP],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
   if subprocess.run(['docker','inspect',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
    cmd('docker','stop','--time','10',NAME);cmd('docker','rename',NAME,NAME+'-failed-xpu-cache-20260923')
   cmd('docker','rename',BACKUP,NAME);cmd('docker','start',NAME)
  save('rolled-back.json',{'at':time.time()});raise
