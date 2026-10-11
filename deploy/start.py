"""Run locally on the deployment host. Never connects over SSH."""
import argparse,json,os,pathlib,subprocess,sys
ROOT=pathlib.Path('/data2/monitoring');RELEASE=ROOT/'release'
def api_clients(value=None):
 sys.path.insert(0,str(RELEASE))
 from monitoring.access import client_allowlist
 return ','.join(client_allowlist(value if value is not None else os.environ.get('ALLOWED_CLIENTS')))
def run(*args):return subprocess.check_output(args,stderr=subprocess.STDOUT).decode()
def exists(name):return subprocess.run(['docker','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
def start(name,image,args,options=(),entrypoint=None):
 if exists(name):raise RuntimeError('Container already exists; use explicit upgrade: '+name)
 cmd=['docker','run','-d','--name',name,'--restart','unless-stopped','--network','host','--label','monitoring.owner=independent','--log-opt','max-size=10m','--log-opt','max-file=3',*options]
 if entrypoint:cmd+=['--entrypoint',entrypoint]
 print(run(*cmd,image,*args))
def central(allowed_clients):
 # Build/load committed images first; first install never embeds a stale API tag.
 import re
 images={key:os.environ.get(key,'') for key in ('MONITOR_VM_IMAGE','MONITOR_API_IMAGE','MONITOR_NATIVE_IMAGE')}
 for key,value in images.items():
  if not re.fullmatch(r'sha256:[0-9a-f]{64}',value):raise ValueError(key+' must be an immutable local image ID')
  run('docker','image','inspect',value)
 for name in ('monitoring-vm','monitoring-vmagent','monitoring-vmalert','monitoring-api'):
  if exists(name):raise RuntimeError('Existing install; use deploy/release.py: '+name)
 for d in ('vm','buffer','state','evidence'):(ROOT/d).mkdir(parents=True,exist_ok=True)
 start('monitoring-vm',images['MONITOR_VM_IMAGE'],['-storageDataPath=/storage','-retentionPeriod=30d','-httpListenAddr=127.0.0.1:18428','-memory.allowedBytes=1536MiB','-storage.minFreeDiskSpaceBytes=20GiB'],['--cpus','4','--memory','2g','-v',str(ROOT/'vm')+':/storage'],'/vm')
 start('monitoring-vmagent',images['MONITOR_VM_IMAGE'],['-promscrape.config=/config/scrape.yml','-remoteWrite.url=http://127.0.0.1:18428/api/v1/write','-remoteWrite.tmpDataPath=/buffer','-remoteWrite.maxDiskUsagePerURL=5GiB','-httpListenAddr=127.0.0.1:18429','-memory.allowedBytes=384MiB'],['--cpus','0.5','--memory','512m','-v',str(ROOT/'buffer')+':/buffer','-v',str(RELEASE/'deploy')+':/config:ro'],'/vmagent')
 start('monitoring-vmalert',images['MONITOR_NATIVE_IMAGE'],['--listen=127.0.0.1:18432'],['--cpus','1','--memory','768m'])
 start('monitoring-api',images['MONITOR_API_IMAGE'],['-m','uvicorn','monitoring.api:app','--host','0.0.0.0','--port','18430','--no-access-log','--no-proxy-headers'],['--cpus','2','--memory','512m','-e','ALLOWED_CLIENTS='+allowed_clients],'python')

def main():
 p=argparse.ArgumentParser();p.add_argument('mode',choices=['central','node']);p.add_argument('--bind');p.add_argument('--allowed-clients');args=p.parse_args()
 if args.mode=='central':central(api_clients(args.allowed_clients))
 else:
  assert args.bind
  image=run('docker','inspect','kongmx-deepseek-v4-0828','--format','{{.Image}}').strip()
  (RELEASE/'Dockerfile.node').write_text('FROM '+image+'\nWORKDIR /monitoring\nCOPY monitoring/exporter.py /monitoring/exporter.py\nCOPY monitoring/compat.py /monitoring/compat.py\nCOPY vendor/bin/node_exporter /monitoring/node_exporter\nHEALTHCHECK NONE\n')
  print(run('docker','build','-f',str(RELEASE/'Dockerfile.node'),'-t','monitoring-node:20260913.1',str(RELEASE)))
  start('monitoring-node','monitoring-node:20260913.1',['--web.listen-address=127.0.0.1:19100','--path.rootfs=/host','--collector.disable-defaults','--collector.cpu','--collector.meminfo','--collector.filesystem','--collector.netdev','--collector.diskstats','--collector.stat','--collector.time','--collector.loadavg'],['--cpus','0.5','--memory','256m','--pid','host','-v','/:/host:ro,rslave'],'/monitoring/node_exporter')
  start('monitoring-dcu','monitoring-node:20260913.1',['/monitoring/exporter.py'],['--cpus','0.5','--memory','256m','--device','/dev/kfd','--device','/dev/mkfd','--device','/dev/dri','--group-add','video','-v','/opt/hyhal:/opt/hyhal:ro','-e','BIND='+args.bind,'-e','ALLOWED_CLIENTS=127.0.0.1,122.247.53.180,122.247.53.162,'+args.bind],'python3')
if __name__=='__main__':main()
