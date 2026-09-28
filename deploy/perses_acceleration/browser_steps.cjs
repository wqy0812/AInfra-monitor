const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(process.argv[2]);
const catalog = JSON.parse(fs.readFileSync(path.resolve('monitoring/perses_acceleration_catalog.json')));
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const rows = [], errors = [];
  try {
    const page = await browser.newPage({viewport:{width:1920,height:1080}});
    page.on('pageerror', e => errors.push(e.message));
    let current;
    await page.route('**/proxy/**', async route => {
      const request = route.request(), url = new URL(request.url());
      const params = new URLSearchParams(request.postData() || url.search);
      if (url.pathname.endsWith('/query_range')) rows.push({...current, step:params.get('step'), query:params.get('query')});
      await route.fulfill({json:{status:'success',data:{resultType:'matrix',result:[]}}});
    });
    const targets = [...new Set(catalog.panels.map(p => p.project + '/' + p.dashboard))];
    const end = Math.floor(Date.now() / 3600000) * 3600000 - 3600000;
    for (const target of targets) for (const hours of [1,6,24,168,720]) {
      const [project,dashboard] = target.split('/'); current = {project,dashboard,hours};
      const first = rows.length;
      await page.goto(`http://127.0.0.1:18549/projects/${project}/dashboards/${dashboard}?start=${end-hours*3600000}&end=${end}`);
      await page.getByText('固定窗口 · 自动刷新已暂停').waitFor();
      await page.waitForTimeout(700);
      assert(rows.length > first, target + ' must make range queries');
    }
    assert.deepEqual(errors, []);
    const steps = [...new Set([5,15,60,...rows.map(r=>Number(r.step))])].sort((a,b)=>a-b);
    fs.writeFileSync(path.join(root,'browser-steps.json'),JSON.stringify({passed:true,viewport:{width:1920,height:1080},syntheticResponses:true,steps,rows,errors},null,2));
    console.log(JSON.stringify({passed:true,steps,pages:targets.length*5,requests:rows.length}));
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1});
