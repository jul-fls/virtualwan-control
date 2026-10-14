// Exercise recovery only against a seeded localhost demonstration.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const url=process.env.VWAN_TEST_URL||'http://localhost:8787';
assert(['localhost','127.0.0.1'].includes(new URL(url).hostname));
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
 const context=await browser.newContext();const page=await context.newPage();const errors=[];
 async function waitState(predicate){for(let i=0;i<150;i++){const s=await page.evaluate(async()=>await(await fetch('/api/state')).json());if(predicate(s))return s;await page.waitForTimeout(100);}throw Error('State transition timeout');}
 page.on('pageerror',e=>errors.push(e.message));
 await context.route('**/*',route=>new URL(route.request().url()).origin===new URL(url).origin?route.continue():route.abort());
 try{
  await page.goto(url);await page.locator('#login-form input[name=username]').fill('admin');
  await page.locator('#login-form input[name=password]').fill(process.env.VWAN_TEST_PASSWORD);await page.locator('#login-form button').click();
  await page.locator('#shell').waitFor({state:'visible'});
  const state=await page.evaluate(async()=>await(await fetch('/api/state')).json());assert.equal(state.demo,true);
  const client=state.deployments.find(d=>d.lifecycle_job?.state==='failed');assert(client);assert.equal(client.status,'retirement_failed');
  const job=client.retirement_job;
  await page.locator('nav a[data-page=purchases]').click();
  await page.locator('[data-action=events][data-id="'+job+'"]').click();
  await page.locator('#modal [data-action=cancel-job]').click();
  await page.locator('#action-form input[name=confirmation]').fill('ANNULER '+job.slice(0,8));
  await page.locator('#action-form button.primary').click();
  await waitState(s=>s.deployments.some(d=>d.id===client.id&&d.status==='retirement_cancelled'));
  await page.locator('#refresh').click();
  await page.locator('[data-action=retry][data-id="'+job+'"]').click();
  await page.locator('#action-form input[name=confirmation]').fill('REPRENDRE '+job.slice(0,8));await page.locator('#action-form button.primary').click();
  await waitState(s=>!s.deployments.some(d=>d.id===client.id)&&s.jobs.some(j=>j.id===job&&j.state==='succeeded'));
  const after=await page.evaluate(async()=>await(await fetch('/api/state')).json());
  assert.equal(after.jobs.find(j=>j.id===job).state,'succeeded');assert.equal(after.ips.find(i=>i.ip===client.ip).state,'available');assert.deepEqual(errors,[]);
  console.log(JSON.stringify({pass:true,old_stuck_state_repaired:true,cancel_from_journal:true,retry_same_job:true,console_errors:errors}));
 }finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e.stack);process.exit(1)});
