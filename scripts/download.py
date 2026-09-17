"""Download pinned upstream archives; verify official per-binary/archive SHA256."""
import hashlib,json,pathlib,tarfile,urllib.request,concurrent.futures
root=pathlib.Path(__file__).resolve().parents[1]/'vendor'
(root/'bin').mkdir(parents=True,exist_ok=True)
releases=json.loads((root/'releases.json').read_text())
def fetch(spec):
 url=spec['browser_download_url'];p=root/spec['name']
 if not p.exists():
  with urllib.request.urlopen(url,timeout=90) as r:p.write_bytes(r.read())
 return p
wanted=[]
for repo,d in releases.items():
 for a in d['assets']:
  if not any(x in a['name'] for x in ['enterprise','cluster']):wanted.append(a)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:files=list(pool.map(fetch,wanted))
manifest={}
for archive in files:
 if not archive.name.endswith('.tar.gz'):continue
 with tarfile.open(archive) as t:
  for member in t.getmembers():
   if pathlib.PurePosixPath(member.name).name not in ['victoria-metrics-prod','vmagent-prod','vmbackup-prod','vmrestore-prod','node_exporter']:continue
   data=t.extractfile(member).read();name=pathlib.PurePosixPath(member.name).name
   digest=hashlib.sha256(data).hexdigest()
   checks=(root/('sha256sums.txt' if name=='node_exporter' else archive.name.replace('.tar.gz','_checksums.txt'))).read_text()
   expected=hashlib.sha256(archive.read_bytes()).hexdigest() if name=='node_exporter' else digest
   assert expected in checks,(name,'checksum mismatch')
   p=root/'bin'/name;p.write_bytes(data);p.chmod(0o755);manifest[name]={'sha256':digest,'archive':archive.name,'url':next(a['browser_download_url'] for a in wanted if a['name']==archive.name)}
(root/'manifest.json').write_text(json.dumps(manifest,indent=2));print(json.dumps(manifest,indent=2))
