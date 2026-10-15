import RFB from './novnc/core/rfb.js';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const modifiers={ctrl:[0xffe3,'ControlLeft','Ctrl'],alt:[0xffe9,'AltLeft','Alt'],meta:[0xffeb,'MetaLeft','Windows']};
const keys={tab:[0xff09,'Tab'],esc:[0xff1b,'Escape']};

export class ConsoleView{
    constructor(container,{vm,api,onDetach=()=>{},RFBClass=RFB,createSocket=url=>new WebSocket(url)}={}){
        this.container=container;this.vm=vm;this.api=api;this.onDetach=onDetach;this.RFBClass=RFBClass;this.createSocket=createSocket;this.held=new Set();this.rfb=null;this.socket=null;this.connected=false;this.disposed=false;
        this.blur=()=>this.releaseModifiers();this.visibility=()=>{if(document.hidden)this.releaseModifiers();};this.fullscreen=()=>this.updateFullscreen();
        window.addEventListener('blur',this.blur);document.addEventListener('visibilitychange',this.visibility);document.addEventListener('fullscreenchange',this.fullscreen);
        const href='/client/console/'+encodeURIComponent(vm.id);
        container.innerHTML=`<section class="console-workspace"><div class="console-toolbar" role="toolbar" aria-label="Contrôles de la console"><div class="console-key-controls">${Object.entries(modifiers).map(([id,[,,name]])=>`<button type="button" data-console="modifier" data-key="${id}" aria-pressed="false" title="Maintenir ${name} jusqu’au prochain clic" disabled>${name}</button>`).join('')}<button type="button" data-console="key" data-key="tab" disabled>Tab</button><button type="button" data-console="key" data-key="esc" disabled>Esc</button><button type="button" data-console="cad" disabled>Ctrl + Alt + Suppr</button><button type="button" data-console="release" disabled>Relâcher les touches</button></div><div class="console-window-controls"><button type="button" data-console="fullscreen">Plein écran</button><a class="contract-link" data-console="tab" href="${esc(href)}" target="_blank" rel="noopener noreferrer">Nouvel onglet ↗</a><button type="button" data-console="window">Nouvelle fenêtre ↗</button></div></div><div class="console-meta"><span data-console-status role="status">Connexion…</span><span data-console-held aria-live="polite">Aucune touche maintenue</span></div><div class="console-screen" data-console-screen></div><p class="console-help">Ctrl, Alt et Windows restent maintenues jusqu’au prochain clic. Les touches sont relâchées en quittant la fenêtre ou la console.</p></section>`;
        this.workspace=container.querySelector('.console-workspace');this.screen=container.querySelector('[data-console-screen]');
        this.click=e=>this.handleClick(e);container.addEventListener('click',this.click);
    }
    setStatus(message){if(!this.disposed)this.container.querySelector('[data-console-status]').textContent=message;}
    setConnected(value){
        this.connected=value;
        for(const b of this.container.querySelectorAll('[data-console="modifier"],[data-console="key"],[data-console="cad"],[data-console="release"]'))b.disabled=!value;
        if(!value){this.held.clear();this.updateModifiers();}
    }
    updateModifiers(){
        for(const b of this.container.querySelectorAll('[data-console="modifier"]'))b.setAttribute('aria-pressed',String(this.held.has(b.dataset.key)));
        this.container.querySelector('[data-console-held]').textContent=this.held.size?'Touches maintenues : '+[...this.held].map(key=>modifiers[key][2]).join(' + '):'Aucune touche maintenue';
    }
    toggleModifier(name){
        if(!this.connected||!modifiers[name])return;
        const [symbol,code]=modifiers[name],down=!this.held.has(name);
        this.rfb.sendKey(symbol,code,down);if(down)this.held.add(name);else this.held.delete(name);
        this.updateModifiers();this.rfb.focus();
    }
    releaseModifiers(){
        for(const key of this.held){const [symbol,code]=modifiers[key];if(this.connected)this.rfb?.sendKey(symbol,code,false);}
        this.held.clear();if(!this.disposed)this.updateModifiers();
    }
    async handleClick(event){
        const b=event.target.closest('[data-console]');if(!b||!this.container.contains(b))return;
        const action=b.dataset.console;
        try{
            if(action==='modifier')this.toggleModifier(b.dataset.key);
            if(action==='key'&&this.connected){const [symbol,code]=keys[b.dataset.key];this.rfb.sendKey(symbol,code);this.rfb.focus();}
            if(action==='release'){this.releaseModifiers();this.rfb?.focus();}
            if(action==='cad'&&this.connected){const held=[...this.held];this.releaseModifiers();this.rfb.sendCtrlAltDel();for(const key of held)this.toggleModifier(key);this.rfb.focus();}
            if(action==='fullscreen'){
                if(document.fullscreenElement===this.workspace)await document.exitFullscreen();
                else if(this.workspace.requestFullscreen)await this.workspace.requestFullscreen();
                else this.setStatus('Le plein écran n’est pas disponible dans ce navigateur.');
            }
            if(action==='tab'){this.releaseModifiers();this.disconnect();this.onDetach();}
            if(action==='window'){
                const href='/client/console/'+encodeURIComponent(this.vm.id);
                window.open(href,'_blank','noopener,noreferrer,popup=yes,width=1280,height=900');
                this.releaseModifiers();this.disconnect();this.setStatus('Console ouverte dans une autre fenêtre. Si elle est bloquée, utilisez « Nouvel onglet ».');
            }
        }catch(error){this.setStatus('Action indisponible : '+error.message);}
    }
    updateFullscreen(){
        if(this.disposed)return;
        const active=document.fullscreenElement===this.workspace;
        this.container.querySelector('[data-console="fullscreen"]').textContent=active?'Quitter le plein écran':'Plein écran';
    }
    async connect(){
        this.setStatus('Connexion…');
        try{
            const data=await this.api('portal/vps/'+this.vm.id+'/console','POST',{});
            if(this.disposed)return;
            if(data.simulated){this.screen.innerHTML='<div class="console-demo">Console de démonstration<br><small>Les commandes clavier seront disponibles sur votre VPS réel.</small></div>';this.setStatus('Démonstration — aucun serveur réel connecté');return;}
            const socket=this.createSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/console');this.socket=socket;
            socket.addEventListener('open',()=>{
                if(this.disposed){socket.close();return;}
                socket.send(JSON.stringify({ticket:data.ticket}));
                this.rfb=new this.RFBClass(this.screen,socket,{credentials:{password:data.password}});this.rfb.scaleViewport=true;this.rfb.resizeSession=true;
                this.rfb.addEventListener('connect',()=>{if(this.disposed)return;this.setConnected(true);this.setStatus('Connecté');this.rfb.focus();});
                this.rfb.addEventListener('disconnect',()=>{if(this.disposed)return;this.setConnected(false);this.setStatus('Déconnecté');});
                this.rfb.addEventListener('securityfailure',()=>this.setStatus('La connexion à l’écran a été refusée. Rouvrez la console pour réessayer.'));
            });
            socket.addEventListener('error',()=>this.setStatus('Connexion à l’écran interrompue. Rouvrez la console pour réessayer.'));
        }catch(error){this.setStatus(error.message);}
    }
    disconnect(){this.releaseModifiers();this.rfb?.disconnect();this.socket?.close();this.setConnected(false);}
    destroy(){
        if(this.disposed)return;
        this.disconnect();if(document.fullscreenElement===this.workspace)document.exitFullscreen().catch(()=>{});
        window.removeEventListener('blur',this.blur);document.removeEventListener('visibilitychange',this.visibility);document.removeEventListener('fullscreenchange',this.fullscreen);this.container.removeEventListener('click',this.click);this.disposed=true;
    }
}
