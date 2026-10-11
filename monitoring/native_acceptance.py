"""Read-only raw-expression versus stored-record acceptance at one sealed tick."""
import argparse
import asyncio
import json
import math
import re
import time
import httpx
from .native_rules import build


def normalized(rows,labels=None):
    result={}
    for row in rows:
        tags={k:v for k,v in row['metric'].items() if k!='__name__'}
        tags.update(labels or {})
        for key in list(tags):
            if key.startswith('exported_') and tags.get(key[9:])==tags[key]:del tags[key]
        identity=tuple(sorted(tags.items()))
        if identity in result:raise ValueError('Duplicate recorded identity')
        result[identity]=float(row['values'][0][1])
    return result


async def check(vm,tick=None):
    tick=tick or int((time.time()-20)//5)*5
    config,meta=build();semaphore=asyncio.Semaphore(2)
    async with httpx.AsyncClient(base_url=vm,trust_env=False,timeout=30) as client:
        async def query(expr):
            response=await client.post('/api/v1/query_range',data={'query':expr,'start':tick,'end':tick,'step':5,'nocache':1,'latency_offset':'1ms'})
            response.raise_for_status();data=response.json()
            if data.get('status')!='success' or data.get('isPartial'):raise ValueError('VM failed')
            return data['data']['result']
        async def compare(rule):
            async with semaphore:
                result={'record':rule['record'],'labels':rule.get('labels',{})}
                try:
                    filters=dict(rule.get('labels',{}))
                    for key in ('environment','node'):
                        values=set(re.findall(key+r'="([^"{}]+)"',rule['expr']))
                        if len(values)==1:filters[key]=values.pop()
                    selector=rule['record']+'{'+','.join(k+'='+json.dumps(v) for k,v in filters.items())+'}'
                    raw,stored=await asyncio.gather(query(rule['expr']),query(selector+' and (timestamp('+selector+') == time())'))
                    before,after=normalized(raw,rule.get('labels')),normalized(stored)
                    equal=before.keys()==after.keys() and all(math.isclose(v,after[k],rel_tol=1e-8,abs_tol=1e-9) for k,v in before.items())
                    result.update(passed=equal,raw_series=len(before),stored_series=len(after))
                    if not equal:result.update(raw=raw[:3],stored=stored[:3])
                except Exception as error:result.update(passed=False,error=type(error).__name__+': '+str(error)[:300])
                return result
        checks=await asyncio.gather(*(compare(rule) for group in config['groups'] for rule in group['rules']))
    return {'passed':all(c['passed'] for c in checks),'tick':tick,'rule_count':meta['rule_count'],
            'nonempty_rules':sum(c.get('stored_series',0)>0 for c in checks),'checks':checks}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--vm',default='http://127.0.0.1:18428')
    parser.add_argument('--tick',type=int);args=parser.parse_args()
    report=asyncio.run(check(args.vm,args.tick));print(json.dumps(report,ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
