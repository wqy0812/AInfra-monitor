// Local-only 1080p presentation check with synthetic query responses.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||path.resolve(__dirname,'../../code-eval/frontend/node_modules/playwright'));
(async()=>{
 const evidence=path.resolve(process.argv[2]),candidate=JSON.parse(fs.readFileSync(path.join(evidence,'candidate.json'),'utf8'));
 const base=process.env.PREVIEW_URL||'http://127.0.0.1:19541';
 assert(['127.0.0.1','localhost'].includes(new URL(base).hostname),'Only isolated local preview allowed');
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1920,height:1080}}),errors=[],checks=[],queries=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/proxy/**',async route=>{
  const req=route.request(),u=new URL(req.url()),p=new URLSearchParams(req.postData()||u.search);
  queries.push(p.get('query')||'');
  if(u.pathname.endsWith('/query_range')){
   const parse=v=>Number.isFinite(Number(v))?Number(v):Date.parse(v)/1000;
   const end=parse(p.get('end'))||Date.now()/1000,start=parse(p.get('start'))||end-3600;
   const values=Array.from({length:61},(_,i)=>[start+(end-start)*i/60,String(10+5*Math.sin(i/6))]);
   return route.fulfill({json:{status:'success',data:{resultType:'matrix',result:[{metric:{node:'a3-1',id:'0',engine:'0',backend:'preview',perses_quantile:'P95',path:'nodes.prefill.requests'},values}]}}});
  }
  return route.fulfill({json:{status:'success',data:[]}});
 });
 try{
  for(const d of candidate.dashboards){
   const project=d.metadata.project,name=d.metadata.name;
   await page.goto(`${base}/projects/${project}/dashboards/${name}`);
   const first=d.spec.layouts[0].spec.items[0].content.$ref.split('/').pop(),title=d.spec.panels[first].spec.display.name;
   await page.getByRole('heading',{name:title,exact:true}).waitFor({timeout:30000});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=1920),`${project}/${name} overflow`);
   const role=page.getByRole('combobox',{name:'角色',exact:true});
   if(await role.count()){
    const before=queries.length;
    await role.click();
    const response=page.waitForResponse(r=>r.url().includes('/query_range'),{timeout:10000});
    await page.getByRole('option',{name:/^Prefill(?: \/.*)?$/}).click();
    await page.waitForFunction(()=>document.querySelector('input[role="combobox"]')?.value.startsWith('Prefill'));
    await response;
    assert(queries.slice(before).some(q=>q.includes('prefill')),`${project}/${name} role did not reach query`);
   }
   if(name==='a3-cache'){
    const moon=page.getByRole('heading',{name:'Mooncake Master',exact:true});await moon.scrollIntoViewIfNeeded();
    await page.getByRole('heading',{name:'Mooncake 内存容量（GiB）',exact:true}).waitFor();
   }
   if(['backend-prefill','backend-decode','accelerator-resources','a3-cache','backend-performance'].includes(name)){
    await page.screenshot({path:path.join(evidence,`${project}-${name}-1080p.png`)});
   }
   checks.push({project,dashboard:name,passed:true});console.log('checked',project,name);
  }
  assert.deepEqual(errors,[]);
  const report={passed:true,viewport:{width:1920,height:1080},source:'isolated-local-perses-with-synthetic-queries',checks,errors};
  fs.writeFileSync(path.join(evidence,'browser-checks.json'),JSON.stringify(report,null,2));
  console.log('PASS',checks.length,'dashboards');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
