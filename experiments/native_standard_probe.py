"""Bounded semantic evidence for the unapproved native standard candidate.

Only injects into the disposable local test VM. Records native behavior rather
than assuming equivalence with the Python compatibility implementation.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.native_standard_rules import build, source_value, aggregate, histogram, ratio
from monitoring.latency import quantile_buckets


def main(output,approved=False):
    output.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(base_url='http://127.0.0.1:18543', trust_env=False, timeout=30)
    context = subprocess.check_output(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
    if not context.startswith('unix://'):
        raise RuntimeError('Local Docker only')
    labels = json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .Config.Labels}}', 'monitoring-test-vm'], text=True))
    if labels.get('monitoring.purpose') != 'local-regression-tests':
        raise RuntimeError('Not the disposable regression VM')
    token = 'native-standard-' + uuid.uuid4().hex[:10]
    end = int(time.time() // 5) * 5 - 300
    lines = []
    checks = []

    def emit(name, value, stamp, case, **extra):
        tags = {'environment':token, 'job':'fixture', 'instance':case+':1', 'node':case, **extra}
        label = ','.join(k+'='+json.dumps(v) for k,v in tags.items())
        lines.append(f'{name}{{{label}}} {value} {stamp * 1000}')

    cases = ('steady','burst','reset','gap','startup','stale','down','hist-overflow','hist-count-mismatch','no-events','zero-denominator','partial')
    for case in cases:
        for offset in range(-65, 1, 5):
            if case == 'gap' and offset == -30 or case == 'startup' and offset < -15 or case == 'stale' and offset > -20:
                continue
            stamp = end + offset
            value = 100 + (offset + 65) * 2
            if case == 'burst':value = 220 if offset == 0 else 100
            if case == 'reset' and offset >= -30:value = (offset + 30) * 2
            emit('up', 0 if case == 'down' and offset == 0 else 1, stamp, case)
            emit('events_total', value, stamp, case)
            emit('denominator_total', 100 if case == 'zero-denominator' else value * 2, stamp, case)
            if case == 'partial':
                emit('up', 0, stamp, case, instance=case+':2')
                emit('events_total', value, stamp, case, instance=case+':2')
            for bound, factor in [('1',.1), ('2',.2 if case == 'hist-overflow' else 1), ('+Inf',1)]:
                emit('latency_bucket', 100 if case == 'no-events' else value * factor, stamp, case, le=bound)
            emit('latency_count', 100 if case == 'no-events' else value * (2 if case == 'hist-count-mismatch' else 1), stamp, case)
    client.post('/api/v1/import/prometheus', content='\n'.join(lines)+'\n').raise_for_status()
    client.get('/internal/force_flush').raise_for_status()

    def query(expression, stamp=end):
        response = client.post('/api/v1/query', data={'query':expression,'time':stamp,'step':'5s','nocache':1,'latency_offset':'1ms'})
        response.raise_for_status()
        data = response.json()
        assert data['status'] == 'success', data
        return data['data']['result']

    if approved:
        from monitoring.native_rules import build as approved_build
        config,audit=approved_build()
        rule_count=audit['rule_count']
    else:
        config,audit=build();rule_count=audit['candidate_total_rules']
    # Every candidate expression is parsed and evaluated by the target VM version.
    for group in config['groups']:
        for rule in group['rules']:
            query(rule['expr'])
    scope = f'environment="{token}",job="fixture"'
    rate_expr = aggregate('events_total', scope)
    hist_expr = histogram('latency', scope)
    ratio_expr = ratio(aggregate('events_total', scope), aggregate('denominator_total', scope))
    rate_rows = query(rate_expr)
    values = {r['metric']['node']:float(r['value'][1]) for r in rate_rows}
    assert abs(values['steady']-2) < 1e-8
    assert abs(values['burst']-2) < 1e-8
    assert abs(values['gap']-2) < 1e-8
    assert abs(values['startup']-2) < 1e-8
    assert values['reset'] > 0
    assert 'stale' not in values and 'down' not in values
    assert abs(values['partial']-2) < 1e-8
    for case in ('steady','burst','reset','gap','startup','stale','down','partial'):
        checks.append({'case':case, 'native_rate':values.get(case),
                       'last_two_sample_rate':24 if case=='burst' else None})
    hist_rows = query(hist_expr)
    quantiles = {(r['metric']['node'],r['metric']['quantile']):float(r['value'][1]) for r in hist_rows}
    assert quantiles['hist-overflow','0.95'] == 2
    old = quantile_buckets({'1':12,'2':24,'+Inf':120}, {'1':0,'2':0,'+Inf':0}, .95)
    assert old is None
    checks.append({'case':'hist-overflow', 'native_p95':2, 'old_python_p95':old})
    assert ('hist-count-mismatch','0.95') in quantiles
    checks.append({'case':'hist-count-mismatch', 'native_p95':quantiles['hist-count-mismatch','0.95'],
                   'meaning':'count is not an input to native histogram_quantiles; no legacy count/bucket veto'})
    assert not any(case=='no-events' for case,q in quantiles)
    checks.append({'case':'no-events', 'native_quantiles':None})
    ratios = {r['metric']['node']:float(r['value'][1]) for r in query(ratio_expr)}
    assert ratios['steady'] == .5 and 'zero-denominator' not in ratios
    checks.append({'case':'zero-denominator', 'native_ratio':None})

    for short, expr in [('rate',rate_expr),('histogram',hist_expr),('ratio',ratio_expr)]:
        config['groups'][0]['rules'].append({'record':'monitoring_probe_'+short, 'expr':expr})
    config_path = output / 'probe-rules.json'
    config_path.write_text(json.dumps(config, indent=2)+'\n')
    images = {name:subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',name],text=True).strip()
              for name in ('victoriametrics/victoria-metrics:v1.151.0','victoriametrics/vmalert:v1.151.0')}
    image = images['victoriametrics/vmalert:v1.151.0']
    iso = lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    name = token + '-replay'
    created = False
    try:
        subprocess.run(['docker','create','--name',name,'--network','monitoring-local-tests_default',
            '--cpus','1','--memory','512m',image,'-rule=/rules.json',
            '-datasource.url=http://monitoring-test-vm:8428','-remoteWrite.url=http://monitoring-test-vm:8428',
            '-datasource.queryStep=0','-replay.timeFrom='+iso(end-10),'-replay.timeTo='+iso(end+1),
            '-replay.rulesDelay=0','-replay.disableProgressBar'],check=True,stdout=subprocess.DEVNULL)
        created = True
        subprocess.run(['docker','cp',str(config_path),name+':/rules.json'],check=True)
        subprocess.run(['docker','start',name],check=True,stdout=subprocess.DEVNULL)
        completed = subprocess.run(['docker','wait',name],text=True,capture_output=True,timeout=60)
        log = subprocess.run(['docker','logs',name],text=True,capture_output=True)
        (output/'replay.log').write_text(log.stdout+log.stderr)
        assert completed.returncode == 0 and completed.stdout.strip() == '0', completed
        client.get('/internal/force_flush').raise_for_status()
        replay_checks = {}
        for short, expr in [('rate',rate_expr),('histogram',hist_expr),('ratio',ratio_expr)]:
            raw = query(expr)
            stored = query('monitoring_probe_'+short+'{environment="'+token+'"}')
            normalize = lambda rows:{tuple(sorted((k,v) for k,v in r['metric'].items() if k!='__name__')):float(r['value'][1]) for r in rows}
            left, right = normalize(raw), normalize(stored)
            assert left.keys() == right.keys() and all(math.isclose(v,right[k],rel_tol=1e-10,abs_tol=1e-12) for k,v in left.items()), (short,raw,stored)
            replay_checks[short] = len(stored)
        report = {'status':'passed', 'candidate_status':'approved_local_verification' if approved else 'not_approved_not_deployed', 'source_samples':len(lines),
                  'images':images, 'candidate_expressions_evaluated':rule_count,
                  'semantic_cases':checks, 'native_replay':replay_checks,
                  'limits':'Synthetic representative cases; not live source inventory, dashboard, or API acceptance.'}
        (output/'probe.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        if created:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,check=False)
        client.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--approved',action='store_true')
    args=parser.parse_args()
    main(args.output.resolve(),args.approved)
