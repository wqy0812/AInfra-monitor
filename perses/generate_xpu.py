"""Derive XPU presentation from an explicit live DCU snapshot, never publish it back to DCU."""
import json,copy,argparse
from pathlib import Path
EMPTY='vector(0) unless on() vector(0)'
def generate(snapshot,output):
 baseline=json.loads(Path(snapshot).read_text())['dcu-monitoring']
 root=Path(output)/'xpu-monitoring';(root/'dashboards').mkdir(parents=True,exist_ok=True)
 def convert(obj):
  s=json.dumps(obj,ensure_ascii=False)
  for a,b in [('dcu-monitoring','xpu-monitoring'),('dcu-pd','xpu-pd'),('Prefill / dcu1','Prefill / xpu-2'),('Decode / dcu2','Decode / xpu-1'),('dcu1','xpu-2'),('dcu2','xpu-1'),('DCU','XPU')]:s=s.replace(a,b)
  out=json.loads(s);out['metadata']={k:v for k,v in out['metadata'].items() if k in ('name','project')};return out
 def write(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
 write(root/'project.json',convert(baseline['project']))
 write(root/'datasource.json',convert(baseline['datasources'][0]))
 for source in baseline['dashboards']:
  from remove_idle_thresholds import remove_panels
  d=remove_panels(convert(source));name=d['metadata']['name']
  if name=='hosts-dcu':d['metadata']['name']='hosts-xpu'
  desc='XPU 独立环境；请求为原生全量口径，P/D 不相加为唯一请求量。缺失、过期、重置、无分母留空。HiCache、缓存层级暂未接入；网关画像单独采集。'
  d['spec']['display']['description']=desc
  for key,p in d['spec']['panels'].items():
   blank=name=='cache-store'
   if blank:
    reason='硬件监控暂未接入' if name=='hosts-dcu' else '缓存与 HiCache 监控暂未接入，未推断服务是否启用 HiCache' if name=='cache-store' else '网关画像暂未采集'
    p['spec']['display']['description']=reason+'。本面板有意留空，No data 不表示零。'
    for q in p['spec'].get('queries',[]):q['spec']['plugin']['spec']['query']=EMPTY
   elif name=='monitoring-health':
    # Central VM/vmagent have a historical dcu-pd label, but are shared services.
    p['spec']['queries']=copy.deepcopy(source['spec']['panels'][key]['spec']['queries'])
    p['spec']['display']['description']='共享中央监控服务状态，不表示 XPU 模型或硬件指标。'+p['spec']['display'].get('description','')
  # Restore host metrics from the baseline; configure XPU exporter card metrics.
  if name=='hosts-dcu':
   from xpu_hosts import configure as configure_hosts
   d=configure_hosts(source)
  if name=='cache-store':
   from xpu_cache import configure
   d=configure(d)
  from metric_scope import request_description
  for panel in d['spec']['panels'].values():
   display=panel['spec']['display']
   display['description']=request_description(display.get('description',''))
  write(root/'dashboards'/(d['metadata']['name']+'.json'),d)
 return root
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('snapshot');p.add_argument('--output',default=str(Path(__file__).parent/'projects'));a=p.parse_args();print(generate(a.snapshot,a.output))
