// Headless test for this app only. Never run against a live-mode console.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const url = process.env.VWAN_TEST_URL || 'http://localhost:8786';
assert(['localhost','127.0.0.1'].includes(new URL(url).hostname),'Only a local development server is allowed');
const out = process.env.VWAN_TEST_OUTPUT || 'data/browser-review';
fs.mkdirSync(out,{recursive:true});
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  const context=await browser.newContext({viewport:{width:1480,height:1050},locale:'fr-FR'});
  const page=await context.newPage();const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',route=>new URL(route.request().url()).origin===new URL(url).origin?route.continue():route.abort());
  try{
    await page.goto(url);
    await page.locator('#login-form input[name=username]').fill('admin');
    await page.locator('#login-form input[name=password]').fill(process.env.VWAN_TEST_PASSWORD);
    await page.locator('#login-form button').click();
    await page.locator('#shell').waitFor({state:'visible'});
    const s=await page.evaluate(async()=>await (await fetch('/api/state')).json());
    assert.equal(s.demo,true,'Refuse to exercise deployment/purchase UI in live mode');
    await page.screenshot({path:path.join(out,'overview.png'),fullPage:true});
    for(const section of ['clients','ips','routers','models','purchases','jobs','settings']){
      await page.locator('nav a[data-page='+section+']').click();
      assert((await page.locator('#page-title').textContent()).length>0);
      await page.screenshot({path:path.join(out,section+'.png'),fullPage:true});
    }
    await page.locator('nav a[data-page=ips]').click();
    await page.locator('[data-action=use-ip]').first().click();
    await page.locator('#action-form textarea[name=public_key]').fill('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test');
    await page.locator('#action-form input[name=customer]').fill('Browser test — simulated');
    await page.screenshot({path:path.join(out,'new-client.png'),fullPage:true});
    await page.locator('#action-form button.primary').click();
    await page.waitForFunction(()=>!document.querySelector('#modal').open);
    await page.waitForFunction(async()=>{const s=await(await fetch('/api/state')).json();return s.deployments.some(d=>d.customer==='Browser test — simulated'&&d.status==='simulated');});
    await page.locator('nav a[data-page=purchases]').click();
    await page.locator('[data-action=new-quote]').click();
    await page.locator('#plan-code').fill('DEMO-ip');
    await page.locator('#action-form button.primary').click();
    await page.waitForFunction(()=>!document.querySelector('#modal').open);
    await page.waitForFunction(async()=>{const s=await(await fetch('/api/state')).json();return s.purchases.some(p=>p.state==='awaiting_approval');});
    await page.locator('#refresh').click();
    await page.getByRole('button',{name:'Examiner & confirmer'}).first().click();
    await page.screenshot({path:path.join(out,'approval.png'),fullPage:true});
    // Stop before confirming: integration tests cover the simulated approval API.
    await page.locator('#close-modal').click();
    await page.setViewportSize({width:390,height:844});
    await page.goto(url+'/#overview');
    await page.locator('#shell').waitFor({state:'visible'});
    await page.screenshot({path:path.join(out,'mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({pass:true,pages:8,simulated_client_deployed:true,quote_ready:true,approval_sent:false,console_errors:errors,screenshots:out}));
  }finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e.stack);process.exit(1)});
