const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const url=process.env.VWAN_TEST_URL||'http://localhost:8786',out=process.env.VWAN_TEST_OUTPUT||'data/audit-review';
assert(['localhost','127.0.0.1'].includes(new URL(url).hostname));fs.mkdirSync(out,{recursive:true});
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE}),context=await browser.newContext({viewport:{width:1440,height:1050},locale:'fr-FR'}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
try{
for(let i=0;i<40;i++){try{if((await fetch(url+'/health')).ok)break;}catch{}await new Promise(r=>setTimeout(r,250));}
await page.goto(url);await page.locator('[name=password]').fill(process.env.VWAN_TEST_PASSWORD);await page.locator('#login-form button').click();await page.locator('#shell').waitFor({state:'visible'});
const state=await (await context.request.get(url+'/api/state')).json();assert(state.demo);const vm=state.deployments[0],fake='a'.repeat(32);
await page.route('**/api/admin/state',async route=>{const response=await route.fetch(),body=await response.json();body.vps.find(v=>v.id===vm.id).admin_operation={id:fake,message:'Suivi synthétique'};await route.fulfill({response,json:body});});
await page.route('**/api/admin/operations/'+fake,route=>route.fulfill({status:503,json:{error:'Suivi temporairement indisponible'}}));
await page.goto(url+'/admin/vps/'+vm.id);await page.locator('[data-action=tab][data-id=administration]').click();await page.locator('#toast').filter({hasText:'Suivi temporairement indisponible'}).waitFor();
assert.deepEqual(errors,[]);await page.screenshot({path:path.join(out,'admin-poll-recovery.png'),fullPage:true});
await page.unroute('**/api/admin/state');await page.unroute('**/api/admin/operations/'+fake);await page.locator('#client-refresh').click();await page.locator('[data-action=admin-notes]').click();await page.locator('#admin-progress').filter({hasText:'Opération terminée'}).waitFor();
await page.route('**/api/logout',route=>route.fulfill({status:503,json:{error:'Déconnexion temporairement indisponible'}}));await page.locator('#client-logout').click();await page.locator('#toast').filter({hasText:'Déconnexion temporairement indisponible'}).waitFor();assert.deepEqual(errors,[]);await page.unroute('**/api/logout');
await page.goto(url);await page.locator('#shell').waitFor({state:'visible'});await page.route('**/api/logout',route=>route.fulfill({status:503,json:{error:'Déconnexion temporairement indisponible'}}));await page.locator('#logout').click();await page.locator('#toast').filter({hasText:'Déconnexion temporairement indisponible'}).waitFor();assert.deepEqual(errors,[]);
console.log(JSON.stringify({pass:true,admin_background_poll_error_handled:true,notes_retry:true,admin_workspace_logout_error_handled:true,main_logout_error_handled:true,js_errors:errors}));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
