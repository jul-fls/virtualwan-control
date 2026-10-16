const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const url=process.env.VWAN_TEST_URL||'http://localhost:8786',out=process.env.VWAN_TEST_OUTPUT||'data/reinstall-review';
assert(['localhost','127.0.0.1'].includes(new URL(url).hostname));fs.mkdirSync(out,{recursive:true});
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE}),admin=await browser.newContext({viewport:{width:1440,height:1050},locale:'fr-FR'}),page=await admin.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
try{
for(let i=0;i<40;i++){try{if((await fetch(url+'/health')).ok)break;}catch{}await new Promise(r=>setTimeout(r,250));}
await page.goto(url);await page.locator('[name=password]').fill(process.env.VWAN_TEST_PASSWORD);await page.locator('#login-form button').click();await page.locator('#shell').waitFor({state:'visible'});
const session=await (await admin.request.get(url+'/api/session')).json(),state=await (await admin.request.get(url+'/api/state')).json();assert(state.demo);const vm=state.deployments[0];
async function req(endpoint,method,body){const r=await admin.request.fetch(url+'/api/'+endpoint,{method,headers:{'X-CSRF-Token':session.csrf},data:body});assert(r.ok(),await r.text());return r.json();}
const email='reinstall-browser-'+Date.now()+'@example.net',account=await req('accounts','POST',{name:'Fresh install client',email,password:'CustomerBrowserPassword2026'});await req('deployments/'+vm.id+'/account','PUT',{account_id:account.id});
const client=await browser.newContext({viewport:{width:1440,height:1050},locale:'fr-FR'}),cp=await client.newPage();cp.on('pageerror',e=>errors.push(e.message));await cp.goto(url+'/client');await cp.locator('[name=email]').fill(email);await cp.locator('[name=password]').fill('CustomerBrowserPassword2026');await cp.locator('#client-login-form .primary').click();await cp.locator('[data-action=manage]').click();
for(const [p,role] of [[cp,'client'],[page,'admin']]){
 if(role==='admin')await p.goto(url+'/admin/vps/'+vm.id);
 await p.locator('[data-action=tab][data-id=tools]').click();await p.getByRole('heading',{name:'Réinstallation complète',exact:true}).waitFor();await p.locator('[data-action=reinstall]').click();
 await p.locator('#client-form [name=ssh_auth]').selectOption('password');await p.locator('#client-form [name=password]').fill('FreshGuestPassword2026');await p.locator('#client-form [name=hostname]').fill(vm.fqdn);await p.locator('#client-form [name=confirmation]').fill('wrong');await p.locator('#client-form [name=erase_system_disk]').check();await p.locator('#client-form .primary').click();await p.locator('#client-form-error').filter({hasText:'Confirmer'}).waitFor();
 await p.locator('#client-form [name=confirmation]').fill('REINSTALLER');await p.screenshot({path:path.join(out,role+'-reinstall-confirmation.png'),fullPage:true});await p.locator('#client-form .primary').click();await p.waitForFunction(()=>!document.querySelector('#client-modal').open);await p.locator('#reinstall-progress').filter({hasText:'Système neuf installé'}).waitFor();
 const r=await (await (role==='admin'?admin:client).request.get(url+'/api/'+(role==='admin'?'admin':'portal')+'/state')).json(),v=r.vps.find(v=>v.id===vm.id);assert.equal(v.ip,vm.ip);assert.equal(v.fqdn,vm.fqdn);assert(!v.reinstall_pending);assert(r.operations.some(o=>o.action==='reinstall'&&o.state==='succeeded'));
 await p.screenshot({path:path.join(out,role+'-reinstall-complete.png'),fullPage:true});
}
await client.close();assert.deepEqual(errors,[]);console.log(JSON.stringify({pass:true,client_and_admin_fresh_install:true,explicit_confirmation:true,invalid_confirmation_stays_in_form:true,identity_preserved:true,js_errors:errors}));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
