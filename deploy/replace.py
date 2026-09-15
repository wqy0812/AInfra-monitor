"""Explicit replacement of an owned monitoring container; uses a temporary rollback container only during replacement."""
import http.client,json,socket,subprocess,sys,time
name,image=sys.argv[1:3]
def cmd(*args):return subprocess.check_output(args).decode()
v=json.loads(cmd('docker','inspect',name))[0]
assert v['Config'].get('Labels',{}).get('monitoring.owner')=='independent'
class Connection(http.client.HTTPConnection):
 def connect(self):self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')
fields=('User','ExposedPorts','Env','Cmd','Healthcheck','Volumes','WorkingDir','Entrypoint','Labels','StopSignal','StopTimeout')
config={k:v['Config'][k] for k in fields if k in v['Config']};config.update(Image=image,HostConfig=v['HostConfig'])
if '--driver-readonly' in sys.argv:
 assert name=='monitoring-dcu'
 config['HostConfig']['Binds']=list(config['HostConfig'].get('Binds') or [])+['/opt/hyhal:/opt/hyhal:ro']
if '--loadavg' in sys.argv:
 assert name=='monitoring-node'
 if '--collector.loadavg' not in config['Cmd']:config['Cmd'].append('--collector.loadavg')
backup=name+'-rollback-'+str(int(time.time()))
cmd('docker','stop',name);cmd('docker','rename',name,backup)
try:
 c=Connection('localhost');c.request('POST','/v1.39/containers/create?name='+name,json.dumps(config),{'Content-Type':'application/json'});r=c.getresponse();body=r.read();assert r.status==201,body;c.close();cmd('docker','start',name)
except BaseException:
 subprocess.run(['docker','rm','-f',name]);cmd('docker','rename',backup,name);cmd('docker','start',name);raise
cmd('docker','rm',backup)
print(json.dumps({'container':name,'temporary_container_removed':backup,'image':image}))
