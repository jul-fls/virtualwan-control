const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const url=process.env.VWAN_TEST_URL||'http://localhost:8786';
assert(['localhost','127.0.0.1'].includes(new URL(url).hostname));
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});try{
const page=await browser.newPage();await page.route('**/*',r=>new URL(r.request().url()).origin===new URL(url).origin?r.continue():r.abort());await page.goto(url+'/client');
await page.evaluate(async()=>{
const {ConsoleView}=await import('/static/console-view.js');window.sent=[];
class FakeSocket extends EventTarget{constructor(){super();setTimeout(()=>this.dispatchEvent(new Event('open')),0);}send(){}close(){}}
class FakeRFB extends EventTarget{constructor(){super();setTimeout(()=>this.dispatchEvent(new Event('connect')),0);}sendKey(...args){sent.push(args);}sendCtrlAltDel(){sent.push(['cad']);}focus(){}disconnect(){}}
const host=document.createElement('div');host.id='console-test';document.body.replaceChildren(host);
window.testConsole=new ConsoleView(host,{vm:{id:'synthetic'},api:async()=>({ticket:'synthetic',password:'synthetic'}),RFBClass:FakeRFB,createSocket:()=>new FakeSocket()});await testConsole.connect();
});
const button=key=>page.locator('#console-test [data-console=modifier][data-key='+key+']');await button('ctrl').waitFor();await page.waitForFunction(()=>testConsole.connected);
await button('ctrl').click();assert.equal(await button('ctrl').getAttribute('aria-pressed'),'true');
await page.locator('[data-console=key][data-key=tab]').click();await page.locator('[data-console=key][data-key=esc]').click();await button('ctrl').click();
await button('alt').click();await button('meta').click();assert.equal(await button('alt').getAttribute('aria-pressed'),'true');assert.equal(await button('meta').getAttribute('aria-pressed'),'true');
await page.locator('[data-console=cad]').click();
assert.deepEqual(await page.evaluate(()=>sent),[[65507,'ControlLeft',true],[65289,'Tab'],[65307,'Escape'],[65507,'ControlLeft',false],[65513,'AltLeft',true],[65515,'MetaLeft',true],[65513,'AltLeft',false],[65515,'MetaLeft',false],['cad'],[65513,'AltLeft',true],[65515,'MetaLeft',true]]);
await page.evaluate(()=>window.dispatchEvent(new Event('blur')));assert.equal(await button('alt').getAttribute('aria-pressed'),'false');assert.equal(await button('meta').getAttribute('aria-pressed'),'false');
await button('ctrl').click();await page.locator('[data-console=release]').click();assert.equal(await button('ctrl').getAttribute('aria-pressed'),'false');
await page.locator('[data-console=fullscreen]').click();await page.waitForFunction(()=>!!document.fullscreenElement);await page.locator('[data-console=fullscreen]').click();await page.waitForFunction(()=>!document.fullscreenElement);
await button('ctrl').click();await page.evaluate(()=>testConsole.destroy());assert.deepEqual(await page.evaluate(()=>sent.at(-1)),[65507,'ControlLeft',false]);
console.log(JSON.stringify({pass:true,sticky_modifiers:true,tab_escape:true,ctrl_alt_delete:true,release_on_blur:true,release_on_destroy:true,fullscreen:true}));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
