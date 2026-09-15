"""Real vmagent outage/replay check in disposable localhost-only instances."""
import json,pathlib,subprocess,threading,time,urllib.request,urllib.parse,tempfile
from http.server import BaseHTTPRequestHandler,HTTPServer
BIN=pathlib.Path('/data2/monitoring/release/vendor/bin');ROOT=pathlib.Path('/data2/monitoring/evidence')
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  body=('fixture_counter '+str(time.time())+'\n').encode();self.send_response(200);self.end_headers();self.wfile.write(body)
def get(path):return json.load(urllib.request.urlopen('http://127.0.0.1:18528'+path,timeout=3))
def main():
 processes=[];server=HTTPServer(('127.0.0.1',18531),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
 with tempfile.TemporaryDirectory(prefix='monitor-fault-') as td:
  p=pathlib.Path(td);(p/'scrape.yml').write_text('global:\n  scrape_interval: 1s\n  scrape_timeout: 800ms\nscrape_configs:\n- job_name: fixture\n  static_configs:\n  - targets: ["127.0.0.1:18531"]\n')
  log=(ROOT/'fault-process.log').open('w')
  def vm():
   v=subprocess.Popen([str(BIN/'victoria-metrics-prod'),'-httpListenAddr=127.0.0.1:18528','-storageDataPath='+str(p/'vm'),'-memory.allowedBytes=64MiB','-retentionPeriod=1d'],stdout=log,stderr=log);processes.append(v);return v
  try:
   v=vm();a=subprocess.Popen([str(BIN/'vmagent-prod'),'-httpListenAddr=127.0.0.1:18529','-promscrape.config='+str(p/'scrape.yml'),'-remoteWrite.url=http://127.0.0.1:18528/api/v1/write','-remoteWrite.tmpDataPath='+str(p/'buffer'),'-remoteWrite.maxDiskUsagePerURL=64MiB','-memory.allowedBytes=64MiB'],stdout=log,stderr=log);processes.append(a)
   time.sleep(5);outage_start=time.time();v.terminate();v.wait(timeout=10);time.sleep(7);outage_end=time.time();v=vm()
   deadline=time.time()+30;times=[]
   while time.time()<deadline:
    try:
     url='http://127.0.0.1:18528/api/v1/export?'+urllib.parse.urlencode({'match[]':'fixture_counter','start':outage_start,'end':outage_end})
     raw=urllib.request.urlopen(url,timeout=3).read().decode();times=[t for line in raw.splitlines() for t in json.loads(line)['timestamps']]
     if len(times)>=5:break
    except Exception:pass
    time.sleep(1)
   assert len(times)>=5,'Buffered outage samples not recovered'
   result={'passed':True,'outage_seconds':outage_end-outage_start,'recovered_outage_samples':len(times),'isolated_ports':[18528,18529,18531],'production_stopped':False}
   (ROOT/'fault-validation.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
  finally:
   for process in reversed(processes):
    if process.poll() is None:process.terminate()
   for process in processes:
    try:process.wait(timeout=8)
    except subprocess.TimeoutExpired:process.kill();process.wait()
   server.shutdown();log.close()
if __name__=='__main__':main()
