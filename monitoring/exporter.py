"""Node-local DCU sampler and restricted HTTP access to node_exporter."""
import csv,io,json,math,os,subprocess,threading,time,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
FIELDS={'utilization_percent':('HCU use (%)',1),'memory_used_bytes':('vram Total Used Memory (MiB)',1048576),'memory_total_bytes':('vram Total Memory (MiB)',1048576),'temperature_celsius':('Temperature (Sensor junction) (C)',1),'power_watts':('Average Graphics Package Power (W)',1)}
ALLOWED=set(os.environ.get('ALLOWED_CLIENTS','127.0.0.1').split(','))
STATE={'ts':0,'rows':[],'ok':False};LOCK=threading.Lock()
def parse_csv(text):
 lines=text.splitlines();start=next(i for i,s in enumerate(lines) if s.startswith('device,'))
 rows=list(csv.DictReader(io.StringIO('\n'.join(lines[start:]))))
 if not rows or len({r['device'] for r in rows})!=len(rows):raise ValueError('Missing or duplicate devices')
 return rows

def render(state,now):
 good=state['ok'] and 0<=now-state['ts']<15
 lines=['dcu_sample_success '+str(int(good)),'dcu_sample_timestamp_seconds '+str(state['ts'])]
 if good:
  for row in state['rows']:
   for name,(key,scale) in FIELDS.items():
    try:value=float(row[key])*scale
    except (ValueError,KeyError,TypeError):continue
    if math.isfinite(value) and value>=0:lines.append('dcu_'+name+'{device='+json.dumps(row['device'])+'} '+str(value))
 return '\n'.join(lines)+'\n'

def sample():
 while True:
  start=time.monotonic()
  try:
   r=subprocess.run(['/opt/dtk/bin/rocm-smi','--showuse','--showmemuse','--showmeminfo','vram','--showtemp','--showpower','--csv'],capture_output=True,text=True,timeout=3,check=True)
   value={'ts':time.time(),'rows':parse_csv(r.stdout),'ok':True}
  except Exception:value={'ts':time.time(),'rows':[],'ok':False}
  with LOCK:STATE.update(value)
  time.sleep(max(0,5-(time.monotonic()-start)))
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  if self.client_address[0] not in ALLOWED:self.send_error(403);return
  if self.path=='/metrics':
   with LOCK:body=render(STATE,time.time()).encode()
  elif self.path=='/node-metrics':
   try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:19100/metrics',timeout=3) as r:body=r.read()
   except Exception:self.send_error(503);return
  else:self.send_error(404);return
  self.send_response(200);self.send_header('Content-Type','text/plain; version=0.0.4');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
if __name__=='__main__':
 from compat import apply
 apply()
 threading.Thread(target=sample,daemon=True).start()
 ThreadingHTTPServer((os.environ.get('BIND','127.0.0.1'),19500),Handler).serve_forever()
