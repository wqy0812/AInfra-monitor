# Project-aware CLI routing; legacy helpers below remain importable.
if __name__ == "__main__":
    from project_split import main
    main()
    raise SystemExit(0)

"""Generate editable Perses v0.54 dashboards; no writes to the metric store."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
PROJECT='dcu-monitoring'

def variable(name,title,values):
 return {'kind':'ListVariable','spec':{'name':name,'display':{'name':title},'defaultValue':values[0]['value'],'plugin':{'kind':'StaticListVariable','spec':{'values':values}}}}
ROLE=variable('role','角色',[{'value':'.*','label':'全部'},{'value':'prefill','label':'Prefill / dcu1'},{'value':'decode','label':'Decode / dcu2'}])
NODE=variable('node','节点',[{'value':'.*','label':'全部'},{'value':'dcu1','label':'dcu1'},{'value':'dcu2','label':'dcu2'}])
DEVICE=variable('device','DCU',[{'value':'.*','label':'全部'}]+[{'value':'card'+str(i),'label':'card'+str(i)} for i in range(8)])

def derived_schema(path):
 return 'request-metrics-v2' if '.resources.queue.' in path or '.resources.service_requests.' in path or '.percentiles.' in path or path.rsplit('.',1)[-1] in ('requests','input_tokens','output_tokens','decode_tokens','latency_window_seconds') else 'v1'

def derived(path,scale=1):
 schema=derived_schema(path)
 s='{environment="dcu-pd",schema='+json.dumps(schema)+',path=~'+json.dumps(path)+'}'
 v='monitoring_chart_value'+s; ok='monitoring_chart_valid'+s
 return f'({v} and ({ok} == 1) and (min_over_time({ok}[$__interval]) == 1) and (count_over_time({ok}[$__interval]) >= ($__interval / 5)) and (time() - timestamp({v}) < 15)) * {scale}'

def raw(expr,job,extra=''):
 up='up{environment="dcu-pd",job=~"'+job+'"}'
 gate=f'(min_over_time({up}[$__interval]) == 1) and (count_over_time({up}[$__interval]) >= ($__interval / 5)) and (time() - timestamp({up}) < 15)'
 return f'({expr}) and on(job,instance) ({gate})'+extra

def gateway_rate(metric):
 s=metric+'{job="aigate",request_scope="all"}'
 marker='aigate_profile_counter_start_time_seconds{job="aigate",request_scope="all"}'
 return raw(f'rate({s}[1m]) and (resets({s}[1m]) == 0) and (count_over_time({s}[1m]) >= 12)','aigate',f' and on(job,instance) ((changes({marker}[1m]) == 0) and (count_over_time({marker}[1m]) >= 12) and (time() - {marker} >= 60) and on(job,instance) (min_over_time(up{{job="aigate"}}[1m]) == 1) and on(job,instance) (count_over_time(up{{job="aigate"}}[1m]) >= 12))')

def panel(title,queries,unit='',desc=''):
 return {'kind':'Panel','spec':{'display':{'name':title,'description':desc},'plugin':{'kind':'TimeSeriesChart','spec':{'legend':{'position':'bottom','mode':'list'},'yAxis':{'label':unit,'min':0},'visual':{'display':'line','lineWidth':1,'connectNulls':False},'tooltip':{'enablePinning':True}}},'queries':[{'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{'query':q,'seriesNameFormat':legend,'minStep':'5s'}}}} for q,legend in queries]}}
