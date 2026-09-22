"""Enable only the three XPU gateway dashboards after authorized collection."""
from xpu_perses_release import *

def main():
 names=('gateway','gateway-generation','gateway-requests')
 before={name:http(BASE+'/api/v1/projects/xpu-monitoring/dashboards/'+name,auth=True) for name in names}
 save('gateway-dashboards-before.json',before)
 for name in names:
  old=before[name];new=json.loads((ROOT/'xpu-monitoring'/'dashboards'/(name+'.json')).read_text())
  assert all(q['spec']['plugin']['spec']['query']==EMPTY for p in old['spec']['panels'].values() for q in p['spec'].get('queries',[]))
  path=BASE+'/api/v1/projects/xpu-monitoring/dashboards/'+name
  assert http(path,auth=True)['spec']==old['spec'],'Concurrent edit'
  new['metadata']=old['metadata']
  http(path,'PUT',new,auth=True)
  got=http(path,auth=True);assert got['spec']['panels']==new['spec']['panels']
 # Update the other dashboard descriptions to reflect gateway availability.
 for name in ('overview','backend-diagnostics','cache-store','hosts-xpu','monitoring-health'):
  path=BASE+'/api/v1/projects/xpu-monitoring/dashboards/'+name
  old=http(path,auth=True);new=copy.deepcopy(old)
  new['spec']['display']['description']=new['spec']['display']['description'].replace('网关画像暂未采集','网关画像单独采集')
  http(path,'PUT',new,auth=True)
 save('gateway-published.json',{'dashboards':names,'at':time.time()})
 print('XPU gateway dashboards enabled')
if __name__=='__main__':main()
