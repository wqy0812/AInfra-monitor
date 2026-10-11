"""Read-only raw/Python parity and native chart publication acceptance."""
import argparse
import asyncio
import json
import math
import time

from . import chart_recording as charts, host_cpu
from .api import Service, get, VM


async def check(vm=VM, *, stored=False, start=None, end=None):
    end = int((time.time()-30)//5)*5 if end is None else end
    start = end-60 if start is None else start
    if start % 5 or end % 5 or not 0 <= end-start <= 295:
        raise ValueError('Use at most 60 aligned chart points')
    checks=[]
    for environment in ('dcu-pd','a3-vllm'):
        service=Service(environment)
        # The source export is explicit so the passed VM, not module defaults,
        # owns the read-only acceptance traffic.
        try:
            scope='{environment='+json.dumps(environment)+',job="'+('mooncake' if environment=='dcu-pd' else 'mooncake-a3')+'"}'
            response=await service.client.get(vm+'/api/v1/export',params={'match[]':scope,'start':start-80,'end':end,'reduce_mem_usage':1})
            response.raise_for_status()
            from .replay import decode_export, replay
            from . import a3
            raw=[json.loads(line) for line in response.text.splitlines() if line]
            parser,runner=(a3.decode_export,a3.replay) if environment=='a3-vllm' else (decode_export,replay)
            snapshots,points=runner(parser(raw),start-80,end)
            if environment=='a3-vllm':
                cpu,status=await host_cpu.collect(service.client,vm,start,end)
                if status!='ok':raise ValueError('CPU source query failed')
                host_cpu.attach(snapshots,points,cpu,status)
            async def query(expr):
                r=await service.client.get(vm+'/api/v1/query_range',params={'query':expr,'start':start,'end':end,'step':5,'nocache':1,'latency_offset':'1ms'})
                r.raise_for_status(); rows=r.json()['data']['result']
                out={}
                for row in rows:
                    for ts,value in row['values']:
                        if ts in out:raise ValueError('Ambiguous chart result')
                        out[ts]=float(value)
                return out
            for field in (f for f in charts.FIELDS if f['environment']==environment):
                expected={p['ts']:get(p,field['path']) for p in points if start<=p['ts']<=end}
                source=await query(field['expression'])
                for ts,value in expected.items():
                    actual=source.get(ts)
                    if not (actual==value or actual is not None and value is not None and math.isclose(actual,value,rel_tol=1e-9,abs_tol=1e-10)):
                        raise ValueError(f'Source parity failed: {environment}:{field["path"]}:{ts}: {actual} != {value}')
                item={'environment':environment,'path':field['path'],'source_points':len(expected),'source_valid_points':len(source)}
                if stored:
                    selector='{'+','.join(k+'='+json.dumps(v) for k,v in field.items() if k!='expression')+'}'
                    metric='monitoring_chart_valid'+selector
                    flags=await query(metric+' and (timestamp('+metric+') == time())')
                    metric='monitoring_chart_value'+selector
                    values=await query(metric+' and (timestamp('+metric+') == time())')
                    checked=0
                    for ts,flag in flags.items():
                        want=expected[ts]
                        if flag not in (0,1) or bool(flag)!=(want is not None):
                            raise ValueError(f'Native validity differs: {environment}:{field["path"]}:{ts}')
                        if flag:
                            if ts not in values:continue  # partial remote-write is a visible gap
                            if not math.isclose(values[ts],want,rel_tol=1e-9,abs_tol=1e-10):
                                raise ValueError(f'Native value differs: {environment}:{field["path"]}:{ts}')
                        checked+=1
                    if not checked:raise ValueError('No native chart points: '+environment+':'+field['path'])
                    item.update(native_checked_points=checked,missing_points=len(expected)-checked)
                checks.append(item)
        finally:await service.close()
    return {'passed':True,'start':start,'end':end,'stored':stored,'checks':checks}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm',default=VM);parser.add_argument('--stored',action='store_true')
    parser.add_argument('--start',type=int);parser.add_argument('--end',type=int)
    args=parser.parse_args()
    print(json.dumps(asyncio.run(check(args.vm,stored=args.stored,start=args.start,end=args.end)),ensure_ascii=False))


if __name__=='__main__':main()
