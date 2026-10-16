import {ConsoleView} from './console-view.js';
let view,csrf='';const admin=location.pathname.startsWith('/admin/');
async function api(path,method='GET',body){if(admin)path=path.replace(/^portal\//,'admin/');const r=await fetch('/api/'+path,{method,headers:{'Content-Type':'application/json',...(method!=='GET'?{'X-CSRF-Token':csrf}:{})},body:body===undefined?undefined:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(r.status===401?'Votre session a expiré. Connectez-vous à votre espace client puis rechargez cette console.':data.error||'Console indisponible.');return data;}
async function open(){
    view?.destroy();const container=document.querySelector('#standalone-console');
    try{
        const state=await api('portal/state');csrf=state.csrf;
        const id=decodeURIComponent(location.pathname.split('/').filter(Boolean).at(-1)),vm=state.vps.find(v=>v.id===id);
        if(!vm)throw Error('Ce VPS n’est pas accessible avec votre compte.');
        document.querySelector('#console-vm-title').textContent=vm.display_name||vm.fqdn;document.title='Console · '+(vm.display_name||vm.fqdn);
        view=new ConsoleView(container,{vm,api,admin});await view.connect();
    }catch(error){container.textContent=error.message;}
}
document.querySelector('#console-reconnect').onclick=open;window.addEventListener('pagehide',()=>view?.destroy());open();
