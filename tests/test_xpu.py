import copy
import unittest
from monitoring.xpu import replay
from monitoring.replay import decode_export

class XpuTest(unittest.TestCase):
 def fixtures(self):
  out=[]
  for role in ('prefill','decode'):
   def add(name,labels,values):
    out.append({'metric':{'__name__':name,'environment':'xpu-pd','job':'sglang-'+role,'instance':role,**labels},'timestamps':[100000,105000],'values':values})
   add('up',{},[1,1])
   for name in ('num_requests_total','prompt_tokens_total','generation_tokens_total'):
    add('sglang:'+name,{'model_name':'glm'},[100,110])
   for rank in range(2 if role=='decode' else 1):
    labels={'model_name':'glm','engine_type':'unified','dp_rank':str(rank),'tp_rank':str(rank),'pp_rank':'0'}
    add('sglang:num_running_reqs',labels,[1,1])
    add('sglang:realtime_tokens_total',{**labels,'mode':'decode'},[100,125])
   add('sglang:hicache_host_used_tokens',{'model_name':'glm'},[10,10])
   add('sglang:hicache_host_total_tokens',{'model_name':'glm'},[100,100])
  return decode_export(out)
 def test_adapter_and_blanks(self):
  groups=self.fixtures(); original=copy.deepcopy(groups)
  snaps,points=replay(groups,100,105)
  self.assertEqual(groups,original)
  self.assertEqual(points[-1]['nodes']['decode']['decode_tokens'],10)
  self.assertEqual(points[-1]['nodes']['decode']['requests'],2)
  for node in points[-1]['nodes'].values():
   for k in ('cpu','hicache'):self.assertIsNone(node[k])
   self.assertIsNone((node.get('cache_60s') or {}).get('ratio'))
   self.assertNotIn('hicache_tokens',node['resources'])
  self.assertEqual(snaps[-1]['nodes']['decode']['telemetry']['status'],'not_integrated')
 def test_missing_and_reset(self):
  groups=self.fixtures()
  for row in groups['sglang-decode'][1][105]:
   if row['name']=='sglang:realtime_tokens_total':row['value']=0
  self.assertIsNone(replay(groups,100,105)[1][-1]['nodes']['decode']['decode_tokens'])
  self.assertIsNone(replay(groups,125,130)[1][-1]['nodes']['decode']['requests'])
if __name__=='__main__':unittest.main()
