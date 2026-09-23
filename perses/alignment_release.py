"""SSH-MCP-only release helper: native Mooncake collection and live equivalence."""
import argparse
import concurrent.futures
import json
import re
import subprocess
import time
from pathlib import Path
import project_release as r
from align_dashboards import NODES, MOONCAKE_METRICS, exprs

CONFIG=Path('/data2/monitoring/release/deploy/scrape.yml')


def write_config(content):
    tmp=CONFIG.with_suffix('.alignment.tmp');tmp.write_bytes(content);tmp.chmod(CONFIG.stat().st_mode);tmp.replace(CONFIG)
    subprocess.check_call(['docker','kill','--signal=HUP','monitoring-vmagent'])


def collect(root):
    old=(root/'scrape-before.yml').read_bytes();new=(root/'scrape-candidate.yml').read_bytes()
    assert CONFIG.read_bytes()==old,'Concurrent scrape edit'
    c=json.loads(subprocess.check_output(['docker','inspect','monitoring-vmagent']))[0]
    subprocess.check_call(['docker','run','--rm','--network=none','-v',str(root/'scrape-candidate.yml')+':/candidate.yml:ro','--entrypoint',c['Config']['Entrypoint'][0],c['Image'],'-promscrape.config=/candidate.yml','-promscrape.config.dryRun'])
    r.save(root,'collection-journal.json',{'action':'reload','at':time.time()})
    try:
        assert CONFIG.read_bytes()==old
        write_config(new)
        assert r.fingerprint()==json.loads((root/'services-before.json').read_text())
    except Exception:
        if CONFIG.read_bytes()==new:write_config(old)
        raise
    print('Mooncake scrape config reloaded; no service restart',flush=True)


def instant(query):
    params=r.urllib.parse.urlencode({'query':query})
    return r.http(r.VM+'/api/v1/query?'+params)['data']['result']


def acceptance(root):
    assert CONFIG.read_bytes()==(root/'scrape-candidate.yml').read_bytes()
    # Require a continuous two-minute window from the actual central collector.
    rows=instant('min_over_time(up{environment="a3-vllm",job="mooncake-a3"}[2m])')
    samples=instant('count_over_time(up{environment="a3-vllm",job="mooncake-a3"}[2m])')
    assert len(rows)==len(samples)==1 and float(rows[0]['value'][1])==1 and float(samples[0]['value'][1])>=23,(rows,samples)
    q='count by(__name__) ({environment="a3-vllm",job="mooncake-a3"})'
    observed={x['metric']['__name__'] for x in instant(q)}
    assert set(MOONCAKE_METRICS)<=observed, set(MOONCAKE_METRICS)-observed
    r.save(root,'collection-accepted.json',{'passed':True,'metrics':sorted(observed),'up':rows,'samples':samples})


def audit_semantics(root):
    before=json.loads((root/'before.json').read_text());after=r.read_resources(root/'release/projects')
    manifest=json.loads((root/'migration.json').read_text())
    by=lambda ds:{(d['metadata']['project'],d['metadata']['name']):d for d in ds}
    old,new=by(before['dashboards']),by(after['dashboards'])
    end=int(time.time()//60)*60-60;checks=[]
    def expand(q,project,role=None,new=False):
        value='.*' if role is None else ('('+role+'|'+NODES[project][0 if role=='prefill' else 1]+')' if new else role)
        return q.replace('$role',value).replace('$node','.*').replace('$device','.*')
    def selected(rows,project,role):
        if role is None:return rows
        node=NODES[project][0 if role=='prefill' else 1]
        return [x for x in rows if x['metric'].get('node')==node or x['metric'].get('role')==role or x['metric'].get('path','').startswith('nodes.'+role+'.')]
    jobs=[]
    for m in manifest:
        a=old[m['project'],m['source']]['spec']['panels'][m['panel']]
        b=new[m['project'],m['target']]['spec']['panels'][m['target_panel']]
        for qa,qb in zip(exprs(a),exprs(b)):
            # Every changed expression, with both role selections as applicable.
            if qa==qb:continue
            for role in ([None,'prefill','decode'] if m['selectable'] else [m['role']]):
                for seconds,step in [(1800,15),(86400,300)]:jobs.append((m,qa,qb,role,seconds,step))
    def check(job):
        m,qa,qb,role,seconds,step=job;project=m['project']
        # Read old with its original full population, then partition by labels.
        a=r.query(r.VM,expand(qa,project),end-seconds,end,step)
        a=selected(a,project,role)
        b=r.query(r.VM,expand(qb,project,role,new=True),end-seconds,end,step)
        assert r.equivalent(a,b),(project,m['source'],m['panel'],role,seconds,len(a),len(b))
        return dict(project=project,panel=m['panel'],role=role,seconds=seconds,step=step,series=len(b))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for c in pool.map(check,jobs):
            checks.append(c)
            if len(checks)%60==0:print(json.dumps({'semantic_checks':len(checks),'total':len(jobs)}),flush=True)
    r.save(root,'alignment-query-checks.json',{'passed':True,'checks':checks,'at':time.time()})
    print(json.dumps({'semantic_checks':len(checks),'passed':True}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['collect','verify','apply','rollback']);p.add_argument('--evidence',type=Path,required=True);a=p.parse_args();root=a.evidence
    if a.action=='collect':collect(root)
    elif a.action=='verify':
        acceptance(root);audit_semantics(root)
        resources=r.read_resources(root/'release/projects');r.validate(resources)
        assert r.audit(resources,root)['passed']
    elif a.action=='apply':
        acceptance(root)
        assert json.loads((root/'alignment-query-checks.json').read_text())['passed']
        r.apply(r.read_resources(root/'release/projects'),root)
    else:
        if (root/'journal.json').exists():r.rollback(root)
        assert CONFIG.read_bytes() in [(root/'scrape-before.yml').read_bytes(),(root/'scrape-candidate.yml').read_bytes()]
        write_config((root/'scrape-before.yml').read_bytes())
