const assert=require('node:assert/strict'),fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_CORE||'playwright-core');
const url=process.env.VWAN_TEST_URL||'http://localhost:8786';
assert(['localhost','127.0.0.1'].includes(new URL(url).hostname));
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});try{
const page=await browser.newPage({viewport:{width:1480,height:1050}});const errors=[];page.on('pageerror',e=>{errors.push(e.message);console.error('PAGE',e.message);});await page.route('**/*',r=>new URL(r.request().url()).origin===new URL(url).origin?r.continue():r.abort());await page.goto(url+'/client');
await page.evaluate(async()=>{
const {ConsoleView}=await import('/static/console-view.js');
class RfbSocket extends EventTarget{
constructor(){super();Object.assign(this,{readyState:0,binaryType:'arraybuffer',bufferedAmount:0,extensions:'',protocol:'',onopen:null,onmessage:null,onerror:null,onclose:null,stage:0,buffer:[]});setTimeout(()=>{this.readyState=1;const handler=this.onopen;this.dispatchEvent(new Event('open'));handler?.(new Event('open'));setTimeout(()=>this.reply(new TextEncoder().encode('RFB 003.008\n')),30);},0);}
reply(bytes){setTimeout(()=>this.onmessage?.({data:new Uint8Array(bytes).buffer}),0);}
send(data){if(typeof data==='string')return;this.buffer.push(...new Uint8Array(data));this.consume();}
consume(){while(this.buffer.length){const b=this.buffer;if(this.stage===0){if(b.length<12)return;b.splice(0,12);this.stage=1;this.reply([1,1]);}else if(this.stage===1){b.shift();this.stage=2;this.reply([0,0,0,0]);}else if(this.stage===2){b.shift();this.stage=3;this.reply([4,0,3,0,32,24,0,1,0,255,0,255,0,255,16,8,0,0,0,0,0,0,0,4,84,101,115,116]);}else{const type=b[0];let length;if(type===0)length=20;else if(type===2){if(b.length<4)return;length=4+4*((b[2]<<8)|b[3]);}else if(type===3)length=10;else if(type===4)length=8;else if(type===5)length=6;else throw Error('Unexpected RFB message '+type);if(b.length<length)return;b.splice(0,length);if(type===3&&!this.sentFrame){this.sentFrame=true;const frame=[0,0,0,1,0,0,0,0,0,16,0,16,0,0,0,0];for(let i=0;i<256;i++)frame.push(255,0,0,0);this.reply(frame);}}}}
close(){this.readyState=3;this.onclose?.({code:1000});}
}
window.openRenderConsole=async()=>{window.view?.destroy();const dialog=document.querySelector('#client-modal');document.querySelector('#client-modal-body').innerHTML='<div id="live-console"></div>';if(!dialog.open)dialog.showModal();window.view=new ConsoleView(document.querySelector('#live-console'),{vm:{id:'render-test'},api:async()=>({ticket:'synthetic',password:'synthetic'}),createSocket:()=>new RfbSocket()});await view.connect();};await openRenderConsole();
});
try{await page.waitForFunction(()=>view.connected,null,{timeout:10000});}catch(e){console.log(await page.evaluate(()=>({status:document.querySelector('[data-console-status]').textContent,state:view.rfb?._rfbConnectionState,stage:view.socket.stage,buffer:view.socket.buffer})));throw e;}await page.waitForFunction(()=>{const c=document.querySelector('#live-console canvas');return c&&c.getContext('2d').getImageData(5,5,1,1).data[0]===255;},null,{timeout:10000});
const result=await page.evaluate(()=>{const c=document.querySelector('#live-console canvas'),s=document.querySelector('[data-console-screen]');return {canvas:c.getBoundingClientRect().toJSON(),screen:s.getBoundingClientRect().toJSON(),dialog:document.querySelector('#client-modal').getBoundingClientRect().toJSON(),pixel:[...c.getContext('2d').getImageData(5,5,1,1).data]};});
console.log(JSON.stringify({result,errors}));assert(result.canvas.width>200);assert(result.canvas.height>100);assert.deepEqual(errors,[]);
await page.locator('[data-console=fullscreen]').click();await page.waitForFunction(()=>!!document.fullscreenElement);await page.waitForFunction(()=>document.querySelector('#live-console canvas').getBoundingClientRect().height>300);await page.locator('[data-console=fullscreen]').click();await page.waitForFunction(()=>!document.fullscreenElement);
await page.setViewportSize({width:390,height:844});await page.waitForFunction(()=>document.querySelector('#live-console canvas').getBoundingClientRect().width>100);
await page.setViewportSize({width:1480,height:1050});await page.evaluate(()=>{view.destroy();document.querySelector('#client-modal').close();});await page.evaluate(()=>openRenderConsole());await page.waitForFunction(()=>view.connected&&document.querySelector('#live-console canvas').getBoundingClientRect().width>200);
assert.deepEqual(errors,[]);console.log('PASS: actual noVNC framebuffer visible inside dialog, fullscreen, mobile resize, close/reopen');
if(process.env.VWAN_TEST_SCREENSHOT)await page.screenshot({path:process.env.VWAN_TEST_SCREENSHOT,fullPage:true});
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
