"""Verify the exact deployed binary on loopback fixtures; never restart production."""
import hashlib,json,os,socket,subprocess,tempfile,threading,time,urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path('/data2/monitoring/evidence/profile-environments-20260914');binary=ROOT/'aigate'
assert hashlib.sha256(binary.read_bytes()).hexdigest()=='bf273b8698c0f71305fa92ce816d286d85ca31cd7c08a402648e3315359479d4'
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,data):
  b=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_GET(self):self.reply({'object':'list','data':[{'id':'fixture','object':'model'}]})
 def do_POST(self):
  self.rfile.read(int(self.headers['Content-Length']));self.reply({'id':'fixture','object':'chat.completion','model':'fixture','choices':[{'index':0,'message':{'role':'assistant','content':'fixture answer'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}})
def port():
 s=socket.socket();s.bind(('127.0.0.1',0));p=s.getsockname()[1];s.close();return p
def fetch(p,path,body=None,auth=False):
 r=urllib.request.Request('http://127.0.0.1:'+str(p)+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**({'Authorization':'Bearer isolated-fixture'} if auth else {})})
 with urllib.request.urlopen(r,timeout=5) as response:return response.read()
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();process=None
try:
 with tempfile.TemporaryDirectory(prefix='a3-profile-restart-') as tmp:
  root=Path(tmp);api_port,profile_port=port(),port();assert api_port!=profile_port
  config={'backends':{'a3':{'adapter':'openai','url':'http://127.0.0.1:'+str(server.server_port)+'/v1/chat/completions','models_url':'http://127.0.0.1:'+str(server.server_port)+'/v1/models'}},'profile':{'enabled':True,'listen':'127.0.0.1:'+str(profile_port),'directory':str(root/'events'),'max_disk_bytes':16777216}}
  (root/'config.json').write_text(json.dumps(config));env=dict(os.environ,AIGATE_PROFILE_KEY='isolated-fixture')
  def start():
   global process
   process=subprocess.Popen([str(binary),'serve','--config',str(root/'config.json'),'--listen','127.0.0.1:'+str(api_port)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   for _ in range(100):
    try:fetch(api_port,'/health');fetch(profile_port,'/metrics',auth=True);return
    except Exception:assert process.poll() is None;time.sleep(.1)
   raise RuntimeError('fixture startup failed')
  def counters():
   values={}
   for line in fetch(profile_port,'/metrics',auth=True).decode().splitlines():
    if line.startswith('aigate_'):
     k,v=line.rsplit(' ',1);values[k]=float(v)
   return values
  def total(v,n):return sum(x for k,x in v.items() if k==n or k.startswith(n+'{'))
  start();window_start=time.time()
  for _ in range(2):assert json.loads(fetch(api_port,'/v1/chat/completions',{'messages':[{'role':'user','content':'synthetic fixture'}]}))['choices']
  before=counters();hmac=hashlib.sha256((root/'events/hmac.key').read_bytes()).hexdigest();process.terminate();process.wait(timeout=10);assert process.returncode==0
  assert (root/'events/counters.json').exists();start()
  assert hashlib.sha256((root/'events/hmac.key').read_bytes()).hexdigest()==hmac
  restored=counters();assert total(restored,'aigate_requests_started_total')==2 and total(restored,'aigate_prompt_tokens_total')==20
  assert total(restored,'aigate_profile_counter_start_time_seconds')==total(before,'aigate_profile_counter_start_time_seconds')
  fetch(api_port,'/v1/chat/completions',{'messages':[{'role':'user','content':'synthetic fixture'}]});time.sleep(2)
  after=counters();assert total(after,'aigate_requests_started_total')==3 and total(after,'aigate_prompt_tokens_total')==30
  status=json.loads(fetch(profile_port,'/profile/v1/status',auth=True))
  j=json.loads(fetch(profile_port,'/profile/v1/analyses',{'start':max(window_start,status['retained_start']),'end':time.time()},True))
  for _ in range(30):
   j=json.loads(fetch(profile_port,'/profile/v1/analyses/'+j['id'],auth=True))
   if j['state'] in ('completed','failed'):break
   time.sleep(.1)
  assert j['state']=='completed' and j['result']['requests']==3,j
  result={'passed':True,'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'isolated_loopback':True,'restored_requests':2,'final_requests':3,'final_prompt_tokens':30,'detail_requests':3,'hmac_key_preserved':True,'counter_lifecycle_preserved':True,'production_restarted':False}
  (ROOT/'restart-acceptance.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
finally:
 if process and process.poll() is None:process.terminate();process.wait(timeout=10)
 server.shutdown();server.server_close()
