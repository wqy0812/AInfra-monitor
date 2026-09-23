import copy,json,re,unittest
from pathlib import Path
from dcu_bottlenecks import expand_scrape,configure,presentation,ROOT

class DCUBottlenecksTest(unittest.TestCase):
 def test_scrape_scope(self):
  old="- job_name: sglang-prefill\n  metric_relabel_configs:\n  - source_labels: [environment, __name__]\n    regex: '(.*;sglang:token_usage|xpu-pd;sglang:num_used_tokens)'\n    action: keep\n- job_name: sglang-decode\n  metric_relabel_configs:\n  - source_labels: [__name__]\n    regex: 'sglang:token_usage'\n    action: keep\n- job_name: unrelated\n  static_configs: []\n"
  new=expand_scrape(old);self.assertEqual(expand_scrape(new),new)
  blocks=re.split(r'(?=^- job_name: )',new,flags=re.M)
  for b in blocks:
   if not b.startswith('- job_name: sglang-'):continue
   rx=re.search("regex: '([^\\n]+)'",b)[1]
   self.assertIsNotNone(re.fullmatch(rx,'dcu-pd;sglang:queue_time_seconds_bucket'))
   self.assertIsNone(re.fullmatch(rx,'xpu-pd;sglang:queue_time_seconds_bucket'))
   self.assertIsNone(re.fullmatch(rx,'a3-vllm;sglang:queue_time_seconds_bucket'))
   self.assertIsNotNone(re.fullmatch(rx,'xpu-pd;sglang:token_usage'))
  self.assertTrue(new.endswith('- job_name: unrelated\n  static_configs: []\n'))
 def test_query_size_and_representatives(self):
  inv=json.loads((ROOT/'dcu_bottlenecks_inventory.json').read_text())
  inv['prefill']['representatives']=[{'dp_rank':'0','pp_rank':'0','tp_rank':'0','moe_ep_rank':'0'}, {'dp_rank':'1','pp_rank':'0','tp_rank':'4','moe_ep_rank':'4'}]
  d={'metadata':{'project':'dcu-monitoring','name':'backend-diagnostics'},'spec':{'panels':{},'layouts':[{'spec':{'items':[]}}]}}
  got=configure(d,inv);self.assertEqual(d['spec']['panels'],{})
  self.assertEqual(configure(got,inv),got)
  qs=got['spec']['panels']['bn-prefill-throughput']['spec']['queries']
  self.assertEqual(len(qs),4)
  for item in qs:
   q=item['spec']['plugin']['spec']['query'];self.assertTrue('dp_rank="0"' in q or 'dp_rank="1"' in q)
   self.assertNotIn('sum(',q)
  for p in got['spec']['panels'].values():
   for q in p['spec']['queries']:self.assertLess(len(q['spec']['plugin']['spec']['query'].encode()),16000)
 def test_time_scaling_is_idempotent(self):
  d=json.loads((ROOT/'projects/a3-monitoring/dashboards/backend-performance.json').read_text())
  self.assertEqual(presentation(presentation(d)),presentation(d))
  p=d['spec']['panels']['core-ttft'];p['spec']['plugin']['spec']['yAxis']['label']='ms';p['spec']['queries'][0]['spec']['plugin']['spec']['query']='vector(1500)'
  q=presentation(d)['spec']['panels']['core-ttft']['spec']['queries'][0]['spec']['plugin']['spec']['query']
  self.assertEqual(q,'(vector(1500)) * 0.001')
if __name__=='__main__':unittest.main()
