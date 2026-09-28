// Actual perf.3 frontend + disposable VM fixtures, never a production timing claim.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict'), crypto = require('node:crypto');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(process.argv[2]);
const mode = process.argv[3] || 'merge';
assert(['merge','materialized'].includes(mode));
const changes = JSON.parse(fs.readFileSync(path.join(root,mode+'-changes.json')));
const fixture = JSON.parse(fs.readFileSync(path.join(root,'synthetic-fixtures.json')));
const synthetic = JSON.parse(fs.readFileSync(path.join(root,'merge-synthetic.json')));
(async()=>{
 const browser = await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const checks=[],errors=[],requests=[];
 try {
  const targets=[...new Set(changes.map(c=>c.project+'/'+c.dashboard))];
  for(const target of targets){
   const [project,dashboard]=target.split('/'), chosen=changes.filter(c=>c.project===project&&c.dashboard===dashboard), snapshots=[];
   for(const version of ['baseline','candidate']){
    const page=await browser.newPage({viewport:{width:1920,height:1080}});
    page.on('pageerror',e=>errors.push(e.message));
    page.on('response',r=>{if(r.url().includes('/query_range'))requests.push({version,target,status:r.status(),body:r.request().postData()})});
    const end=fixture.ends.normal*1000,start=end-300000,port=version==='baseline'?18548:18549;
    await page.goto(`http://127.0.0.1:${port}/projects/${project}/dashboards/${dashboard}?start=${start}&end=${end}`);
    await page.getByText('固定窗口 · 自动刷新已暂停').waitFor();
    const document=await (await page.request.get(`http://127.0.0.1:${port}/api/v1/projects/${project}/dashboards/${dashboard}`)).json();
    for(const change of chosen)assert.deepEqual(document.spec.panels[change.panel],change[version==='baseline'?'before':'after'],'Browser resource fingerprint');
    const panels={};
    for(const change of chosen){
     const title=change.before.spec.display.name;
     const panel=page.locator('[data-testid="panel"]').filter({has:page.getByRole('heading',{name:title,exact:true})});
     for(let attempt=0;attempt<20 && !(await panel.count());attempt++){
      await page.mouse.move(1200,800);await page.mouse.wheel(0,600);await page.waitForTimeout(150);
     }
     await panel.scrollIntoViewIfNeeded();
     await panel.locator('[role="listitem"]').first().waitFor({timeout:30000});
     panels[change.panel]=await panel.locator('[role="listitem"]').evaluateAll(nodes=>nodes.map(n=>({name:n.innerText,color:n.querySelector('[style*="background-color"]')?.style.backgroundColor})));
     assert(panels[change.panel].every(x=>x.color),'Legend swatches must be visible');
     await panel.screenshot({path:path.join(root,`${project}-${change.panel}-${version}.png`)});
    }
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=1920),'No horizontal overflow');
    // Exercise existing role filtering and fixed-window navigation on both sides.
    const role=page.getByRole('combobox',{name:'角色',exact:true});
    if(await role.count()){
     const count=requests.length; await role.click(); await page.getByRole('option',{name:/^Prefill(?: \/.*)?$/}).click();
     await page.waitForTimeout(500);
     assert(requests.slice(count).some(r=>(r.body||'').includes('prefill')),'Role filter must reach the query');
    }
    snapshots.push(panels); await page.close();
   }
   const match=JSON.stringify(snapshots[0])===JSON.stringify(snapshots[1]);
   checks.push({project,dashboard,passed:match,before:snapshots[0],after:snapshots[1]});
   console.log(target,'legend/colour parity:',match);
  }
  const passed=checks.every(c=>c.passed)&&errors.length===0&&requests.every(r=>r.status===200);
  fs.writeFileSync(path.join(root,mode+'-browser.json'),JSON.stringify({passed,candidate_sha256:synthetic.candidate_sha256,catalog_sha256:crypto.createHash('sha256').update(fs.readFileSync('monitoring/perses_acceleration_catalog.json')).digest('hex'),viewport:{width:1920,height:1080},source:'local unchanged perf.3 image with actual isolated-VM fixtures',checks,errors,requests},null,2));
  assert(passed,'Browser admission failed; see per-panel evidence');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
