import asyncio, importlib.util, json, time, statistics
from monitoring.api import Service as Original
spec=importlib.util.spec_from_file_location('monitoring.live_candidate','/tmp/codeeval-live-api-20261002.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def normalized(obj):
 if isinstance(obj,dict):return {k:normalized(v) for k,v in obj.items()}
 if isinstance(obj,(list,tuple)):return sorted((normalized(v) for v in obj),key=lambda x:json.dumps(x,sort_keys=True))
 if isinstance(obj,float):return round(obj,9)
 return obj
async def main():
 end=int((time.time()-60)//5)*5;start=end-85
 old,new=Original(),m.Service();runs=[];output={}
 try:
  for round in range(3):
   for label,s in [('before',old),('after',new)]:
    t=time.monotonic();groups=await s.raw(start,end);raw=time.monotonic()-t
    t=time.monotonic();value=s.replay(groups,start,end);replay=time.monotonic()-t
    output[label]=normalized(value)
    runs.append({'version':label,'raw_seconds':raw,'replay_seconds':replay,'rows':sum(len(rows) for _,values in groups.values() for rows in values.values())})
   assert output['before']==output['after'],'Live snapshots or chart points differ'
  print(json.dumps({'equivalent':True,'float_decimal_places':9,'start':start,'end':end,'runs':runs,'median_seconds':{label:statistics.median(r['raw_seconds']+r['replay_seconds'] for r in runs if r['version']==label) for label in ['before','after']}},indent=2))
 finally:await old.close();await new.close()
asyncio.run(main())
