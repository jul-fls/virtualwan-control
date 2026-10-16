const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const url=process.env.VWAN_TEST_URL||'http://localhost:8786',out=process.env.VWAN_TEST_OUTPUT||'data/admin-review';fs.mkdirSync(out,{recursive:true});
assert(['localhost','127.0.0.1'].includes(new URL(url).hostname));
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});const context=await browser.newContext({viewport:{width:1480,height:1080},locale:'fr-FR'}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
try{
for(let i=0;i<40;i++){try{if((await fetch(url+'/health')).ok)break;}catch{}await new Promise(r=>setTimeout(r,250));}
await page.goto(url);await page.locator('[name=password]').fill(process.env.VWAN_TEST_PASSWORD);await page.locator('#login-form button').click();await page.locator('#shell').waitFor({state:'visible'});
const state=await page.evaluate(()=>api('state'));assert(state.demo);const vm=state.deployments[0],csrf=(await (await context.request.get(url+'/api/session')).json()).csrf;
async function req(endpoint,method,body){const r=await context.request.fetch(url+'/api/'+endpoint,{method,headers:{'X-CSRF-Token':csrf},data:body});assert(r.ok(),await r.text());return r.json();}
const email='admin-browser-'+Date.now()+'@example.net',account=await req('accounts','POST',{name:'Client Admin Browser',email,password:'CustomerBrowserPassword2026'});
await req('deployments/'+vm.id+'/account','PUT',{account_id:account.id});
await req('admin/vps/'+vm.id+'/lock','PUT',{locked:false,confirmation:'CONFIRMER'});
const reset=await req('admin/vps/'+vm.id+'/network','POST',{blocked:false,confirmation:'CONFIRMER'});for(let i=0;i<50;i++){const r=await req('admin/operations/'+reset.operation_id,'GET');if(r.state!=='running')break;await new Promise(r=>setTimeout(r,100));}
const model=await req('models','POST',{name:'Petit test','range':'Essentiel','cores':1,'memory_mb':1024,'disk_gb':1,'template':9001});
await page.goto(url+'/admin/vps/'+vm.id);await page.locator('[data-action=tab][data-id=administration]').click();await page.locator('#admin-hardware').filter({hasText:'Ressources réelles'}).waitFor();
await page.locator('#admin-resize [name=model_id]').selectOption(model.id);page.once('dialog',d=>d.accept());await page.locator('#admin-resize .primary').click();await page.locator('#admin-progress').filter({hasText:'Opération terminée'}).waitFor();
let adminstate=await (await context.request.get(url+'/api/admin/state')).json();const changed=adminstate.vps.find(v=>v.id===vm.id);assert.equal(changed.cores,1);assert(changed.disk_gb>=1);
await page.locator('#admin-lock-reason').fill('Incident de test interne');page.once('dialog',d=>d.accept());await page.locator('[data-action=admin-lock]').click();await page.locator('[data-action=admin-lock]').filter({hasText:'Déverrouiller'}).waitFor();
const clientContext=await browser.newContext(),clientPage=await clientContext.newPage();clientPage.on('pageerror',e=>errors.push(e.message));await clientPage.goto(url+'/client');await clientPage.locator('[name=email]').fill(email);await clientPage.locator('[name=password]').fill('CustomerBrowserPassword2026');await clientPage.locator('#client-login-form .primary').click();await clientPage.locator('[data-action=manage]').click();await clientPage.getByRole('heading',{name:'Gestion verrouillée',exact:true}).waitFor();assert.equal(await clientPage.locator('[data-action=power]').count(),0);
assert.equal((await clientContext.request.get(url+'/api/admin/state')).status(),401);
page.once('dialog',d=>d.accept());await page.locator('[data-action=admin-network]').click();await page.locator('[data-action=admin-network]').filter({hasText:'Rétablir'}).waitFor();
await page.locator('[data-action=admin-notes]').click();await page.locator('#admin-progress').filter({hasText:'Opération terminée'}).waitFor();
await page.screenshot({path:path.join(out,'admin-management.png'),fullPage:true});
await page.locator('[data-action=tab][data-id=tools]').click();await page.locator('[data-action=console]').click();await page.locator('.console-demo').waitFor();
const [detached]=await Promise.all([context.waitForEvent('page'),page.locator('[data-console=tab]').click()]);await detached.locator('.console-demo').waitFor();assert(new URL(detached.url()).pathname.startsWith('/admin/console/'));await detached.close();
await page.locator('[data-action=tab][data-id=administration]').click();page.once('dialog',d=>d.accept());await page.locator('[data-action=admin-network]').click();await page.locator('[data-action=admin-network]').filter({hasText:'Couper'}).waitFor();
page.once('dialog',d=>d.accept());await page.locator('[data-action=admin-lock]').click();await page.locator('[data-action=admin-lock]').filter({hasText:'Verrouiller la gestion'}).waitFor();
await clientPage.reload();await clientPage.locator('[data-action=manage]').click();await clientPage.locator('[data-action=power][data-id=start]').waitFor();
await clientContext.close();assert.deepEqual(errors,[]);console.log(JSON.stringify({pass:true,admin_workspace:true,resize:true,lock:true,network_suspend_restore:true,admin_console_while_locked:true,admin_detached_console:true,client_unlock:true,js_errors:errors}));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
