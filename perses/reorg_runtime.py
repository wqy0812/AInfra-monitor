"""Install/restore generator files on test4, via SSH MCP only, with byte-level journal."""
import argparse
import base64
import json
from pathlib import Path

MODULES = ('align_dashboards.py', 'dashboard_reorg.py', 'project_split.py', 'project_release.py', 'generate_xpu.py',
           'metric_scope.py', 'generate.py', 'project_queries.py', 'dcu_bottlenecks.py',
           'remove_idle_thresholds.py', 'host_cpu_panel.py', 'npu_panels.py', 'xpu_hosts.py',
           'xpu_hardware.py', 'xpu_cache.py', 'query_acceleration.py', 'acceleration_catalog.py',
           'acceleration_publication.py', 'acceleration_state.json', 'a3_coverage.py')


def encoded(path):
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else None


def sync(root, runtime, rollback=False):
    journal = root/'runtime-install.json'
    if rollback:
        entries = json.loads(journal.read_text())
        for e in reversed(entries):
            path = runtime/e['path']; current = encoded(path)
            if current == e['before']: continue
            assert current == e['after'], 'Concurrent runtime edit: ' + e['path']
            if e['before'] is None:
                if path.exists(): path.unlink()
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(base64.b64decode(e['before']))
        print('Runtime restored');return
    assert not journal.exists(), 'Runtime journal already exists'
    release = root/'release'
    from dashboard_reorg import RETIRED
    files = {name:release/name for name in MODULES}
    files.update({str(p.relative_to(release)):p for p in (release/'projects').rglob('*.json')})
    for project,names in RETIRED.items():
        for name in names:files['projects/'+project+'/dashboards/'+name+'.json']=None
    if (release/'projects/a3-monitoring/dashboards/backend-prefill.json').exists():
        files['projects/a3-monitoring/dashboards/backend-diagnostics.json']=None
    entries = [{'path':name,'before':encoded(runtime/name),'after':encoded(source) if source else None}
               for name,source in files.items()]
    # Save entire rollback journal before touching any generator file.
    journal.write_text(json.dumps(entries,ensure_ascii=False,indent=2)+'\n')
    for e in entries:
        path=runtime/e['path']
        assert encoded(path)==e['before'], 'Concurrent runtime edit'
        if e['after'] is None:
            if path.exists():path.unlink()
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(base64.b64decode(e['after']))
        assert encoded(path)==e['after']
    print(json.dumps({'runtime_files':len(entries),'passed':True}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['apply','rollback']);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--runtime',type=Path,default=Path('/data2/monitoring/perses/release'));a=p.parse_args()
    sync(a.evidence,a.runtime,a.action=='rollback')
