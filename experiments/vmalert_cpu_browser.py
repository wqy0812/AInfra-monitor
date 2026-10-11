"""Playwright CLI check of the unchanged Perses UI with CPU candidate data."""
import argparse
import json
import subprocess
from pathlib import Path

SCRIPT = r'''async (page) => {
  const input=__INPUT__, checks=[], errors=[], responses=[];
  const canonical=v=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
  await page.setViewportSize({width:1920,height:1080});
  page.on('pageerror',e=>errors.push(e.message));
  let pending=[];
  page.on('response',r=>{
    if(!r.url().includes('/query_range')) return;
    pending.push((async()=>{
      const p=new URLSearchParams(r.request().postData()||new URL(r.url()).search);
      responses.push({status:r.status(),query:p.get('query'),step:p.get('step'),body:await r.json()});
    })());
  });
  for(const [scenario,end,seconds] of [['normal',input.end,300],['hour',input.end,3600],
      ...['zero','gap','stale'].map(c=>[c,input.case_ends[c],300])]){
    for(const key of [...new Set(input.targets.map(t=>t.key))]){
      const dashboard=input.targets.find(t=>t.key===key).dashboard;
      const versions=[];
      for(const version of ['baseline','candidate']){
        const target=input.targets.find(t=>t.version===version&&t.key===key);
        responses.length=0;pending=[];
        await page.goto(`${input.ui}/projects/${target.project}/dashboards/${dashboard}?start=${(end-seconds)*1000}&end=${end*1000}`);
        await page.getByText('固定窗口 · 自动刷新已暂停').waitFor();
        await page.locator('body').ariaSnapshot();
        const dashboardDocument=await (await page.request.get(`${input.ui}/api/v1/projects/${target.project}/dashboards/${dashboard}`)).json();
        const legends={};
        for(const title of target.titles){
          const panel=page.getByRole('region').filter({has:page.getByRole('heading',{name:title,exact:true})});
          await panel.scrollIntoViewIfNeeded();
          await panel.getByRole('figure').waitFor({timeout:30000});
          legends[title]=await panel.getByRole('listitem').evaluateAll(ns=>ns.map(n=>({name:n.innerText,color:n.querySelector('[style*="background-color"]')?.style.backgroundColor})));
          if(legends[title].some(x=>!x.color)) throw Error('Legend colour missing');
          if(scenario==='normal') await panel.screenshot({path:input.output+'/'+version+'-'+key.replaceAll('/','-')+'-'+target.titles.indexOf(title)+'.png'});
        }
        for(let i=0;i<100;i++){
          await Promise.all(pending);
          if(responses.length>=target.titles.length)break;
          await page.waitForTimeout(100);
        }
        await page.waitForTimeout(200);
        for(const title of target.titles){
          const panel=page.getByRole('region').filter({has:page.getByRole('heading',{name:title,exact:true})});
          legends[title]=await panel.getByRole('listitem').evaluateAll(ns=>ns.map(n=>({name:n.innerText,color:n.querySelector('[style*="background-color"]')?.style.backgroundColor})));
          if(scenario==='normal')await panel.screenshot({path:input.output+'/'+version+'-'+key.replaceAll('/','-')+'-'+target.titles.indexOf(title)+'.png'});
        }
        if(await page.evaluate(()=>document.documentElement.scrollWidth>1920))throw Error('Horizontal overflow');
        const data=responses.map(r=>({query:r.query,step:r.step,status:r.status,body:r.body})).sort((a,b)=>a.query.localeCompare(b.query));
        if(data.length!==target.titles.length || data.some(r=>r.status!==200))throw Error('Expected two successful CPU query responses: '+JSON.stringify(data));
        let filteredData=[];
        if(scenario==='normal' && (dashboardDocument.spec.variables||[]).some(v=>v.spec.name==='role')){
          const index=responses.length;
          await page.locator('body').ariaSnapshot();
          const filteredResponse=page.waitForResponse(r=>r.url().includes('/query_range')&&(r.request().postData()||r.url()).includes('prefill'));
          await page.getByRole('combobox',{name:'角色',exact:true}).click();
          await page.getByRole('option',{name:/^Prefill(?: \/.*)?$/}).click();
          await filteredResponse;
          await page.waitForTimeout(300);
          await Promise.all(pending);
          const filtered=responses.slice(index);
          const role=dashboardDocument.spec.variables.find(v=>v.spec.name==='role').spec.plugin.spec.values.find(v=>/^Prefill/.test(v.label)).value;
          const matches=new RegExp('^(?:'+role+')$');
          if(filtered.length!==target.titles.length || filtered.some(r=>r.body.data.result.some(s=>!matches.test(s.metric.node))))throw Error('Role filtering differs: '+JSON.stringify(filtered.map(r=>({status:r.status,nodes:r.body.data.result.map(s=>s.metric.node)}))));
          filteredData=filtered.map(r=>({query:r.query,step:r.step,status:r.status,body:r.body})).sort((a,b)=>a.query.localeCompare(b.query));
        }
        versions.push({legends,data:canonical(data),filteredData:canonical(filteredData)});
      }
      const passed=JSON.stringify(versions[0])===JSON.stringify(versions[1]);
      checks.push({scenario,key,dashboard,passed,before:versions[0],after:versions[1]});
    }
  }
  return {passed:checks.every(c=>c.passed)&&!errors.length,viewport:{width:1920,height:1080},
          scope:'all 18 affected panel copies; equality and interaction',checks,errors};
}'''


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cli',type=Path,required=True)
    parser.add_argument('--session',default='issue4-cpu')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    data=json.loads((args.evidence/'browser-ready.json').read_text())
    data['output']=str(args.output.resolve())
    script=args.output/'cpu-browser.js'
    script.write_text(SCRIPT.replace('__INPUT__',json.dumps(data,ensure_ascii=False)))
    result=subprocess.run([str(args.cli),'-s='+args.session,'run-code','--filename',str(script.resolve())],
        cwd=args.output,text=True,capture_output=True,timeout=300)
    (args.output/'cpu-browser.log').write_text(result.stdout+result.stderr)
    result.check_returncode()
    assert '### Result\n' in result.stdout, 'See cpu-browser.log'
    report=json.loads(result.stdout.split('### Result\n',1)[1].split('\n### ',1)[0])
    (args.evidence/'browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    assert report['passed'], 'CPU browser comparison failed'
    print(json.dumps({'passed':True,'checks':len(report['checks'])}))
