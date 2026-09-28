"""Publish only the calculator fix. Invoke on test2 through SSH MCP."""
import hashlib,http.client,json,socket,subprocess,sys,time,urllib.request
from pathlib import Path

ROOT=Path('/data2/monitoring/evidence/request-rate-20260914')
NAME='monitoring-api'
TAG='monitoring-api:request-rate-20260914'
DEST='/monitoring/monitoring/calculator.py'
BEFORE='9c6ef47ddb4e0713abb4bf14a1ef8089473f67b2f57f1e50bff2b0be1619ca9f'

def cmd(*args):return subprocess.check_output(args,stderr=subprocess.STDOUT).decode()
def inspect(name):return json.loads(cmd('docker','inspect',name))[0]
def save(name,data):
 p=ROOT/name;p.write_text(json.dumps(data,indent=2));p.chmod(0o600)
def read(name):return json.loads((ROOT/name).read_text())
def api(path):
 with urllib.request.urlopen('http://127.0.0.1:18430'+path,timeout=10) as r:return json.load(r)
def protected():
 ids=cmd('docker','ps','-q').split()
 return {v['Name']:{'id':v['Id'],'started':v['State']['StartedAt'],'pid':v['State']['Pid']}
         for v in json.loads(cmd('docker','inspect',*ids)) if v['Name']!='/'+NAME}

def prepare():
 assert not (ROOT/'baseline.json').exists(),'Already prepared'
 ROOT.chmod(0o700);v=inspect(NAME)
 assert v['Config']['Labels']['monitoring.owner']=='independent'
 assert cmd('docker','exec',NAME,'sha256sum',DEST).split()[0]==BEFORE
 save('baseline.json',{'container':v,'protected':protected(),'latest':api('/api/monitoring/latest')})
 cmd('docker','cp',NAME+':'+DEST,str(ROOT/'calculator-before.py'))
 (ROOT/'Dockerfile').write_text('FROM '+v['Image']+'\nCOPY calculator.py '+DEST+'\n')
 with (ROOT/'build.log').open('w') as log:
  subprocess.run(['docker','build','-t',TAG,str(ROOT)],stdout=log,stderr=subprocess.STDOUT,check=True)
 cmd('docker','run','--rm','--network','none','--entrypoint','python3',TAG,'-c',
     'from monitoring.calculator import Calculator,request_counters; from monitoring.api import app; assert request_counters([]) is None')
 save('prepared.json',{'image':inspect(TAG)['Id']});print('candidate prepared')

class Connection(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')

def switch():
 b=read('baseline.json');v=inspect(NAME)
 assert v['Id']==b['container']['Id'] and protected()==b['protected']
 assert cmd('docker','exec',NAME,'sha256sum',DEST).split()[0]==BEFORE
 backup=NAME+'-request-rate-rollback-'+str(int(time.time()))
 save('transaction.json',{'backup':backup,'at':time.time()})
 fields=('User','ExposedPorts','Env','Cmd','Healthcheck','Volumes','WorkingDir','Entrypoint','Labels','StopSignal','StopTimeout','Hostname')
 config={k:v['Config'][k] for k in fields if k in v['Config']}
 config.update(Image=read('prepared.json')['image'],HostConfig=v['HostConfig'])
 cmd('docker','stop','--time','30',NAME);cmd('docker','rename',NAME,backup)
 try:
  c=Connection('localhost',timeout=30)
  c.request('POST','/v1.39/containers/create?name='+NAME,json.dumps(config),{'Content-Type':'application/json'})
  r=c.getresponse();body=r.read();c.close();assert r.status==201,body
  cmd('docker','start',NAME);deadline=time.time()+45
  while True:
   try:
    latest=api('/api/monitoring/latest');health=api('/health')
    assert health['status']=='ok'
    assert latest['nodes']['decode']['metrics']['data']['rates']['requests'] is not None
    assert protected()==b['protected']
    save('switched.json',{'at':time.time(),'latest':latest,'health':health,'protected_unchanged':True})
    break
   except Exception:
    if time.time()>=deadline:raise
    time.sleep(2)
 except BaseException as error:
  error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
  raise
 print('monitoring-api switched; request rate valid; protected containers unchanged')

def finalize():
 b=read('baseline.json');assert protected()==b['protected']
 latest=api('/api/monitoring/latest');history=api('/api/monitoring/history?hours=1')
 rate=latest['nodes']['decode']['metrics']['data']['rates']['requests'];assert rate is not None
 valid=[p for p in history['points'] if p['nodes']['decode'].get('requests') is not None]
 assert len(valid)>=2,'Need at least two valid chart points'
 expected=hashlib.sha256((ROOT/'calculator.py').read_bytes()).hexdigest()
 assert cmd('docker','exec',NAME,'sha256sum',DEST).split()[0]==expected
 source=Path('/data2/monitoring/release/monitoring/calculator.py')
 assert hashlib.sha256(source.read_bytes()).hexdigest()==BEFORE
 source.write_bytes((ROOT/'calculator.py').read_bytes())
 backup=read('transaction.json')['backup'];assert inspect(backup)['Id']==b['container']['Id']
 cmd('docker','rm',backup)
 report={'at':time.time(),'image':inspect(NAME)['Image'],'rate':rate,'valid_history_points':len(valid),
         'first_valid_ts':valid[0]['ts'],'last_valid_ts':valid[-1]['ts'],'protected_unchanged':True}
 save('complete.json',report);save('history.json',history);print(json.dumps(report))

{'prepare':prepare,'switch':switch,'finalize':finalize}[sys.argv[1]]()
