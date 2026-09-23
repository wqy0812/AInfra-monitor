"""Run on test4 through SSH MCP; repair collection before publishing XPU roles."""
import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, '/data2/monitoring/perses/release')
import project_release as r

CONFIG = Path('/data2/monitoring/release/deploy/scrape.yml')


def reload_config(content):
    tmp = CONFIG.with_suffix('.xpu-role.tmp')
    tmp.write_bytes(content); tmp.chmod(CONFIG.stat().st_mode); tmp.replace(CONFIG)
    subprocess.check_call(['docker', 'kill', '--signal=HUP', 'monitoring-vmagent'])


def instant(q):
    return r.http(r.VM+'/api/v1/query?'+r.urllib.parse.urlencode({'query': q, 'nocache': 1}))['data']['result']


def verify(root):
    checks = []
    for role, node, ip in [('prefill','xpu-1','122.209.21.33'), ('decode','xpu-2','122.209.21.34')]:
        for job, port in [('sglang-'+role,8501), ('node-xpu',9110), ('xpu-hardware',9507)]:
            s = f'environment="xpu-pd",role="{role}",node="{node}",job="{job}",instance="{ip}:{port}"'
            rows = instant('min_over_time(up{'+s+'}[2m])')
            counts = instant('count_over_time(up{'+s+'}[2m])')
            assert len(rows)==len(counts)==1 and float(rows[0]['value'][1])==1 and float(counts[0]['value'][1])>=23, (job, rows, counts)
            checks.append({'target':ip+':'+str(port),'role':role,'samples':counts[0]['value'][1]})
    raw=urllib.request.urlopen('http://122.209.21.33:8501/metrics',timeout=15).read().decode()
    (root/'prefill.metrics').write_text(raw)
    for name in ('cache_hit_rate','cached_tokens_total','load_back_tokens_total','evicted_tokens_total','load_back_duration_seconds_sum','load_back_duration_seconds_count','eviction_duration_seconds_sum','eviction_duration_seconds_count'):
        metric='sglang:'+name
        assert metric+'{' in raw
        rows=instant(metric+'{environment="xpu-pd",job="sglang-prefill",node="xpu-1",instance="122.209.21.33:8501"}')
        assert rows, metric
    r.save(root,'collection-accepted.json',{'passed':True,'targets':checks,'at':time.time()})
    return checks


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['collect','publish','install','rollback']);p.add_argument('--evidence',type=Path,required=True);a=p.parse_args();root=a.evidence
    old=(root/'scrape-before.yml').read_bytes();new=(root/'scrape-candidate.yml').read_bytes()
    runtime=Path('/data2/monitoring/perses/release')
    def encoded(path):return base64.b64encode(path.read_bytes()).decode() if path.exists() else None
    if a.action=='install':
        assert json.loads((root/'publication.json').read_text())['passed']
        assert not (root/'runtime-install.json').exists()
        payload=json.loads((root/'runtime-payload.json').read_text())
        entries=[{'path':name,'before':encoded(runtime/name),'after':data} for name,data in payload.items()]
        r.save(root,'runtime-install.json',entries)
        for e in entries:
            path=runtime/e['path'];assert encoded(path)==e['before'],'Concurrent runtime edit'
            path.write_bytes(base64.b64decode(e['after']))
        print('Runtime files installed');return
    if a.action=='rollback':
        if (root/'journal.json').exists(): r.rollback(root)
        assert CONFIG.read_bytes() in (old,new), 'Concurrent scrape edit'
        reload_config(old)
        if (root/'runtime-install.json').exists():
            for e in reversed(json.loads((root/'runtime-install.json').read_text())):
                path=runtime/e['path'];current=encoded(path)
                if current==e['before']:continue
                assert current==e['after'],'Concurrent runtime edit'
                if e['before'] is None:path.unlink()
                else:path.write_bytes(base64.b64decode(e['before']))
        return
    if a.action=='collect':
        assert CONFIG.read_bytes()==old,'Concurrent scrape edit'
        c=json.loads(subprocess.check_output(['docker','inspect','monitoring-vmagent']))[0]
        subprocess.check_call(['docker','run','--rm','--network=none','-v',str(root/'scrape-candidate.yml')+':/candidate.yml:ro','--entrypoint',c['Config']['Entrypoint'][0],c['Image'],'-promscrape.config=/candidate.yml','-promscrape.config.dryRun'])
        r.save(root,'collection-journal.json',{'at':time.time()})
        try:
            assert CONFIG.read_bytes()==old
            reload_config(new)
            assert r.fingerprint()==json.loads((root/'services-before.json').read_text())
        except Exception:
            if CONFIG.read_bytes()==new:reload_config(old)
            raise
        print('Collection reloaded without restart');return
    assert CONFIG.read_bytes()==new
    verify(root)
    candidate=json.loads((root/'candidate.json').read_text());r.validate(candidate)
    before=json.loads((root/'before.json').read_text())
    for key, d in r.flattened(before).items():
        if key[1]!='xpu-monitoring':assert d==r.flattened(candidate)[key]
    cache=next(d for d in candidate['dashboards'] if d['metadata']['project']=='xpu-monitoring' and d['metadata']['name']=='cache-store')
    checks=[]
    for key,panel in cache['spec']['panels'].items():
        q=panel['spec']['queries'][0]['spec']['plugin']['spec']['query']
        rows=instant(q)
        # Mean durations are undefined in an idle window; preserve the empty result.
        if key not in ('p5','p6'):assert rows,(key,q)
        assert all(x['metric'].get('node')=='xpu-1' for x in rows)
        checks.append({'panel':key,'result':rows})
    r.save(root,'semantics.json',{'passed':True,'cache':checks})
    assert r.audit(candidate,root)['passed']
    r.apply(candidate,root)


if __name__=='__main__':main()
