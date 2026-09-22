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
   for k in ('cpu','hicache','cache_60s'):self.assertIsNone(node[k])
   self.assertNotIn('hicache_tokens',node['resources'])
  self.assertEqual(snaps[-1]['nodes']['decode']['telemetry']['status'],'not_integrated')
 def test_missing_and_reset(self):
  groups=self.fixtures()
  for row in groups['sglang-decode'][1][105]:
   if row['name']=='sglang:realtime_tokens_total':row['value']=0
  self.assertIsNone(replay(groups,100,105)[1][-1]['nodes']['decode']['decode_tokens'])
  self.assertIsNone(replay(groups,125,130)[1][-1]['nodes']['decode']['requests'])
 def test_api_environment_and_no_false_valid(self):
  from monitoring.api import encode,history_expression,validate_environment
  from monitoring.request_profile import selector
  from monitoring.gateway_live import expressions
  self.assertEqual(validate_environment('xpu-pd'),'xpu-pd')
  self.assertIn('environment="xpu-pd"',selector('requests_total',environment='xpu-pd'))
  self.assertTrue(expressions('xpu-pd',15))
  point=replay(self.fixtures(),100,105)[1][-1]
  lines=encode([point],'xpu-pd').splitlines()
  for line in lines:
   if line.startswith('monitoring_chart_valid') and any(x in line for x in ('.cpu"','.hicache.','.cache_60s.','path="mooncake.')):
    self.assertEqual(line.split('} ')[1].split()[0],'0')
  self.assertNotIn('dcu-pd',history_expression('xpu-pd','value',15))
if __name__=='__main__':unittest.main()
