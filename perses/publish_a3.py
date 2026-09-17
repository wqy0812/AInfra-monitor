# Project-aware CLI routing; legacy helpers below remain importable.
if __name__ == "__main__":
    raise SystemExit("此历史发布入口已退役。使用 project_release.py prepare/audit/apply --evidence DIR；资源按 project/name 定位。")
    raise SystemExit(0)

"""Create missing A3 dashboards and verify API normalization; preserve existing edits."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parent
BASE = 'http://122.247.53.162:18431/api/v1/projects/dcu-monitoring/dashboards'

def get(url):
    return json.load(urllib.request.urlopen(url, timeout=15))

def normalized(spec):
    obj = json.loads(json.dumps(spec))
    for var in obj.get('variables', []):
        s = var['spec']
        s.setdefault('allowAllValue', False)
        s.setdefault('allowMultiple', False)
        s.setdefault('display', {}).setdefault('hidden', False)
    for panel in obj.get('panels', {}).values():
        display = panel['spec'].get('display', {})
        if display.get('description') == '':
            del display['description']
    return obj


def main():
    backup=ROOT/'perses-before.json'
    if not backup.exists():backup.write_text(json.dumps(get(BASE),ensure_ascii=False,indent=2))
    before=json.loads(backup.read_text());created=[]
    for path in sorted((ROOT/'dashboards').glob('a3-*.json')):
        obj=json.loads(path.read_text());name=obj['metadata']['name']
        try:
            old=get(BASE+'/'+name)
            assert normalized(old['spec'])==normalized(obj['spec']),name+' has existing edits; preserved'
        except urllib.error.HTTPError as e:
            if e.code!=404:raise
            req=urllib.request.Request(BASE,data=json.dumps(obj).encode(),headers={'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(req,timeout=15) as r:assert r.status in (200,201)
        assert normalized(get(BASE+'/'+name)['spec'])==normalized(obj['spec'])
        created.append(name)
    after=get(BASE)
    for d in before:assert next(x for x in after if x['metadata']['name']==d['metadata']['name'])==d
    result={'created':created,'existing_unchanged':True,'dashboards':len(after)}
    (ROOT/'perses-created.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))

if __name__=='__main__':main()
