"""Use the existing NTP client with the verified internal sources. Execute via SSH MCP."""
import json,re,subprocess,time,socket,shutil,os
from pathlib import Path
ROOT=Path(os.environ.get('PROFILE_TIME_BACKUP_DIR','/root/profile-time-sync-20260914'));ROOT.mkdir(mode=0o700,exist_ok=True)
def run(*args):return subprocess.check_output(args,stderr=subprocess.STDOUT,text=True,timeout=20)
clients=[x for x in ('ntpd','chronyd') if subprocess.run(['systemctl','is-active','--quiet',x]).returncode==0]
assert len(clients)==1,clients
client=clients[0];config=Path('/etc/ntp.conf' if client=='ntpd' else '/etc/chrony.conf')
probe=run('ntpdate','-q','-t','2','-p','1','122.16.46.15','122.16.46.16');offsets=[float(v) for v in re.findall(r'offset ([+-][0-9.]+), delay',probe)]
assert len(offsets)==2 and abs(offsets[0]-offsets[1])<.1,probe
assert min(offsets)>0,'This bounded correction expects the observed slow clock'
backup=ROOT/config.name;assert not backup.exists(),'Time sync already configured; inspect saved evidence'
shutil.copy2(config,backup)
before={'host':socket.gethostname(),'time':time.time(),'client':client,'offsets':offsets,'probe':probe}
(ROOT/'before.json').write_text(json.dumps(before,indent=2))
text=config.read_text();text='\n'.join(line for line in text.splitlines() if not re.match(r'^\s*(server|pool|peer)\s+',line))+'\nserver 122.16.46.15 iburst prefer\nserver 122.16.46.16 iburst\n'
config.write_text(text)
run('systemctl','stop',client)
try:result=run('ntpdate','-u','-t','2','-p','2','122.16.46.15','122.16.46.16')
finally:run('systemctl','start',client)
run('systemctl','enable',client)
after={'host':socket.gethostname(),'time':time.time(),'client':client,'step':result,'service':run('systemctl','is-active',client).strip()}
(ROOT/'after.json').write_text(json.dumps(after,indent=2));print(json.dumps(after))
