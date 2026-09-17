"""Start local baseline/candidate with synthetic data only; never contacts test4."""
import argparse,json,os,secrets,subprocess,time,urllib.request
from pathlib import Path
ROOT=Path('/tmp/perses-performance-local')
PROJECTS=Path(__file__).resolve().parents[1]/'projects'

def selected_images(lock_path):
 lock=json.loads(lock_path.read_text())
 baseline=json.loads((PROJECTS.parent/'image-lock.json').read_text())
 images=[('candidate',18541,lock['candidate_config_digest'],lock['candidate_version']),
         ('baseline',18542,baseline['image_id'],baseline['version'].removeprefix('v'))]
 for name,_,image,version in images:
  actual=json.loads(subprocess.check_output(['docker','image','inspect',image]))[0]
  assert actual['Id']==image and actual['Os']=='linux' and actual['Architecture']=='amd64','Image digest or architecture mismatch: '+name
  if name=='candidate':
   assert actual['Config'].get('Labels',{}).get('monitoring.patch')=='perses-'+version,'Candidate version mismatch'
  output=subprocess.check_output(['docker','run','--rm','--network','none','--platform','linux/amd64',
                                 '--entrypoint','/bin/perses',image,'--version'],stderr=subprocess.STDOUT).decode()
  import re
  assert re.search(r'\bversion\s+'+re.escape(version)+r'(?=\s|,|$)',output),'Binary version mismatch: '+name
 return [(name,port,image) for name,port,image,_ in images]

def main():
 parser=argparse.ArgumentParser()
 parser.add_argument('--lock',type=Path,default=Path(__file__).with_name('release-lock.json'))
 args=parser.parse_args()
 images=selected_images(args.lock)
 ROOT.mkdir(exist_ok=True)
 for name,port,image in images:
  data=ROOT/name;data.mkdir(exist_ok=True);data.chmod(0o777)
  config=ROOT/(name+'.yaml');config.write_text('security:\n  enable_auth: false\n  readonly: false\n  encryption_key: '+json.dumps(secrets.token_urlsafe(24))+'\ndatabase:\n  file:\n    folder: /perses\n    extension: json\nephemeral_dashboard:\n  enable: false\n')
  subprocess.run(['docker','run','-d','--name','perses-perf-local-'+name,'--platform','linux/amd64','--cpus','1','--memory','1g','--cap-drop','ALL','--security-opt','no-new-privileges:true','-p','127.0.0.1:'+str(port)+':8080','-v',str(data)+':/perses','-v',str(config)+':/etc/perses/config.yaml:ro',image,'--config=/etc/perses/config.yaml','--web.listen-address=0.0.0.0:8080'],check=True)
  base='http://127.0.0.1:'+str(port)
  for _ in range(120):
   try:urllib.request.urlopen(base+'/api/v1/health',timeout=1).close();break
   except OSError:time.sleep(.25)
  else:raise RuntimeError('startup timed out')
  for project in sorted(PROJECTS.iterdir()):
   paths=[project/'project.json',project/'datasource.json',*sorted((project/'dashboards').glob('*.json'))]
   for p in paths:
    d=json.loads(p.read_text());kind=d['kind'];route='/api/v1/projects'
    if kind!='Project':route+='/'+project.name+'/'+('datasources' if kind=='Datasource' else 'dashboards')
    if kind=='Datasource':d['spec']['plugin']['spec']['proxy']['spec']['url']='http://host.docker.internal:18543'
    req=urllib.request.Request(base+route,data=json.dumps(d).encode(),headers={'Content-Type':'application/json'})
    urllib.request.urlopen(req,timeout=15).close()
  print(name,'ready',base)
if __name__=='__main__':main()
