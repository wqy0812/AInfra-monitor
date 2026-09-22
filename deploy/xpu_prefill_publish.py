"""Publish the prepared XPU cache dashboard using existing Perses administrator auth."""
from xpu_release import ROOT,BASE,http,save
import json,copy

def main():
 path=BASE+'/api/v1/projects/xpu-monitoring/dashboards/cache-store'
 old=http(path,auth=True)
 new=json.loads((ROOT/'cache-store-prefill-candidate.json').read_text())
 save('cache-store-before-prefill.json',old)
 new['metadata']=old['metadata']
 assert http(path,auth=True)['spec']==old['spec'],'Concurrent dashboard edit'
 http(path,'PUT',new,auth=True)
 got=http(path,auth=True)
 assert got['spec']['panels']==new['spec']['panels']
 assert got['spec']['layouts']==new['spec']['layouts']
 proxy=BASE+'/api/v1/projects/xpu-monitoring/datasources'
 sources=http(proxy,auth=True)
 assert len(sources)==1
 proxy=BASE+'/proxy/projects/xpu-monitoring/datasources/'+sources[0]['metadata']['name']
 import urllib.parse
 results=[]
 for key,panel in new['spec']['panels'].items():
  q=panel['spec']['queries'][0]['spec']['plugin']['spec']['query']
  params=urllib.parse.urlencode({'query':q,'nocache':1})
  result=http(proxy+'/api/v1/query?'+params,auth=True)
  assert result['status']=='success'
  results.append({'panel':key,'series':len(result['data']['result'])})
 save('prefill-cache-published.json',results)
 print('Published XPU Prefill dashboard; all 10 panel queries passed through Perses')
if __name__=='__main__':main()
