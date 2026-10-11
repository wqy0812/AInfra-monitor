"""Read-only deployed catalog parity and native coverage acceptance."""
import argparse
import asyncio
import itertools
import json
from pathlib import Path
import time

import httpx
from .perses_recording import RecordingService
from .perses_acceleration import points_by_time, equal_values


async def check(args):
    catalog = json.loads(args.catalog.read_text())
    checks=[]
    async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
        service=RecordingService(args.vm, args.state, catalog, client=client)
        for panel in catalog['panels']:
            for step in catalog['steps']:
                end=int((time.time()-120)//step)*step
                ticks=[end-step,end]
                stored=await service.stored(panel,step,ticks)
                bindings_list=[None]
                if args.filters:
                    names=sorted(panel['variables'])
                    bindings_list += [dict(zip(names,values)) for values in itertools.product(*(panel['variables'][n] for n in names))]
                for bindings in bindings_list:
                    query=service.expression(panel,step,bindings)
                    raw=await service.vm_query(query,ticks[0],ticks[-1],step)
                    response=await client.post(args.api+'/internal/perses/api/v1/query_range',data={
                        'query':query,'start':str(ticks[0]),'end':str(ticks[-1]),'step':str(step),'nocache':'1'})
                    response.raise_for_status()
                    body=response.json()
                    assert body['status']=='success' and not body.get('warnings') and not body.get('isPartial')
                    expected=points_by_time(raw,ticks); actual=points_by_time(body['data']['result'],ticks)
                    assert all(equal_values(expected[t],actual[t]) for t in ticks),(panel['id'],step,bindings)
                    checks.append({'panel':panel['id'],'step':step,'bindings':bindings,'native_ticks':len(stored),'passed':True})
        short=[c for c in checks if c['step']==5 and c['bindings'] is None]
        assert len(short)==len(catalog['panels']) and all(c['native_ticks']==2 for c in short), 'Short-step native coverage incomplete'
        if args.require_full: assert all(c['native_ticks']==2 for c in checks),'Native coverage incomplete'
        return {'passed':True,'checks':checks,'native_jobs':sum(c['native_ticks']==2 and c['bindings'] is None for c in checks),
                'total_jobs':len(catalog['panels'])*len(catalog['steps']), 'at':time.time()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,default=Path('/monitoring/monitoring/perses_acceleration_catalog.json'))
    parser.add_argument('--state',type=Path,default=Path('/state'))
    parser.add_argument('--vm',default='http://127.0.0.1:18428')
    parser.add_argument('--api',default='http://127.0.0.1:18430')
    parser.add_argument('--filters',action='store_true')
    parser.add_argument('--require-full',action='store_true')
    print(json.dumps(asyncio.run(check(parser.parse_args()))))


if __name__=='__main__':main()
