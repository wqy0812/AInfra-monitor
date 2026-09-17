"""Pure compatibility calculations copied from code-eval; no database or transport."""
import json,math,re
from .request_scope import request_counters as source_counters, SCHEMA
from .latency import LatencyWindow, quantile_buckets
from .monitor_series import decode_counters,counter_rate,CacheSeriesWindow,MAX_GAP
from .cache_metrics import effective_counters,host_capacity
PROM=re.compile(r'^([^\s{]+)(\{.*\})?\s+(\S+)')
LABEL=re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')
def parse_prom(text):
 out=[]
 for line in text.splitlines():
  if line.startswith('#'):continue
  m=PROM.match(line)
  if not m:continue
  try:
   value=float(m[3]);labels={k:json.loads('"'+v+'"') for k,v in LABEL.findall(m[2] or '')}
   if math.isfinite(value):out.append({'name':m[1],'labels':labels,'value':value})
  except (ValueError,TypeError):continue
 return out
def select(rows,name):return [x for x in rows if x['name']==name]
def unique_value(rows,name):
 xs=select(rows,name)
 # Global tokenizer series only. Ambiguous multi-series counters must not be blindly summed.
 return xs[0]['value'] if len(xs)==1 else None
def request_counters(rows):
 return source_counters(rows,'sglang:num_requests_total')
def delta(a,b):return a-b if a is not None and b is not None and a>=b else None

class Calculator:
 def __init__(self):
  self.latency_windows={r:LatencyWindow() for r in ('prefill','decode')}
  self.cache_windows={r:CacheSeriesWindow() for r in ('prefill','decode')}
  self.previous={}
 def metrics(self,role,rows,ts):
  p={'ts':ts,'request_scope':'all','request_schema':SCHEMA,'metric_scopes':{'ttft':'native_mixed','itl':'native_mixed','e2e':'all'},'itl_semantics':'按输出批次平均的 Token 间隔，未区分流式'}
  for short,name in [('input_tokens','prompt_tokens_total'),('output_tokens','generation_tokens_total')]:
   counters=source_counters(rows,'sglang:'+name)
   p[short+'_counters']=counters
   p[short]=sum(counters.values()) if counters else None
  p['cache_input_tokens']=p['input_tokens']
  p['request_counters']=request_counters(rows)
  p['requests']=sum(p['request_counters'].values()) if p['request_counters'] else None
  p['cache_sources']={x['labels'].get('cache_source','unknown'):x['value'] for x in select(rows,'sglang:cached_tokens_total')}
  # Keep scheduler gauges per rank rather than inventing an aggregation across TP/CP.
  p['rank_gauges']=[x for x in rows if x['name'] in ['sglang:num_running_reqs','sglang:num_queue_reqs','sglang:token_usage','sglang:cache_hit_rate','sglang:hicache_host_used_tokens','sglang:hicache_host_total_tokens']]
  p['decode_counters']=decode_counters(rows) if role=='decode' else None
  old=self.previous.get(('metrics',role));p['rates']={'decode_tokens':None,'requests':None};p['cache_hit_ratio']=None;p['rate_interval_seconds']=None
  if old:
   dt=p['ts']-old['ts']
   p['rate_interval_seconds']=dt if 0<dt<MAX_GAP else None
   p['rates']['decode_tokens']=counter_rate(p['decode_counters'],old.get('decode_counters'),dt)
   p['rates']['requests']=counter_rate(p['request_counters'],old.get('request_counters'),dt)
   for k in ['input_tokens','output_tokens']:
    p['rates'][k]=counter_rate(p[k+'_counters'],old.get(k+'_counters'),dt)
   inp=delta(p['cache_input_tokens'],old['cache_input_tokens'])
   if inp and p['cache_sources']:
    changes=[delta(v,old['cache_sources'].get(k,0)) for k,v in p['cache_sources'].items()]
    if all(x is not None for x in changes) and sum(changes)<=inp:p['cache_hit_ratio']=sum(changes)/inp
  p.update(self.latency_windows[role].add(rows,ts))
  for kind,history in self.latency_windows[role].histories.items():
   current=history[-1][1] if history and history[-1][0]==ts else {}
   edges=next(iter(current.values()),{})
   p['latencies'][kind]={('+Inf' if math.isinf(k) else str(k)):sum(g[k] for g in current.values()) for k in edges}
  if role=='prefill':p.update(cache_schema='prefill-effective-v1',cache_effective=effective_counters(rows),hicache=host_capacity(rows))
  p['cache_60s']=self.cache_windows[role].add({**p,'input_tokens':p['cache_input_tokens']})
  if role=='prefill':p['cache_hit_ratio']=p['cache_60s']['ratio']
  self.previous[('metrics',role)]=p
  return p
