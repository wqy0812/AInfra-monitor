import copy,json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quantile_release import merge_panel,prepare

class ReleaseTest(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.before={'projects':[],'datasources':[],'dashboards':[]}
  targets=[('a3-monitoring','gateway-requests',['first','duration','input','output']),('dcu-monitoring','gateway-requests',['first','duration','input','output']),('a3-monitoring','backend-diagnostics',['request_'+part+'_seconds' for part in ['queue_time','prefill_time','decode_time','inference_time','time_per_output_token']])]
  for project,name,keys in targets:
   panels={}
   for key in keys:
    queries=[]
    for phi,label in [('0.5','P50'),('0.95','P95'),('0.99','P99')]:
     queries.append({'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{'query':f'histogram_quantile({phi}, buckets) unless on(environment) invalid and on(environment) active','seriesNameFormat':'{{instance}} '+label,'minStep':'5s'}}}})
    panels['extra-'+key]={'kind':'Panel','spec':{'queries':queries,'display':{'name':key},'plugin':{'kind':'TimeSeriesChart','spec':{}}}}
   cls.before['dashboards'].append({'kind':'Dashboard','metadata':{'name':name,'project':project},'spec':{'panels':panels,'duration':'1h'}})

 def test_changes_are_exactly_thirteen_panels(self):
  after,changes=prepare(self.before);self.assertEqual(len(changes),13)
  restored=copy.deepcopy(after)
  for x in changes:
   d=next(d for d in restored['dashboards'] if d['metadata']['project']==x['project'] and d['metadata']['name']==x['dashboard']);d['spec']['panels'][x['panel']]=x['before']
  self.assertEqual(restored,self.before)
 def test_different_guard_is_rejected(self):
  _,changes=prepare(self.before);panel=copy.deepcopy(changes[0]['before']);panel['spec']['queries'][1]['spec']['plugin']['spec']['query']+=' and vector(1)'
  with self.assertRaises(AssertionError):merge_panel(panel)
 def test_custom_query_style_is_not_silently_dropped(self):
  _,changes=prepare(self.before);panel=copy.deepcopy(changes[0]['before']);panel['spec']['plugin']['spec']['querySettings']=[{'queryIndex':2,'color':'red'}]
  with self.assertRaises(AssertionError):merge_panel(panel)
 def test_merged_resources_are_idempotent(self):
  after,_=prepare(self.before);again,changes=prepare(after);self.assertEqual(again,after);self.assertEqual(changes,[])
if __name__=='__main__':unittest.main()
