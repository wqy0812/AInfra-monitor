# Project-aware CLI routing; legacy helpers below remain importable.
if __name__ == "__main__":
    from project_release import main
    main()
    raise SystemExit(0)

"""Explicit operator update of generated dashboards; snapshot existing definitions first."""
import json,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent;BASE='http://122.247.53.162:18431/api/v1/projects/dcu-monitoring/dashboards'
backup=ROOT.parent/'evidence'/('dashboards-before-'+str(int(time.time())));backup.mkdir(parents=True)
for p in sorted((ROOT/'dashboards').glob('*.json')):
 new=json.loads(p.read_text());url=BASE+'/'+new['metadata']['name']
 with urllib.request.urlopen(url) as r:old=json.load(r)
 (backup/p.name).write_text(json.dumps(old,ensure_ascii=False,indent=2))
 old['spec']=new['spec']
 req=urllib.request.Request(url,data=json.dumps(old).encode(),headers={'Content-Type':'application/json'},method='PUT')
 with urllib.request.urlopen(req) as r:print(new['metadata']['name'],r.status)
