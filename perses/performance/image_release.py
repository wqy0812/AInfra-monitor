"""Run on test4 through SSH MCP. No connections or SSH subprocesses are started."""
import argparse,hashlib,json,os,subprocess,sys,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_support import save, record_failure, snapshot as resource_snapshot
from connection import BASE, opener, urlopen
NAME='monitoring-perses'
BACKUP=NAME+'-before-perf1'

class PersesClient:
 """Keep credentials and per-server access tokens on the deployment host."""
 def __init__(self,base):
  self.base=base.rstrip('/');self.token=None
  self.opener=opener(direct=True)
 def login(self):
  credentials=Path(os.environ.get('PERSES_CREDENTIALS_FILE','/data2/monitoring/perses/admin-credentials.json'))
  request=urllib.request.Request(self.base+'/api/auth/providers/native/login',data=credentials.read_bytes(),headers={'Content-Type':'application/json'})
  with self.opener.open(request,timeout=20) as response:self.token=json.load(response)['access_token']
  assert isinstance(self.token,str) and self.token,'Missing Perses access token'
 def get(self,path):
  assert path.startswith('/') and not path.startswith('//'),'Expected a local API path'
  for attempt in range(2):
   if self.token is None:self.login()
   request=urllib.request.Request(self.base+path,headers={'Authorization':'Bearer '+self.token})
   try:
    with self.opener.open(request,timeout=20) as response:return json.load(response)
   except urllib.error.HTTPError as error:
    if error.code==401 and attempt==0:self.token=None;continue
    raise RuntimeError('Perses GET failed: HTTP '+str(error.code)+' '+path) from None

CLIENTS={}
def client(base):
 if base not in CLIENTS:CLIENTS[base]=PersesClient(base)
 return CLIENTS[base]

def previous_release(lock):
 assert lock.get('previous_image_digest') and lock.get('previous_version'),'Release lock must pin previous_image_digest and previous_version'
 return lock['previous_image_digest'],lock['previous_version']

def run(*args):return subprocess.check_output(args).decode().strip()
def inspect(name):return json.loads(run('docker','inspect',name))[0]
def find_container(name):
 ids=run('docker','container','ls','-aq','--filter','name=^/'+name+'$').split()
 assert len(ids)<=1,'Ambiguous container name'
 return inspect(ids[0]) if ids else None


def apply_image(root,image,version,previous_image,validation):
 candidate=inspect(image)
 assert candidate['Id']==image and candidate['Architecture']=='amd64' and candidate['Os']=='linux','Candidate image mismatch'
 assert candidate['Config'].get('Labels',{}).get('monitoring.patch')=='perses-'+version,'Candidate version mismatch'
 assert find_container(BACKUP) is None,'Backup name already exists; reconcile before publication'
 old=inspect(NAME)
 assert old['Image']==previous_image,'Original image changed'
 before=resources()
 save(root,'image-apply-before.json',{'container':old,'resources':before})
 assert resources()==before and inspect(NAME)['Id']==old['Id'],'Concurrent change before cutover'
 try:
  run('systemctl','stop','monitoring-perses.service')
  run('docker','rename',old['Id'],BACKUP)
  create(old,NAME,image,old['HostConfig']['Binds'],'122.247.53.162:18431')
  run('systemctl','start','monitoring-perses.service')
  assert health(BASE)['version']==version
  current=inspect(NAME)
  assert current['Image']==image and current['State']['Running'] and not current['State'].get('Restarting')
  assert resources()==before,'Resources changed'
  save(root,'image-publication.json',{'passed':True,'image':image,'time':time.time(),'resources_unchanged':True,'validation':validation})
 except BaseException as error:
  record_failure(root, 'image-publication-failure.json', error)
  raise
def health(base):
 for _ in range(80):
  try:
   with urlopen(base+'/api/v1/health',timeout=2) as r:return json.load(r)
  except OSError:time.sleep(.25)
 raise RuntimeError('Perses health timed out')
def resources(base=BASE):
 result=resource_snapshot(client(base).get, grouped=True)
 return {key: sorted(items, key=lambda item: item['metadata']['name']) for key, items in result.items()}

def validation_status(root,image):
 return {'mode':'maintenance-window','image':image,
         'post_upgrade_checks':['version','resources'],
         'checks_not_run':['candidate-browser','candidate-1800-second-soak']}
def create(old,name,image,binds,listen):
 cfg=old['Config'];host=old['HostConfig']
 assert host['NetworkMode']=='host' and not host['Privileged']
 assert host.get('CapDrop')==['ALL'] and 'no-new-privileges:true' in host.get('SecurityOpt',[])
 args=['docker','create','--name',name,'--network','host','--restart','no','--cpus',str(host['NanoCpus']/1e9),'--memory',str(host['Memory']),'--cap-drop','ALL','--security-opt','no-new-privileges:true']
 for k,v in (host.get('LogConfig',{}).get('Config') or {}).items():args+=['--log-opt',k+'='+v]
 if cfg.get('User'):args+=['--user',cfg['User']]
 if cfg.get('WorkingDir'):args+=['--workdir',cfg['WorkingDir']]
 for e in cfg.get('Env',[]):args+=['--env',e]
 for k,v in (cfg.get('Labels') or {}).items():
  if k not in ('monitoring.patch','monitoring.transaction'):args+=['--label',k+'='+v]
 args+=['--label','monitoring.release=performance-20260916']
 args+=['--label','monitoring.transaction='+old['Id']]
 for bind in binds:args+=['--volume',bind]
 # Preserve TLS and other deployed flags during later image upgrades.
 command=list(cfg['Cmd'])
 for i,value in enumerate(command):
  if value.startswith('--web.listen-address='):command[i]='--web.listen-address='+listen;break
 else:raise ValueError('Missing explicit listen address')
 args += [image,*command]
 return run(*args)
def main():
 global BACKUP
 os.umask(0o077)
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument('action', choices=('load', 'apply'))
 parser.add_argument('--evidence', type=Path, required=True)
 parser.add_argument('--lock', type=Path, help='Release lock pinning the image and upgrade baseline')
 args = parser.parse_args()
 root = args.evidence.resolve()
 assert root.is_dir(), 'Evidence directory must already exist'
 lock = json.loads((args.lock or root / 'release-lock.json').read_text())
 image, version = lock['candidate_config_digest'], lock['candidate_version']
 BACKUP = NAME + '-before-' + version.split('-')[-1].replace('.', '')
 previous_image, _ = previous_release(lock)
 if args.action == 'load':
  archive = root / lock['candidate_archive_name']
  assert hashlib.sha256(archive.read_bytes()).hexdigest() == lock['candidate_archive_sha256']
  run('docker', 'load', '-i', str(archive))
  assert inspect('monitoring-perses:' + version)['Id'] == image
  print('archive and image verified')
 else:
  assert not (root / 'image-apply-before.json').exists(), 'Use a fresh evidence directory'
  apply_image(root, image, version, previous_image, validation_status(root, image))


if __name__ == '__main__':
 main()
