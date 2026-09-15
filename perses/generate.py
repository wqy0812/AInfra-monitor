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
 return 'request-streaming-v1' if '.resources.queue.' in path or '.resources.service_requests.' in path or '.percentiles.' in path or path.rsplit('.',1)[-1] in ('requests','input_tokens','output_tokens','decode_tokens','latency_window_seconds') else 'v1'

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
 s=metric+'{job="aigate",request_scope="streaming"}'
 marker='aigate_profile_counter_start_time_seconds{job="aigate",request_scope="streaming"}'
 return raw(f'rate({s}[1m]) and (resets({s}[1m]) == 0) and (count_over_time({s}[1m]) >= 12)','aigate',f' and on(job,instance) ((changes({marker}[1m]) == 0) and (count_over_time({marker}[1m]) >= 12) and (time() - {marker} >= 60) and (min_over_time(up{{job="aigate"}}[1m]) == 1) and (count_over_time(up{{job="aigate"}}[1m]) >= 12))')

def panel(title,queries,unit='',desc=''):
 return {'kind':'Panel','spec':{'display':{'name':title,'description':desc},'plugin':{'kind':'TimeSeriesChart','spec':{'legend':{'position':'bottom','mode':'list'},'yAxis':{'label':unit,'min':0},'visual':{'display':'line','lineWidth':1,'connectNulls':False},'tooltip':{'enablePinning':True}}},'queries':[{'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{'query':q,'seriesNameFormat':legend,'minStep':'5s'}}}} for q,legend in queries]}}

def dashboard(name,title,panels,variables):
 obj={'kind':'Dashboard','metadata':{'name':name,'project':PROJECT},'spec':{'display':{'name':title,'description':'数据源：test4 VictoriaMetrics。缺失或无效观测留空；默认 1 小时 / 15 秒刷新。'},'duration':'1h','refreshInterval':'15s','variables':variables,'panels':{'p'+str(i):p for i,p in enumerate(panels)},'layouts':[{'kind':'Grid','spec':{'items':[{'x':(i%2)*12,'y':(i//2)*8,'width':12,'height':8,'content':{'$ref':'#/spec/panels/p'+str(i)}} for i in range(len(panels))]}}]}}
 (ROOT/'dashboards'/f'{name}.json').write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')

if __name__ == '__main__':
 p=[panel('采集目标状态',[('up{environment="dcu-pd"} and (time() - timestamp(up{environment="dcu-pd"}) < 15)','{{job}}')],'1 = 正常')]
 for field,title,unit in [('requests','推理请求速率','请求 / 秒'),('output_tokens','输出 Token 吞吐','Token / 秒'),('decode_tokens','Decode Token 吞吐','Token / 秒')]:
  p.append(panel(title,[(derived('nodes.$role.'+field),'{{path}}')],unit,'源监控 output_tokens 有效性为 0 时留空，不补零；实时生成速度请同时查看 Decode Token 吞吐。' if field=='output_tokens' else ''))
 for kind,title in [('ttft','首 Token 延迟 TTFT'),('itl','Token 间延迟 ITL'),('e2e','端到端延迟 E2E')]:
  p.append(panel(title,[(derived('nodes.$role.percentiles.'+kind+'.'+q),'{{path}}') for q in ['p50','p95','p99']],'秒','沿用监控服务的 rank 去重与有效窗口；无样本留空。'))
 p.append(panel('主机 CPU',[(derived('nodes.$role.cpu'),'{{path}}')],'%'))
 dashboard('overview','运行概览',p,[ROLE])

 s='{environment="dcu-pd",node=~"$node"}'
 host=lambda e:raw(e,'node-.*')
 p=[panel('主机 CPU',[(host('100 * (1 - avg by(job,instance,node) (rate(node_cpu_seconds_total{mode="idle",node=~"$node"}[1m]) + ignoring(mode) rate(node_cpu_seconds_total{mode="iowait",node=~"$node"}[1m])))'),'{{node}}')],'%'),panel('主机内存',[(host('(node_memory_MemTotal_bytes'+s+' - node_memory_MemAvailable_bytes'+s+') / 1024^3'),'{{node}} 已用'),(host('node_memory_MemTotal_bytes'+s+' / 1024^3'),'{{node}} 总量')],'GiB')]
 fs='{node=~"$node",fstype!~"tmpfs|devtmpfs|overlay|squashfs"}'
 p.append(panel('文件系统容量使用率',[(host('100 * (1 - node_filesystem_avail_bytes'+fs+' / node_filesystem_size_bytes'+fs+')'),'{{node}} {{mountpoint}}')],'%'))
 for title,names in [('磁盘 I/O',[('read','读'),('written','写')]),('网络吞吐',[('receive','接收'),('transmit','发送')])]:
  qs=[]
  for name,label in names:
   m=('node_disk_'+name+'_bytes_total' if title=='磁盘 I/O' else 'node_network_'+name+'_bytes_total')
   filt='{node=~"$node",device!~"lo|loop.*|ram.*|veth.*|docker.*|br-.*"}'
   qs.append((host(f'rate({m}{filt}[1m]) / 1024^2'), '{{node}} {{device}} '+label))
  p.append(panel(title,qs,'MiB / 秒'))
 for metric,title,unit,scale in [('dcu_utilization_percent','DCU 利用率','%',1),('dcu_memory_used_bytes','DCU 显存已用','GiB',1/2**30),('dcu_temperature_celsius','DCU 温度','°C',1),('dcu_power_watts','DCU 功耗','W',1)]:
  expr=metric+'{node=~"$node",device=~"$device"}'
  gate=' and on(job,instance) (dcu_sample_success == 1 and (time() - dcu_sample_timestamp_seconds < 15))'
  p.append(panel(title,[(raw(f'{expr} * {scale}','dcu-.*',gate),'{{node}} {{device}}')],unit))
 dashboard('hosts-dcu','主机与 DCU',p,[NODE,DEVICE])

 p=[]
 for path,title,unit,scale in [('nodes.$role.cache_60s.ratio','模型缓存命中率','%',100),('nodes.$role.cache_60s.(device|host|storage)','缓存命中分层','%',100),('nodes.$role.hicache.representative.ratio','HiCache 容量使用率','%',100),('nodes.$role.hicache.representative.(used|total)','HiCache Token 容量','Token',1),('mooncake.capacity.(used|total)','Mooncake 内存配额','GiB',1/2**30),('mooncake.ssd_capacity.(used|total)','Mooncake SSD 配额','GiB',1/2**30),('mooncake.query_60s.ratio','Store 查询成功比例','%',100),('mooncake.tier_query_60s.(memory|ssd)','Store 分层查询命中率','%',100)]:
  p.append(panel(title,[(derived(path,scale),'{{path}}')],unit,'缓存窗口约 60 秒。Store 查询命中不等同于模型 Token 命中、读取成功或物理 SSD I/O；SSD 容量是后端配额。'))
 dashboard('cache-store','HiCache 与 Mooncake',p,[ROLE])
 p=[panel('在途请求',[(raw('aigate_requests_inflight{job="aigate",request_scope="streaming"}','aigate'),'在途')],'请求'),panel('请求到达速率',[(gateway_rate('aigate_requests_started_total'),'到达')],'请求 / 秒','1 分钟窗口；生命周期变化、计数重置和采集断档留空。')]
 for metric,title,unit in [('aigate_profile_queue_events','画像队列','事件'),('aigate_profile_index_entries','画像索引条目','条目'),('aigate_profile_index_ready','画像索引就绪','1 = 就绪')]:
  p.append(panel(title,[(raw(metric+'{job="aigate",request_scope="streaming"}','aigate'),title)],unit))
 for metric,title in [('aigate_profile_write_errors_total','画像写入错误速率'),('aigate_profile_dropped_events_total','画像事件丢弃速率')]:p.append(panel(title,[(gateway_rate(metric),title)],'事件 / 秒'))
 p.append(panel('画像存储量',[(raw('aigate_profile_stored_bytes{job="aigate",request_scope="streaming"} / 1024^2','aigate'),'存储')],'MiB'))
 dashboard('gateway','网关基础状态',p,[])

 project={'kind':'Project','metadata':{'name':PROJECT},'spec':{'display':{'name':'DCU 监控'}}}
 endpoints=[{'endpointPattern':p,'method':m} for p,methods in [('/api/v1/query',['GET','POST']),('/api/v1/query_range',['GET','POST']),('/api/v1/labels',['GET','POST']),('/api/v1/series',['GET','POST']),('/api/v1/metadata',['GET']),('/api/v1/label/([a-zA-Z0-9_-]+)/values',['GET'])] for m in methods]
 ds={'kind':'Datasource','metadata':{'name':'victoriametrics','project':PROJECT},'spec':{'display':{'name':'test4 VictoriaMetrics'},'default':True,'plugin':{'kind':'PrometheusDatasource','spec':{'scrapeInterval':'5s','proxy':{'kind':'HTTPProxy','spec':{'url':'http://127.0.0.1:18428','allowedEndpoints':endpoints}}}}}}
 for name,obj in [('project',project),('datasource',ds)]: (ROOT/f'{name}.json').write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
 print('Generated 4 dashboards:',sum(len(json.loads(p.read_text())['spec']['panels']) for p in (ROOT/'dashboards').glob('*.json')),'panels')
