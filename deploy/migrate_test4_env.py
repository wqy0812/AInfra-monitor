"""Run on each host through SSH MCP; replace only explicitly named containers."""
import http.client,json,pathlib,socket,subprocess,sys,time,urllib.request

NAME=sys.argv[1]
assert NAME in ('monitoring-dcu','code-eval-web','code-eval-engine')
ROOT=pathlib.Path('/data2/monitoring/migrate-test4-20260914')
ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
def cmd(*args):return subprocess.check_output(args).decode()
def inspect(name):return json.loads(cmd('docker','inspect',name))[0]
def get(url):return json.load(urllib.request.urlopen(url,timeout=10))
def save(name,value):
 p=ROOT/name;p.write_text(json.dumps(value,indent=2));p.chmod(0o600)
class Connection(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')

v=inspect(NAME)
env=dict(e.split('=',1) for e in v['Config']['Env'])
if NAME=='monitoring-dcu':
 assert v['Config'].get('Labels',{}).get('monitoring.owner')=='independent'
 old=env['ALLOWED_CLIENTS'];new=','.join(dict.fromkeys(old.split(',')+['122.247.53.162']))
 key='ALLOWED_CLIENTS'
 protected={d['Name']:(d['Id'],d['State']['StartedAt'],d['State']['Pid']) for d in json.loads(cmd('docker','inspect',*cmd('docker','ps','-q').split())) if d['Name']!='/monitoring-dcu'}
else:
 key='MONITOR_QUERY_URL';old=env[key];new='http://122.247.53.162:18430'
 assert old=='http://122.247.53.180:18430', 'Unexpected source endpoint'
 capacity=get('http://127.0.0.1:18080/api/task-capacity')
 assert not capacity['active'] and not capacity['reservations'], 'Evaluation activity present'
 assert get(new+'/health')['status']=='ok'
 protected={}
if old==new:raise SystemExit('Already configured')
fields=('User','ExposedPorts','Env','Cmd','Healthcheck','Volumes','WorkingDir','Entrypoint','Labels','StopSignal','StopTimeout','Hostname')
config={k:v['Config'][k] for k in fields if k in v['Config']}
config['Env']=[e for e in config['Env'] if e.split('=',1)[0]!=key]+[key+'='+new]
config.update(Image=v['Image'],HostConfig=v['HostConfig'])
backup=NAME+'-test4-rollback-'+str(int(time.time()))
save(NAME+'-before-private.json',v)
save(NAME+'-transaction.json',{'name':NAME,'backup':backup,'old_id':v['Id'],'state':'prepared'})
cmd('docker','stop','--time','30',NAME);cmd('docker','rename',NAME,backup)
try:
 c=Connection('localhost',timeout=30);c.request('POST','/v1.39/containers/create?name='+NAME,json.dumps(config),{'Content-Type':'application/json'})
 r=c.getresponse();body=r.read();c.close();assert r.status==201,'Container creation failed: '+str(r.status)
 cmd('docker','start',NAME)
 deadline=time.time()+45
 while True:
  try:
   if NAME=='monitoring-dcu':
    request=urllib.request.urlopen('http://'+env['BIND']+':19500/metrics',timeout=5)
    assert 'dcu_sample_success 1' in request.read().decode()
   elif NAME=='code-eval-web':assert get('http://127.0.0.1:18080/api/monitoring/latest')['source']=='victoriametrics'
   else:assert get('http://127.0.0.1:18080/api/health')['components']['engine']['status']=='ok'
   break
  except Exception:
   if time.time()>deadline:raise
   time.sleep(1)
 for n,s in protected.items():
  d=inspect(n);assert (d['Id'],d['State']['StartedAt'],d['State']['Pid'])==s,'Protected container changed'
 result={'name':NAME,'backup':backup,'old_id':v['Id'],'new_id':inspect(NAME)['Id'],'changed_key':key,'state':'complete','protected_unchanged':len(protected)}
 save(NAME+'-transaction.json',result);print(json.dumps(result))
except BaseException:
 subprocess.run(['docker','rm','-f',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 cmd('docker','rename',backup,NAME);cmd('docker','start',NAME)
 raise
