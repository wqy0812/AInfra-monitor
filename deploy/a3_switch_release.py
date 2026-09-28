"""Bounded image patch release. Run on test4/test1 exclusively through SSH MCP."""
import base64,hashlib,http.client,json,os,socket,sqlite3,subprocess,sys,time,urllib.request
from pathlib import Path
ROOT=Path(sys.argv[1]);ACTION=sys.argv[2]
PAYLOAD=json.loads((ROOT/'payload.json').read_text());NAME=PAYLOAD['container'];TAG=NAME+':a3-switch-20260914'
assert NAME in ('monitoring-api','code-eval-web')
BASE='http://127.0.0.1:'+('18430' if NAME=='monitoring-api' else '18080')
def cmd(*args):return subprocess.check_output(args,stderr=subprocess.STDOUT).decode()
def inspect(name):return json.loads(cmd('docker','inspect',name))[0]
def save(name,value):
 p=ROOT/name;p.write_text(json.dumps(value,ensure_ascii=False,indent=2));p.chmod(0o600)
def read(name):return json.loads((ROOT/name).read_text())
def get(path):
 with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(BASE+path,timeout=12) as r:return json.load(r)
def protected():
 ids=cmd('docker','ps','-q').split()
 return {v['Name']:{'id':v['Id'],'started':v['State']['StartedAt'],'pid':v['State']['Pid']} for v in json.loads(cmd('docker','inspect',*ids)) if v['Name']!='/'+NAME and '-a3-rollback-' not in v['Name']}
def business():
 if NAME!='code-eval-web':return None
 with sqlite3.connect('file:/data2/code-eval/data/platform.db?mode=ro',uri=True) as db:
  out={}
  tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  for table in ('runs','samples','profiles','stress_rounds','stress_requests','archived_runs','task_schedule'):
   if table not in tables:continue
   h=hashlib.sha256();count=0
   for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid'):
    h.update(json.dumps(row,ensure_ascii=False,default=str).encode());h.update(b'\n');count+=1
   out[table]={'rows':count,'sha256':h.hexdigest()}
  row=db.execute("SELECT value FROM state WHERE key='maintenance'").fetchone();out['maintenance']=row[0] if row else None
  return out

def prepare():
 assert not (ROOT/'prepared.json').exists()
 v=inspect(NAME);assert v['State']['Running']
 assert not any(m['Destination'].startswith('/app' if NAME=='code-eval-web' else '/monitoring') for m in v['Mounts'])
 if NAME=='code-eval-web':assert get('/api/task-capacity')['active_count']==0
 save('baseline.json',{'container':v,'protected':protected(),'business':business()})
 build=ROOT/'build';build.mkdir(exist_ok=True)
 for item in PAYLOAD['files']:
  dest=item['destination'];old=item['before_sha256']
  if old is None:assert subprocess.run(['docker','exec',NAME,'test','-e',dest]).returncode==1
  else:assert cmd('docker','exec',NAME,'sha256sum',dest).split()[0]==old,dest+' baseline changed'
  source=build/item['relative'];source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(base64.b64decode(item['content']))
  assert hashlib.sha256(source.read_bytes()).hexdigest()==item['sha256']
 (build/'Dockerfile').write_text('FROM '+v['Image']+'\n'+''.join('COPY '+i['relative']+' '+i['destination']+'\n' for i in PAYLOAD['files']))
 with (ROOT/'build.log').open('w') as log:subprocess.run(['docker','build','-t',TAG,str(build)],stdout=log,stderr=subprocess.STDOUT,check=True)
 expected={i['destination']:i['sha256'] for i in PAYLOAD['files']}
 check='import hashlib,json;from pathlib import Path;expected=json.loads('+repr(json.dumps(expected))+');assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in expected.items());'+PAYLOAD['smoke']
 cmd('docker','run','--rm','--network','none','--entrypoint','python3',TAG,'-c',check)
 save('prepared.json',{'image':inspect(TAG)['Id'],'tag':TAG});print(json.dumps({'prepared':NAME,'image':inspect(TAG)['Id']}))
class Connection(http.client.HTTPConnection):
 def connect(self):self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')
def rollback():
 tx=read('transaction.json');backup=tx['backup'];baseline=read('baseline.json')
 current=cmd('docker','ps','-aq','--filter','name=^/'+NAME+'$').strip()
 if not current or inspect(NAME)['Id']!=baseline['container']['Id']:
  if current:
   assert inspect(NAME)['Image']==read('prepared.json')['image'];cmd('docker','rm','-f',NAME)
  cmd('docker','rename',backup,NAME);cmd('docker','start',NAME)
 elif not inspect(NAME)['State']['Running']:cmd('docker','start',NAME)
 save('rollback.json',{'at':time.time()});print('Rolled back '+NAME)
def switch():
 baseline=read('baseline.json');v=inspect(NAME)
 assert v['Id']==baseline['container']['Id'] and protected()==baseline['protected'] and business()==baseline['business']
 if NAME=='code-eval-web':assert get('/api/task-capacity')['active_count']==0
 backup=NAME+'-a3-rollback-'+str(int(time.time()));save('transaction.json',{'backup':backup,'at':time.time()})
 fields=('User','ExposedPorts','Env','Cmd','Healthcheck','Volumes','WorkingDir','Entrypoint','Labels','StopSignal','StopTimeout','Hostname')
 config={k:v['Config'][k] for k in fields if k in v['Config']};config.update(Image=read('prepared.json')['image'],HostConfig=v['HostConfig'])
 try:
  cmd('docker','stop','--time','30',NAME);cmd('docker','rename',NAME,backup)
  c=Connection('localhost',timeout=30);c.request('POST','/v1.39/containers/create?name='+NAME,json.dumps(config),{'Content-Type':'application/json'});r=c.getresponse();body=r.read();c.close();assert r.status==201,body;cmd('docker','start',NAME)
  deadline=time.time()+45
  while True:
   try:
    for env in ('dcu-pd','a3-vllm'):
     data=get('/api/monitoring/latest?environment='+env);assert data['environment']==env and time.time()-data['ts']<20
     assert all(data['nodes'][role]['metrics']['status']=='ok' for role in ('prefill','decode'))
    if NAME=='code-eval-web':assert get('/api/health')['components']['engine']['status']=='ok'
    break
   except Exception:
    if time.time()>deadline:raise
    time.sleep(2)
  assert protected()==baseline['protected'] and business()==baseline['business']
  save('switched.json',{'at':time.time(),'container':NAME,'id':inspect(NAME)['Id'],'protected_unchanged':True,'business_unchanged':True})
 except BaseException as error:
  error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
  raise
 print(json.dumps(read('switched.json')))

def finalize():
 assert read('acceptance.json')['passed']
 baseline=read('baseline.json');assert protected()==baseline['protected'] and business()==baseline['business']
 save('complete.json',{'at':time.time(),'container':NAME,'image':inspect(NAME)['Image'],'protected_unchanged':True,'business_unchanged':True,'rollback_container':read('transaction.json')['backup']})
 print(json.dumps(read('complete.json')))
{'prepare':prepare,'switch':switch,'rollback':rollback,'finalize':finalize}[ACTION]()
