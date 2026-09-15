import asyncio,json,math
import httpx,pytest
from monitoring.calculator import Calculator,parse_prom
from monitoring.replay import decode_export,latest_rows,replay,gpus,host
from monitoring.exporter import parse_csv,render
from monitoring.api import encode,PATHS,Service

def test_scrape_labels_and_stale_markers():
 d=decode_export([{'metric':{'__name__':'total_get_nums_','job':'mooncake','instance':'x','environment':'dcu-pd'},'timestamps':[100000,105000],'values':[20,None]}])
 ts,rows=latest_rows(d,'mooncake',104)
 assert ts==100 and rows[0]['labels']=={}
 assert latest_rows(d,'mooncake',116)==(None,[])

def test_dcu_failure_and_aging_remove_values():
 text='device,HCU use (%),vram Total Memory (MiB),vram Total Used Memory (MiB)\ncard0,4,100,50\n'
 state={'ts':100,'ok':True,'rows':parse_csv(text)}
 raw=render(state,105)
 assert 'dcu_memory_used_bytes{device="card0"} 52428800.0' in raw
 assert 'dcu_memory_used_bytes' not in render(state,116)
 state['ok']=False
 assert 'dcu_sample_success 0' in render(state,105)
 assert gpus(parse_prom(raw),116)==[]

def test_calculator_resets_and_deduplicates():
 c=Calculator()
 def rows(n):
  lines=[]
  for tp in (0,1):
   labels=f'is_streaming="true",engine_type="decode",model_name="m",tp_rank="{tp}"'
   lines.append('sglang:num_running_reqs{'+labels+'} 0')
   lines.append('sglang:realtime_tokens_total{'+labels+',mode="decode"} '+str(n))
  return parse_prom('\n'.join(lines))
 assert c.metrics('decode',rows(100),100)['rates']['decode_tokens'] is None
 assert c.metrics('decode',rows(200),105)['rates']['decode_tokens']==20
 assert c.metrics('decode',rows(10),110)['rates']['decode_tokens'] is None
 assert c.metrics('decode',rows(20),140)['rates']['decode_tokens'] is None

def test_missing_sources_are_null_with_explicit_validity():
 snaps,points=replay({},100,110)
 assert snaps[-1]['nodes']['prefill']['metrics']['status']=='error'
 text=encode(points)
 assert 'monitoring_chart_valid{path="nodes.prefill.cpu",environment="dcu-pd",schema="v1"} 0' in text
 assert 'nan' not in text.lower()

def test_host_reset_no_spike():
 rows=parse_prom('node_boot_time_seconds 1\nnode_cpu_seconds_total{cpu="0",mode="idle"} 100\nnode_cpu_seconds_total{cpu="0",mode="user"} 100')
 after=parse_prom('node_boot_time_seconds 1\nnode_cpu_seconds_total{cpu="0",mode="idle"} 103\nnode_cpu_seconds_total{cpu="0",mode="user"} 102')
 assert host(after,rows,105,100)['cpu_percent']==40
 after[0]['value']=2
 assert host(after,rows,105,100)['cpu_percent'] is None

@pytest.mark.asyncio
async def test_query_failures_do_not_advance_watermark(tmp_path,monkeypatch):
 import monitoring.api as api
 monkeypatch.setattr(api,'STATE',tmp_path)
 s=Service();s.watermark=100
 async def fail(*args):raise httpx.ConnectError('offline')
 monkeypatch.setattr(s,'raw',fail)
 with pytest.raises(httpx.ConnectError):await s.cycle()
 assert s.watermark==100 and not list(tmp_path.iterdir())
 await s.client.aclose()
