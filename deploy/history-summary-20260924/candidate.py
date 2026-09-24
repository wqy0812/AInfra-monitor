import asyncio,copy,json,time
from monitoring.api import Service,summary_point
async def main():
 end=int((time.time()-20)//5)*5;results={}
 for env in ('dcu-pd','a3-vllm','xpu-pd'):
  service=Service(env)
  try:
   for hours in (1,3):
    start=end-hours*3600
    begin=time.monotonic();full=await service.history(hours,start,end);full_time=time.monotonic()-begin
    begin=time.monotonic();summary=await service.history(hours,start,end,view='summary');summary_time=time.monotonic()-begin
    assert summary['points']
    assert summary=={**full,'points':[summary_point(copy.deepcopy(p)) for p in full['points']]},(env,hours,'aggregate mismatch')
    assert 'resources' not in json.dumps(summary)
    results[env+'/'+str(hours)]={'full_bytes':len(json.dumps(full,separators=(',',':')).encode()),'summary_bytes':len(json.dumps(summary,separators=(',',':')).encode()),'full_seconds':round(full_time,3),'summary_seconds':round(summary_time,3),'points':len(summary['points'])}
  finally:await service.client.aclose()
 print(json.dumps({'passed':True,'results':results}))
asyncio.run(main())
