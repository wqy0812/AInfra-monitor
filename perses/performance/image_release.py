"""Run on test4 through SSH MCP. No connections or SSH subprocesses are started."""
import argparse,hashlib,json,os,subprocess,time,urllib.request
from pathlib import Path
NAME='monitoring-perses'
BACKUP=NAME+'-before-perf1'
CANDIDATE=NAME+'-candidate-perf1'
PROTECTED=('monitoring-vm','monitoring-vmagent','monitoring-api')
BASE='http://122.247.53.162:18431'

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
 assert old['Image']==previous_image and old['State']['Running'],'Original image or state changed'
 before=resources();fp=protected()
 save(root,'image-apply-before.json',{'container':old,'resources':before,'protected':fp})
 assert resources()==before and protected()==fp and inspect(NAME)['Id']==old['Id'],'Concurrent change before cutover'
 try:
  run('systemctl','stop','monitoring-perses.service')
  run('docker','rename',old['Id'],BACKUP)
  create(old,NAME,image,old['HostConfig']['Binds'],'122.247.53.162:18431')
  run('systemctl','start','monitoring-perses.service')
  assert health(BASE)['version']==version
  current=inspect(NAME)
  assert current['Image']==image and current['State']['Running'] and current.get('RestartCount',0)==0
  assert resources()==before,'Resources changed'
  assert protected()==fp,'Protected services changed'
  save(root,'image-publication.json',{'passed':True,'image':image,'time':time.time(),'resources_unchanged':True,'protected_unchanged':True,'validation':validation})
 except BaseException:
  restore_original(old,image)
  raise
def health(base):
 for _ in range(80):
  try:
   with urllib.request.urlopen(base+'/api/v1/health',timeout=2) as r:return json.load(r)
  except OSError:time.sleep(.25)
 raise RuntimeError('Perses health timed out')
def resources():
 result={}
 for project in ('a3-monitoring','dcu-monitoring'):
  for kind in ('dashboards','datasources'):
   with urllib.request.urlopen(BASE+'/api/v1/projects/'+project+'/'+kind,timeout=15) as r:result[project+'/'+kind]=json.load(r)
 return result
def protected():return [(n,inspect(n)['Id'],inspect(n)['State']['StartedAt']) for n in PROTECTED]
def save(root,name,data):(root/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
def validation_status(root,image,defer_browser):
 if defer_browser:
  authorization=json.loads((root/'deployment-authorization.json').read_text())
  assert authorization['explicit_user_instruction'] and authorization['image']==image
  assert authorization['scope']=='deploy-perses-performance'
  api=json.loads((root/'candidate-api-validation.json').read_text())
  assert api['passed'] and api['image']==image
  return {'mode':'user-authorized-production-validation','user_instruction':authorization['user_instruction'],'deferred_checks':['remote-candidate-browser','remote-candidate-1800-second-soak']}
 acceptance=json.loads((root/'remote-browser-acceptance.json').read_text());assert acceptance['passed'] and acceptance['environment']=='remote-candidate' and acceptance['image']==image
 soak=json.loads((root/'remote-soak.json').read_text());assert soak['passed'] and soak['elapsed_seconds']>=1800 and soak['image']==image
 return {'mode':'candidate-validated','deferred_checks':[]}
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
 p=argparse.ArgumentParser();p.add_argument('action',choices=['load','candidate','apply','rollback']);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--lock',type=Path,help='Explicit release lock for version-specific deployment or rollback.');p.add_argument('--defer-browser-validation',action='store_true',help='Requires recorded explicit user deployment authorization; validate on production after cutover.');a=p.parse_args();r=a.evidence;assert r.is_dir()
 assert not a.defer_browser_validation or a.action=='apply'
 lock=json.loads((a.lock or r/'release-lock.json').read_text());image=lock['candidate_config_digest']
 version=lock.get('candidate_version','0.54.0-perf.1');suffix=version.split('-')[-1].replace('.','');BACKUP=NAME+'-before-'+suffix;CANDIDATE=NAME+'-candidate-'+suffix
 previous_image=lock.get('previous_image_digest','sha256:7f2b38e8c3d57f2643c02eef108888e125954a0c0d5d51f86235092012b7fb1d');previous_version=lock.get('previous_version','0.54.0')
 if a.action=='load':
  archive=r/lock.get('candidate_archive_name','perses-perf.1.tar.gz');assert hashlib.sha256(archive.read_bytes()).hexdigest()==lock['candidate_archive_sha256'];run('docker','load','-i',str(archive));assert inspect('monitoring-perses:'+version)['Id']==image;print('archive and image verified');return
 if a.action=='candidate':
  old=inspect(NAME);assert old['Image']==previous_image
  # A root-owned copy can pass /health while every resource request fails.
  # Match each source path's ownership, only within the candidate data copy.
  source=next(Path(m['Source']) for m in old['Mounts'] if m['Destination']=='/perses')
  copied=r/'candidate-data'
  for directory,dirs,files in os.walk(copied):
   for path in [Path(directory)]+[Path(directory)/name for name in files]:
    assert not path.is_symlink(),'Unexpected candidate symlink'
    stat=(source/path.relative_to(copied)).stat();os.chown(path,stat.st_uid,stat.st_gid)
  binds=[str(r/'candidate-data')+':/perses',str(r/'candidate-config.yaml')+':/etc/perses/config.yaml:ro']
  create(old,CANDIDATE,image,binds,'127.0.0.1:18541');run('docker','start',CANDIDATE);save(r,'candidate-health.json',health('http://127.0.0.1:18541'))
  for key,expected in resources().items():
   project,kind=key.split('/');url='http://127.0.0.1:18541/api/v1/projects/'+project+'/'+kind
   with urllib.request.urlopen(url,timeout=15) as response:assert json.load(response)==expected,'Candidate resource copy differs: '+key
  save(r,'candidate-resources.json',{'passed':True,'image':image,'resources_match_production':True});return
 if a.action=='rollback':
  current=inspect(NAME);assert current['Image']==image;run('systemctl','stop','monitoring-perses.service');run('docker','rm',NAME);run('docker','rename',BACKUP,NAME);run('systemctl','start','monitoring-perses.service');assert health(BASE)['version']==previous_version;save(r,'image-rollback.json',{'passed':True,'time':time.time()});return
 validation=validation_status(r,image,a.defer_browser_validation)
 apply_image(r,image,version,previous_image,validation)
if __name__=='__main__':main()
