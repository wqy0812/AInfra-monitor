"""Start migrated central services on test4; execute via SSH MCP."""
from start import ROOT,RELEASE,start

for directory in ('vm','buffer','state','evidence'):
 (ROOT/directory).mkdir(parents=True,exist_ok=True)
start('monitoring-vm','monitoring-vm:1.151.0',[
 '-storageDataPath=/storage','-retentionPeriod=30d','-httpListenAddr=127.0.0.1:18428',
 '-memory.allowedBytes=1536MiB','-storage.minFreeDiskSpaceBytes=20GiB'],
 ['--cpus','2','--memory','2g','-v',str(ROOT/'vm')+':/storage'],'/vm')
start('monitoring-vmagent','monitoring-vm:1.151.0',[
 '-promscrape.config=/config/scrape.yml','-remoteWrite.url=http://127.0.0.1:18428/api/v1/write',
 '-remoteWrite.tmpDataPath=/buffer','-remoteWrite.maxDiskUsagePerURL=5GiB',
 '-httpListenAddr=127.0.0.1:18429','-memory.allowedBytes=384MiB'],
 ['--cpus','0.5','--memory','512m','-v',str(ROOT/'buffer')+':/buffer',
 '-v',str(RELEASE/'deploy')+':/config:ro'],'/vmagent')
start('monitoring-api','monitoring-api:xpu-20260921',[
 '-m','uvicorn','monitoring.api:app','--host','0.0.0.0','--port','18430','--no-access-log','--no-proxy-headers'],
 ['--cpus','1','--memory','512m','-v',str(ROOT/'state')+':/state',
 '-e','ALLOWED_CLIENTS=*'],'python')
