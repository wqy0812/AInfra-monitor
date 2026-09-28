// Capture the actual initial viewport request order; this is not a timing test.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(process.argv[2]);
const changes=JSON.parse(fs.readFileSync(path.join(root,'merge-changes.json')));
const hoursList=process.argv[3]?JSON.parse(process.argv[3]):[1,24];
assert(hoursList.length===2 && hoursList.every(h=>Number.isFinite(h)&&h>0));
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const cohorts=[],errors=[];
 try{
  const end=Math.floor(Date.now()/3600000)*3600000-3600000;
  for(const target of [...new Set(changes.map(c=>c.project+'/'+c.dashboard))]){
   const [project,dashboard]=target.split('/');
   for(const hours of hoursList)for(const version of ['before','after']){
    const page=await browser.newPage({viewport:{width:1920,height:1080}}),requests=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/proxy/**',async route=>{
     const r=route.request(),u=new URL(r.url()),p=new URLSearchParams(r.postData()||u.search);
     if(u.pathname.endsWith('/query_range'))requests.push(Object.fromEntries(p));
     await route.fulfill({json:{status:'success',data:{resultType:'matrix',result:[]}}});
    });
    const port=version==='before'?18548:18549;
    await page.goto(`http://127.0.0.1:${port}/projects/${project}/dashboards/${dashboard}?start=${end-hours*3600000}&end=${end}`);
    await page.getByText('固定窗口 · 自动刷新已暂停').waitFor();
    await page.waitForTimeout(1000);
    assert(requests.length>0);assert(requests.every(r=>Number(r.step)>0));
    cohorts.push({project,dashboard,hours,version,requests});
    console.log(target,hours,version,requests.length);
    await page.close();
   }
  }
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(root,'browser-cohorts.json'),JSON.stringify({passed:true,viewport:{width:1920,height:1080},syntheticResponses:true,cohorts},null,2));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
