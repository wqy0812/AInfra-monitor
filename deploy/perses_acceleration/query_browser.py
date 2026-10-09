"""Generate/run Playwright CLI acceptance against two disposable Perses servers."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path


SCRIPT = r'''async (page) => {
  const input = __INPUT__;
  const checks = [], errors = [], requests = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', r => { if (r.url().includes('/query_range')) requests.push({url:r.url(),status:r.status()}); });
  await page.setViewportSize({width:1920,height:1080});
  const targets = [...new Set(input.changes.map(c=>c.project+'/'+c.dashboard))];
  for (const [testCase,endSeconds] of Object.entries(input.ends)) {
    for (const target of targets) {
      const [project,dashboard] = target.split('/');
      const chosen = input.changes.filter(c=>c.project===project && c.dashboard===dashboard &&
        (['normal','zero'].includes(testCase) || c.kind==='generation-results'));
      if (!chosen.length) continue;
      const snapshots = [];
      for (const [version,port] of [['before',input.beforePort],['after',input.afterPort]]) {
        const end=endSeconds*1000;
        await page.goto(`http://127.0.0.1:${port}/projects/${project}/dashboards/${dashboard}?start=${end-300000}&end=${end}`);
        await page.getByText('固定窗口 · 自动刷新已暂停').waitFor();
        // Read current accessibility state before interacting with group controls.
        await page.locator('body').ariaSnapshot();
        const document=await (await page.request.get(`http://127.0.0.1:${port}/api/v1/projects/${project}/dashboards/${dashboard}`)).json();
        const panels={};
        for (const change of chosen) {
          const title=change.title;
          const layout=document.spec.layouts.find(l=>(l.spec.items||[]).some(i=>i.content?.$ref==='#/spec/panels/'+change.panel));
          const group=layout?.spec.display?.title;
          if (group) {
            const escaped=group.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
            const control=page.getByRole('button',{name:new RegExp('^(expand|collapse) group '+escaped+'$')});
            await control.waitFor();
            await control.scrollIntoViewIfNeeded();
            const expand=page.getByRole('button',{name:'expand group '+group,exact:true});
            if (await expand.count()) await expand.click();
          }
          const panel=page.getByRole('region').filter({has:page.getByRole('heading',{name:title,exact:true})});
          const actual=document.spec.panels[change.panel].spec.queries;
          const digest=await page.evaluate(async text=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text)))).map(x=>x.toString(16).padStart(2,'0')).join(''),JSON.stringify(actual));
          if (digest!==change[version+'_sha256']) throw Error('Query fingerprint: '+target+'/'+change.panel);
          for (let attempt=0;attempt<20 && !(await panel.count());attempt++) {
            await page.mouse.move(1200,800); await page.mouse.wheel(0,500); await page.waitForTimeout(150);
          }
          await panel.scrollIntoViewIfNeeded();
          if (!(testCase==='zero' && change.kind==='histogram-monotonic'))
            await panel.getByRole('listitem').first().waitFor({timeout:30000});
          // Wait until target data/empty state finishes rendering; no artificial response mocking.
          await page.waitForTimeout(300);
          panels[change.panel]=await panel.getByRole('listitem').evaluateAll(nodes=>nodes.map(n=>({
            name:n.innerText,color:n.querySelector('[style*="background-color"]')?.style.backgroundColor})));
          if (panels[change.panel].some(x=>!x.color)) throw Error('Missing legend colour');
          if (testCase==='zero' && change.kind==='histogram-monotonic' && panels[change.panel].length) throw Error('Histogram zero must be blank');
          if (testCase==='normal' && project==='a3-monitoring' && ['generation-0','extra-first'].includes(change.panel))
            await panel.screenshot({path:input.output+'/'+change.panel+'-'+version+'.png'});
        }
        if (await page.evaluate(()=>document.documentElement.scrollWidth>1920)) throw Error('Horizontal overflow');
        if (testCase==='normal' && (document.spec.variables||[]).some(v=>v.spec.name==='role')) {
          await page.locator('body').ariaSnapshot();
          const role=page.getByRole('combobox',{name:'角色',exact:true});
          const wanted=document.spec.variables.find(v=>v.spec.name==='role').spec.plugin.spec.values.find(v=>/^Prefill/.test(v.label)).value;
          const filtered=page.waitForResponse(r=>{
            if (!r.url().includes('/query_range') || r.status()!==200) return false;
            const p=new URLSearchParams(r.request().postData() || new URL(r.url()).search);
            const expression=p.get('query')||'';
            return expression.includes('node=~"'+wanted+'"') || expression.includes('nodes.'+wanted+'.');
          },{timeout:30000});
          await role.click();
          await page.getByRole('option',{name:/^Prefill(?: \/.*)?$/}).click();
          await filtered;
          panels.__role_prefill__='filter reached datasource';
        }
        snapshots.push(panels);
      }
      checks.push({project,dashboard,testCase,before:snapshots[0],after:snapshots[1],passed:JSON.stringify(snapshots[0])===JSON.stringify(snapshots[1])});
    }
  }
  const passed=checks.every(c=>c.passed) && !errors.length && requests.every(r=>r.status===200);
  return {passed,candidate_sha256:input.sha,viewport:{width:1920,height:1080},
    scope:'local copy of deployed Perses binary/plugins; real disposable VM fixtures; no production performance claim',checks,errors,requests};
}'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='Existing output/playwright directory')
    parser.add_argument('--cli', type=Path, required=True, help='Installed Playwright CLI wrapper')
    parser.add_argument('--session', default='query-slimming')
    parser.add_argument('--before-port', type=int, default=18564)
    parser.add_argument('--after-port', type=int, default=18565)
    args = parser.parse_args()
    assert args.output.is_dir() and args.evidence.is_dir()
    read = lambda n: json.loads((args.evidence / n).read_text())
    synthetic = read('query-synthetic.json')
    assert synthetic['passed']
    changes = []
    for c in read('query-changes.json'):
        item = {k: c[k] for k in ('project', 'dashboard', 'panel', 'kind')}
        item['title'] = c['before']['spec']['display']['name']
        for version in ('before', 'after'):
            raw = json.dumps(c[version]['spec']['queries'], ensure_ascii=False, separators=(',', ':')).encode()
            item[version + '_sha256'] = hashlib.sha256(raw).hexdigest()
        changes.append(item)
    data = {'changes': changes, 'ends': read('query-fixtures.json')['ends'],
            'sha': synthetic['candidate_sha256'], 'output': str(args.output.resolve()),
            'beforePort': args.before_port, 'afterPort': args.after_port}
    script = args.output / 'query-browser.js'
    script.write_text(SCRIPT.replace('__INPUT__', json.dumps(data, ensure_ascii=False)))
    completed = subprocess.run([str(args.cli), '-s=' + args.session, 'run-code', '--filename', str(script.resolve())],
                               cwd=args.output, text=True, capture_output=True, timeout=600)
    (args.output / 'query-browser.log').write_text(completed.stdout + completed.stderr)
    completed.check_returncode()
    assert '### Result\n' in completed.stdout, 'Browser run failed; see query-browser.log'
    report = json.loads(completed.stdout.split('### Result\n', 1)[1].split('\n### ', 1)[0])
    (args.evidence / 'query-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    assert report['passed'], 'Browser legend/colour acceptance failed'
    print(json.dumps({'passed': True, 'checks': len(report['checks'])}))


if __name__ == '__main__':
    main()
