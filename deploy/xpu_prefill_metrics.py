"""Extend only the XPU Prefill allowlist. Run on test4 through SSH MCP."""
from pathlib import Path
import json,subprocess
EXTRA='sglang:(load_back_tokens_total|evicted_tokens_total|(load_back|eviction)_duration_seconds_(bucket|sum|count)|hicache_scheduler_idle_with_pending(_seconds)?_total|num_used_tokens|max_total_num_tokens)'
def candidate(text):
 start=text.index('- job_name: sglang-prefill\n');end=text.index('\n- job_name:',start)
 part=text[start:end]
 marker='  - source_labels: [__name__]\n    regex: '
 assert part.count(marker)==1, 'Unexpected or already updated Prefill rule'
 part=part.replace(marker,'  - source_labels: [environment, __name__]\n    regex: ',1)
 lines=part.splitlines()
 for i,line in enumerate(lines):
  if line.startswith('    regex: '):
   old=line.split("'",2)[1];lines[i]="    regex: '(.*;"+old+'|xpu-pd;'+EXTRA+")'"
 return text[:start]+'\n'.join(lines)+text[end:]
def main():
 path=Path('/data2/monitoring/release/deploy/scrape.yml')
 root=Path('/data2/monitoring/releases/xpu-20260921')
 old=path.read_text();new=candidate(old)
 backup=root/'scrape-before-prefill-cache.yml';assert not backup.exists(),'Backup already exists'
 backup.write_text(old)
 out=root/'scrape-prefill-cache-candidate.yml';out.write_text(new)
 info=json.loads(subprocess.check_output(['docker','inspect','monitoring-vmagent']))[0]
 subprocess.check_call(['docker','run','--rm','--network=none','-v',str(out)+':/candidate.yml:ro','--entrypoint',info['Config']['Entrypoint'][0],info['Image'],'-promscrape.config=/candidate.yml','-promscrape.config.dryRun'])
 assert path.read_text()==old,'Concurrent config change'
 tmp=path.with_suffix('.prefill-cache.tmp');tmp.write_text(new);tmp.chmod(path.stat().st_mode);tmp.replace(path)
 subprocess.check_call(['docker','kill','--signal=HUP','monitoring-vmagent'])
 print('XPU Prefill allowlist hot reloaded; existing targets preserved')
if __name__=='__main__':main()
