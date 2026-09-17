# Project-aware CLI routing; legacy helpers below remain importable.
if __name__ == "__main__":
    from project_seed import main
    main()
    raise SystemExit(0)

"""Create missing initial resources through the API. Never overwrite UI edits."""
import json,urllib.request,urllib.error,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
BASE='http://122.247.53.162:18431/api/v1'
for _ in range(45):
 try:urllib.request.urlopen(BASE+'/projects',timeout=2);break
 except (OSError,urllib.error.URLError):time.sleep(1)
else:raise RuntimeError('Perses API not ready')
for path,endpoint in [(ROOT/'project.json','/projects'),(ROOT/'datasource.json','/projects/dcu-monitoring/datasources')]+[(p,'/projects/dcu-monitoring/dashboards') for p in sorted((ROOT/'dashboards').glob('*.json'))]:
 obj=json.loads(path.read_text());name=obj['metadata']['name']
 try:urllib.request.urlopen(BASE+endpoint+'/'+name,timeout=10);print('Preserved',name);continue
 except urllib.error.HTTPError as e:
  if e.code!=404:raise
 req=urllib.request.Request(BASE+endpoint,data=json.dumps(obj).encode(),headers={'Content-Type':'application/json'},method='POST')
 try:
  with urllib.request.urlopen(req,timeout=20) as r:print('Created',name,r.status)
 except urllib.error.HTTPError as e:print(e.read().decode());raise
