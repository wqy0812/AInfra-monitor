"""Run locally ON test4 via SSH MCP. Does not manage any existing service."""
import hashlib,json,os,secrets,subprocess,time
from pathlib import Path
ROOT=Path('/data2/monitoring/perses'); RELEASE=ROOT/'release'
def run(*args):return subprocess.check_output(args,text=True)
assert run('hostname').strip()=='dkfhc8287nap002'
assert not subprocess.run(['docker','container','inspect','monitoring-perses'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0,'Perses already exists; use explicit upgrade procedure'
archive=ROOT/'perses-v0.54.0-amd64.tar.gz'
manifest=json.loads((RELEASE/'image-lock.json').read_text())
assert hashlib.sha256(archive.read_bytes()).hexdigest()==manifest['archive_sha256']
protected=json.loads(run('docker','inspect','monitoring-vm','monitoring-vmagent','monitoring-api'))
(ROOT/'evidence').mkdir(exist_ok=True)
(ROOT/'evidence'/'protected-before.json').write_text(json.dumps([{k:v[k] for k in ('Id','Name','State')} for v in protected],indent=2))
(ROOT/'evidence'/'iptables-before.txt').write_text(run('iptables-save'))
print(run('docker','load','-i',str(archive)))
image=json.loads(run('docker','image','inspect','persesdev/perses:v0.54.0'))[0]
assert image['Id']==manifest['image_id'] and image['Architecture']=='amd64'
(ROOT/'data').mkdir(exist_ok=True);os.chown(ROOT/'data',65532,65532)
config=ROOT/'config.yaml'
assert not config.exists()
config.write_text('security:\n  enable_auth: false\n  readonly: false\n  encryption_key: '+json.dumps(secrets.token_urlsafe(24))+'\ndatabase:\n  file:\n    folder: /perses\n    extension: json\nephemeral_dashboard:\n  enable: false\n')
os.chmod(config,0o640);os.chown(config,0,65532)
os.chmod(RELEASE/'access.sh',0o755)
for name in ('perses-access.service','monitoring-perses.service'):
 target=Path('/etc/systemd/system')/name
 assert not target.exists(),str(target)+' exists'
 target.write_text((RELEASE/name).read_text())
print(run('docker','create','--name','monitoring-perses','--network','host','--restart','no','--cpus','1','--memory','1g','--cap-drop','ALL','--security-opt','no-new-privileges:true','--log-opt','max-size=10m','--log-opt','max-file=3','--label','monitoring.owner=perses','-v',str(ROOT/'data')+':/perses','-v',str(config)+':/etc/perses/config.yaml:ro',manifest['image_id'],'--config=/etc/perses/config.yaml','--web.listen-address=122.247.53.162:18431','--log.level=info'))
run('systemctl','daemon-reload');run('systemctl','enable','--now','monitoring-perses.service')
print('Perses started; dashboards must be seeded once with seed.py.')
