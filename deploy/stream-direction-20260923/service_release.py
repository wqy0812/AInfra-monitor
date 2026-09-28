"""Minimal image layer and forward-only replacement, via SSH MCP."""
import http.client,json,os,socket,subprocess,sys,time,urllib.request
from pathlib import Path
ROLE=sys.argv[1];ROOT=Path('/data2/monitoring/releases' if ROLE=='monitoring' else '/data2/code-eval/releases')/'stream-direction-20260923'
NAME='monitoring-api' if ROLE=='monitoring' else 'code-eval-web';BACKUP=NAME+'-before-stream-direction-20260923';TAG=NAME+':stream-direction-20260923';os.umask(0o077)
def cmd(*a):return subprocess.check_output(a,stderr=subprocess.STDOUT).decode()
def inspect(n):return json.loads(cmd('docker','inspect',n))[0]
def save(n,x):(ROOT/n).write_text(json.dumps(x,indent=2))
def get(path):
 with urllib.request.urlopen('http://127.0.0.1:'+('18430' if ROLE=='monitoring' else '18080')+path,timeout=30) as r:return r.read().decode()
def protected():return {c['Name']:(c['Id'],c['State']['StartedAt']) for c in json.loads(cmd('docker','inspect',*cmd('docker','ps','-q').split())) if c['Name']!='/'+NAME}
old=inspect(NAME);save('container-before.json',old);others=protected()
assert old['HostConfig']['NetworkMode']=='host'
expected={'gateway_live.py':'dfa2d84be106b29b3bd42062bc556337745cca22139d2b95f7620cf136a06d5b'} if ROLE=='monitoring' else {'index.html':'588f38ffc9c5575b8ab580d21c22fcdd64a5060d16d8efd52d3520e07e7f12c0','charts.js':'84c247c82d059afe4317521e83618bc6975804f022048f811eac71e158e09cbf'}
source='/monitoring/monitoring/' if ROLE=='monitoring' else '/app/frontend/static/'
code='import pathlib,hashlib,json;print(json.dumps({n:hashlib.sha256(pathlib.Path('+repr(source)+',n).read_bytes()).hexdigest() for n in '+repr(list(expected))+'}))'
assert json.loads(cmd('docker','exec',NAME,'python','-c',code))==expected,'Live source changed'

files=['gateway_live.py'] if ROLE=='monitoring' else ['index.html','charts.js']
dest='/monitoring/monitoring/' if ROLE=='monitoring' else '/app/frontend/static/'
(ROOT/'Dockerfile').write_text('FROM '+old['Image']+'\n'+''.join('COPY '+f+' '+dest+f+'\n' for f in files))
with (ROOT/'build.log').open('w') as log:subprocess.run(['docker','build','--network=none','-t',TAG,str(ROOT)],stdout=log,stderr=subprocess.STDOUT,check=True)
if ROLE=='monitoring':
 check="from monitoring.gateway_live import expressions,FIELDS;assert 'backend_wait_max_seconds' in FIELDS and 'write_active_max_seconds' in FIELDS;assert len(expressions('a3-vllm',15))==4;print('candidate gateway fields passed')"
else:
 check="from pathlib import Path;h=Path('/app/frontend/static/index.html').read_text();j=Path('/app/frontend/static/charts.js').read_text();assert '后端最长未更新时间' in h and '写持续最长时间' in h;assert 'backend_wait_max_seconds' in j and 'write_active_max_seconds' in j;print('candidate page passed')"
print(cmd('docker','run','--rm','--network','none','--entrypoint','python',TAG,'-c',check),flush=True)
new=inspect(TAG)['Id'];save('prepared.json',{'image':new,'protected':others})
class Docker(http.client.HTTPConnection):
 def connect(self):self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')
fields=('User','ExposedPorts','Env','Cmd','Healthcheck','Volumes','WorkingDir','Entrypoint','Labels','StopSignal','StopTimeout','Hostname')
cfg={k:old['Config'][k] for k in fields if k in old['Config']};cfg.update(Image=new,HostConfig=old['HostConfig'])
assert inspect(NAME)['Id']==old['Id'] and protected()==others
try:
 cmd('docker','stop','--time','30',NAME);cmd('docker','rename',NAME,BACKUP)
 c=Docker('localhost',timeout=30);c.request('POST','/v1.39/containers/create?name='+NAME,json.dumps(cfg),{'Content-Type':'application/json'});r=c.getresponse();body=r.read();assert r.status==201,body;c.close();cmd('docker','start',NAME)
 for i in range(30):
  try:
   health=json.loads(get('/health' if ROLE=='monitoring' else '/api/health'));break
  except Exception:
   if i==29:raise
   time.sleep(1)
 assert protected()==others
 if ROLE=='web':
  h=get('/');assert '后端最长未更新时间' in h and '写持续最长时间' in h
  j=get('/static/charts.js');assert 'backend_wait_max_seconds' in j and 'write_active_max_seconds' in j
 else:
  for environment in ('dcu-pd','a3-vllm','xpu-pd'):
   data=json.loads(get('/api/monitoring/history?hours=1&environment='+environment))
   assert data['points'] and all(k in data['gateway_status'] for k in ('backend_wait_max_seconds','write_active_max_seconds'))
   assert all(k in data['points'][-1]['gateway'] for k in ('backend_wait_max_seconds','write_active_max_seconds'))
   save('history-'+environment+'.json',data)
 save('complete.json',{'passed':True,'image':new,'backup':BACKUP,'other_containers_unchanged':True,'health':health});print(json.dumps({'passed':NAME,'image':new,'backup':BACKUP}),flush=True)
except BaseException as error:
 error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
 raise
