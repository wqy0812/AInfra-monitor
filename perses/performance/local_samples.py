"""Synthetic chart data for localhost browser checks. Never writes production."""
import json,math,time,urllib.request
URL='http://127.0.0.1:18543'
paths=['requests','decode_tokens','running','waiting','cpu','memory','cache_60s.ratio','cache_60s.external_ratio']+['percentiles.'+m+'.'+q for m in ['ttft','itl','e2e'] for q in ['p50','p95','p99']]
def push(stamps):
 lines=[]
 for t in stamps:
  for env in ['a3-vllm','dcu-pd']:
   lines.append(f'up{{environment="{env}",job="fixture",instance="synthetic"}} 1 {t*1000}')
   for role in ['decode','prefill']:
    for path in paths:
     for schema in ['request-metrics-v2','v1']:
      labels=json.dumps({'environment':env,'schema':schema,'path':'nodes.'+role+'.'+path})
      tags=','.join(k+'='+json.dumps(v) for k,v in json.loads(labels).items());v=5+math.sin(t/50)
      lines.append(f'monitoring_chart_value{{{tags}}} {v} {t*1000}');lines.append(f'monitoring_chart_valid{{{tags}}} 1 {t*1000}')
 body=('\n'.join(lines)+'\n').encode();urllib.request.urlopen(urllib.request.Request(URL+'/api/v1/import/prometheus',data=body),timeout=20).close();urllib.request.urlopen(URL+'/internal/force_flush',timeout=20).close()
now=int(time.time()//5)*5;push(range(now-3600,now+1,5));print('synthetic chart samples ready',flush=True)
while True:
 time.sleep(5);now=int(time.time()//5)*5;push([now])
