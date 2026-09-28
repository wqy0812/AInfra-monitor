"""Run locally on the deployment host. Never connects over SSH."""
import argparse,json,pathlib,subprocess
ROOT=pathlib.Path('/data2/monitoring');RELEASE=ROOT/'release'
def run(*args):return subprocess.check_output(args,stderr=subprocess.STDOUT).decode()
def exists(name):return subprocess.run(['docker','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
def start(name,image,args,options=(),entrypoint=None):
 if exists(name):raise RuntimeError('Container already exists; use explicit upgrade: '+name)
 cmd=['docker','run','-d','--name',name,'--restart','unless-stopped','--network','host','--label','monitoring.owner=independent','--log-opt','max-size=10m','--log-opt','max-file=3',*options]
 if entrypoint:cmd+=['--entrypoint',entrypoint]
 print(run(*cmd,image,*args))
def main():
 p=argparse.ArgumentParser();p.add_argument('mode',choices=['central','node']);p.add_argument('--bind');args=p.parse_args()
 if args.mode=='central':
  for d in ['vm','buffer','state','evidence','backups']:(ROOT/d).mkdir(parents=True,exist_ok=True)
  # Existing Python runtime is pinned by immutable local image ID; no new dependencies or model processes.
  base=run('docker','inspect','code-eval-platform:0.7.4-executor-fix.1','--format','{{.Id}}').strip()
  binary='FROM scratch\nCOPY vendor/bin/victoria-metrics-prod /vm\nCOPY vendor/bin/vmagent-prod /vmagent\n'
  (RELEASE/'Dockerfile.vm').write_text(binary)
  print(run('docker','build','-f',str(RELEASE/'Dockerfile.vm'),'-t','monitoring-vm:1.151.0',str(RELEASE)))
  (RELEASE/'Dockerfile.api').write_text('FROM '+base+'\nWORKDIR /monitoring\nCOPY monitoring /monitoring/monitoring\nHEALTHCHECK NONE\n')
  print(run('docker','build','-f',str(RELEASE/'Dockerfile.api'),'-t','monitoring-api:20260913.1',str(RELEASE)))
  start('monitoring-vm','monitoring-vm:1.151.0',['-storageDataPath=/storage','-retentionPeriod=30d','-httpListenAddr=127.0.0.1:18428','-memory.allowedBytes=1536MiB','-storage.minFreeDiskSpaceBytes=20GiB'],['--cpus','2','--memory','2g','-v',str(ROOT/'vm')+':/storage'],'/vm')
  start('monitoring-vmagent','monitoring-vm:1.151.0',['-promscrape.config=/config/scrape.yml','-remoteWrite.url=http://127.0.0.1:18428/api/v1/write','-remoteWrite.tmpDataPath=/buffer','-remoteWrite.maxDiskUsagePerURL=5GiB','-httpListenAddr=127.0.0.1:18429','-memory.allowedBytes=384MiB'],['--cpus','0.5','--memory','512m','-v',str(ROOT/'buffer')+':/buffer','-v',str(RELEASE/'deploy')+':/config:ro'],'/vmagent')
  start('monitoring-api','monitoring-api:20260913.1',['-m','uvicorn','monitoring.api:app','--host','0.0.0.0','--port','18430','--no-access-log','--no-proxy-headers'],['--cpus','1','--memory','512m','-v',str(ROOT/'state')+':/state','-e','ALLOWED_CLIENTS=*'],'python')
 else:
  assert args.bind
  image=run('docker','inspect','kongmx-deepseek-v4-0828','--format','{{.Image}}').strip()
  (RELEASE/'Dockerfile.node').write_text('FROM '+image+'\nWORKDIR /monitoring\nCOPY monitoring/exporter.py /monitoring/exporter.py\nCOPY monitoring/compat.py /monitoring/compat.py\nCOPY vendor/bin/node_exporter /monitoring/node_exporter\nHEALTHCHECK NONE\n')
  print(run('docker','build','-f',str(RELEASE/'Dockerfile.node'),'-t','monitoring-node:20260913.1',str(RELEASE)))
  start('monitoring-node','monitoring-node:20260913.1',['--web.listen-address=127.0.0.1:19100','--path.rootfs=/host','--collector.disable-defaults','--collector.cpu','--collector.meminfo','--collector.filesystem','--collector.netdev','--collector.diskstats','--collector.stat','--collector.time','--collector.loadavg'],['--cpus','0.5','--memory','256m','--pid','host','-v','/:/host:ro,rslave'],'/monitoring/node_exporter')
  start('monitoring-dcu','monitoring-node:20260913.1',['/monitoring/exporter.py'],['--cpus','0.5','--memory','256m','--device','/dev/kfd','--device','/dev/mkfd','--device','/dev/dri','--group-add','video','-v','/opt/hyhal:/opt/hyhal:ro','-e','BIND='+args.bind,'-e','ALLOWED_CLIENTS=127.0.0.1,122.247.53.180,122.247.53.162,'+args.bind],'python3')
if __name__=='__main__':main()
