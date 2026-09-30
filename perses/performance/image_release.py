"""Run on test4 through SSH MCP. No connections or SSH subprocesses are started."""
import argparse,hashlib,json,os,subprocess,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
NAME='monitoring-perses'
BACKUP=NAME+'-before-perf1'
CANDIDATE=NAME+'-candidate-perf1'
PROTECTED=('monitoring-vm','monitoring-vmagent','monitoring-api')
BASE='http://122.247.53.162:18431'
CANDIDATE_BASE='http://127.0.0.1:18541'

class PersesClient:
 """Keep credentials and per-server access tokens on the deployment host."""
 def __init__(self,base):
  self.base=base.rstrip('/');self.token=None
  self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
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
 # Only the original perf.1 lock predates explicit upgrade/rollback baselines.
 if lock.get('candidate_version','0.54.0-perf.1')=='0.54.0-perf.1' and 'previous_image_digest' not in lock and 'previous_version' not in lock:
  return 'sha256:7f2b38e8c3d57f2643c02eef108888e125954a0c0d5d51f86235092012b7fb1d','0.54.0'
 assert lock.get('previous_image_digest') and lock.get('previous_version'),'Release lock must pin previous_image_digest and previous_version'
 return lock['previous_image_digest'],lock['previous_version']

def run(*args):return subprocess.check_output(args).decode().strip()
def inspect(name):return json.loads(run('docker','inspect',name))[0]
def find_container(name):
 ids=run('docker','container','ls','-aq','--filter','name=^/'+name+'$').split()
 assert len(ids)<=1,'Ambiguous container name'
 return inspect(ids[0]) if ids else None

def restore_original(old,image):
 """Reconcile Docker state even when a mutating command's response was lost."""
 current=find_container(NAME)
 if current and current['Id']!=old['Id']:
  backup=find_container(BACKUP)
  assert backup and backup['Id']==old['Id'],'Original backup changed; refusing removal'
  assert current['Image']==image and current['Config'].get('Labels',{}).get('monitoring.transaction')==old['Id'],'Concurrent container change'
  run('systemctl','stop','monitoring-perses.service')
  run('docker','rm','-f',current['Id'])
  current=None
 if current is None:
  backup=find_container(BACKUP)
  assert backup and backup['Id']==old['Id'],'Original backup missing'
  run('docker','rename',old['Id'],NAME)
 run('systemctl','start','monitoring-perses.service')
 assert inspect(NAME)['Id']==old['Id'] and inspect(old['Id'])['State']['Running'],'Original container failed to restart'

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
  error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
  raise
def health(base):
 for _ in range(80):
  try:
   with urllib.request.urlopen(base+'/api/v1/health',timeout=2) as r:return json.load(r)
  except OSError:time.sleep(.25)
 raise RuntimeError('Perses health timed out')
def resources(base=BASE):
 api=client(base)
 def ordered(items):return sorted(items,key=lambda item:item['metadata']['name'])
 projects=ordered(api.get('/api/v1/projects'))
 result={'projects':projects}
 for item in projects:
  project=item['metadata']['name'];encoded=urllib.parse.quote(project,safe='')
  for kind in ('dashboards','datasources'):
   result[project+'/'+kind]=ordered(api.get('/api/v1/projects/'+encoded+'/'+kind))
 return result
def protected():return [(n,inspect(n)['Id'],inspect(n)['State']['StartedAt']) for n in PROTECTED]
def save(root,name,data):(root/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
def validation_status(root,image,defer_browser=False,local_browser=False):
 # Legacy flags remain accepted by older callers; all upgrades use a downtime window.
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
 args += [image,'--config=/etc/perses/config.yaml','--web.listen-address='+listen,'--log.level=info']
 return run(*args)
def main():
 global BACKUP,CANDIDATE
 os.umask(0o077)
 p=argparse.ArgumentParser();p.add_argument('action',choices=['load','candidate','apply','rollback']);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--lock',type=Path,help='Explicit release lock for version-specific deployment or rollback.');p.add_argument('--defer-browser-validation',action='store_true',help='Legacy compatibility flag; upgrades always use a maintenance window.');p.add_argument('--local-browser-validation',action='store_true',help='Legacy compatibility flag; run affected browser checks after upgrade.');a=p.parse_args();r=a.evidence.resolve();assert r.is_dir()
 assert not a.defer_browser_validation or a.action=='apply'
 assert not a.local_browser_validation or a.action=='apply'
 lock=json.loads((a.lock or r/'release-lock.json').read_text());image=lock['candidate_config_digest']
 version=lock.get('candidate_version','0.54.0-perf.1');suffix=version.split('-')[-1].replace('.','');BACKUP=NAME+'-before-'+suffix;CANDIDATE=NAME+'-candidate-'+suffix
 previous_image,previous_version=previous_release(lock)
 if a.action=='load':
  archive=r/lock.get('candidate_archive_name','perses-perf.1.tar.gz');assert hashlib.sha256(archive.read_bytes()).hexdigest()==lock['candidate_archive_sha256'];run('docker','load','-i',str(archive));assert inspect('monitoring-perses:'+version)['Id']==image;print('archive and image verified');return
 if a.action=='candidate':
  old=inspect(NAME);assert old['Image']==previous_image
  assert find_container(CANDIDATE) is None,'Candidate name already exists'
  expected=resources()
  # A root-owned copy can pass /health while every resource request fails.
  # Match each source path's ownership, only within the candidate data copy.
  source=next(Path(m['Source']) for m in old['Mounts'] if m['Destination']=='/perses')
  copied=r/'candidate-data'
  for directory,dirs,files in os.walk(copied):
   for path in [Path(directory)]+[Path(directory)/name for name in files]:
    assert not path.is_symlink(),'Unexpected candidate symlink'
    stat=(source/path.relative_to(copied)).stat();os.chown(path,stat.st_uid,stat.st_gid)
  binds=[str(r/'candidate-data')+':/perses',str(r/'candidate-config.yaml')+':/etc/perses/config.yaml:ro']
  create(old,CANDIDATE,image,binds,'127.0.0.1:18541');run('docker','start',CANDIDATE)
  state=health(CANDIDATE_BASE);save(r,'candidate-health.json',state)
  assert state['version']==version and inspect(CANDIDATE)['Image']==image,'Candidate identity mismatch'
  assert resources(CANDIDATE_BASE)==expected,'Candidate resource copy differs'
  assert resources()==expected,'Production resources changed during candidate validation'
  report={'passed':True,'image':image,'resources_match_production':True,'projects':[p['metadata']['name'] for p in expected['projects']]}
  save(r,'candidate-resources.json',report);save(r,'candidate-api-validation.json',report);return
 if a.action=='rollback':
  current=inspect(NAME);assert current['Image']==image;run('systemctl','stop','monitoring-perses.service');run('docker','rm',NAME);run('docker','rename',BACKUP,NAME);run('systemctl','start','monitoring-perses.service');assert health(BASE)['version']==previous_version;save(r,'image-rollback.json',{'passed':True,'time':time.time()});return
 validation=validation_status(r,image,a.defer_browser_validation,a.local_browser_validation)
 apply_image(r,image,version,previous_image,validation)
if __name__=='__main__':main()
