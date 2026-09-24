// Local candidate only. Synthetic datasource responses; no production traffic.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const base = process.env.CANDIDATE_URL || 'http://127.0.0.1:18544';
assert(new URL(base).hostname === '127.0.0.1', 'Only a loopback candidate is allowed');
const output = process.env.BROWSER_EVIDENCE;
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try {
  const context=await browser.newContext({viewport:{width:1920,height:1080}});
  const page=await context.newPage();const errors=[];const queries=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/proxy/**',async route=>{
   const url=new URL(route.request().url());
   if(url.pathname.includes('/query')) {
    queries.push({url:url.toString(),at:Date.now()});
    await route.fulfill({json:{status:'success',data:{resultType:url.pathname.endsWith('query_range')?'matrix':'vector',result:[]}}});
   } else await route.fulfill({json:{status:'success',data:[]}});
  });
  const start=Date.now()-7200000,end=start+3600000;
  await page.goto(`${base}/projects/dcu-monitoring/dashboards/backend-performance?start=${start}&end=${end}&refresh=5s`);
  await page.getByText('固定窗口 · 自动刷新已暂停').waitFor();
  await page.waitForTimeout(2000);
  const initial=queries.length;assert(initial>0,'Expected initial chart queries');
  await page.waitForTimeout(11000);assert.equal(queries.length,initial,'Fixed window must not poll');
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.waitForTimeout(1500);assert.equal(queries.length,initial*2,'Manual fixed refresh must run once');
  // Navigate through actual dashboard links. This also tests persistence after
  // a full document load, not just a component rerender.
  await page.goto(`${base}/projects/a3-monitoring`);
  await page.locator('a[href="/projects/a3-monitoring/dashboards/backend-performance"]').first().click();
  await page.waitForFunction(({start,end})=>{const q=new URLSearchParams(location.search);return q.get('start')===String(start)&&q.get('end')===String(end)},{start,end});
  await page.getByText('固定窗口 · 自动刷新已暂停').waitFor();
  await page.waitForTimeout(2500);
  if(output)await page.screenshot({path:output+'.png'});
  await page.goto(`${base}/projects/dcu-monitoring/dashboards/backend-performance?start=1h&refresh=5s`);
  await page.waitForTimeout(2500);
  const relative=queries.length;
  await page.waitForTimeout(5500);assert(queries.length>relative,'Relative window must poll');
  // Headless Chrome does not expose ordinary OS tab visibility transitions.
  // Dispatch the same DOM event after overriding its visibility getter.
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))});
  await page.waitForTimeout(1500);const hidden=queries.length;
  await page.waitForTimeout(11000);assert.equal(queries.length,hidden,'Hidden window must not poll');
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))});
  await page.waitForTimeout(1500);const resumed=queries.length;assert(resumed>hidden,'Visible relative window must refresh');
  assert.equal(resumed-hidden,initial,'Resume must issue exactly one query group');
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForTimeout(500);assert.equal(queries.length,resumed,'Duplicate visibility event must not refresh');
  assert.deepEqual(errors,[],'No browser runtime errors');
  const result={passed:true,environment:'local-candidate',image:process.env.CANDIDATE_IMAGE,syntheticDatasource:true,visibilityMethod:'DOM event with overridden document.hidden',viewport:{width:1920,height:1080},fixedInitialQueries:initial,relativeQueries:queries.length-relative,resumeQueries:resumed-hidden,queries,errors};
  if(output)fs.writeFileSync(output+'.json',JSON.stringify(result,null,2));
  console.log(JSON.stringify({passed:true,fixedInitialQueries:initial,resumeQueries:resumed-hidden}));
 } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
