// Private per-VPS media. No shared ISO picker or client-controlled volume IDs.
export function mediaCard(){return `<article class="card"><h3>Lecteur CD/DVD privé</h3><p>Un seul ISO par VPS, 10 Go maximum. L’insertion et l’éjection ne redémarrent pas le serveur et ne modifient pas son disque. Après installation, éjectez l’ISO pour revenir au démarrage sur disque.</p><div id="iso-state" role="status">Chargement…</div><label>Image ISO<input id="client-iso-file" type="file" accept=".iso,application/x-iso9660-image"></label><div class="form-actions"><button data-action="iso-upload">Envoyer / reprendre l’envoi</button><button data-action="iso-pause">Suspendre l’envoi</button></div><progress id="iso-progress" value="0" max="100" hidden></progress><p id="iso-message" class="status-message" role="status"></p><details><summary>Réseau pour une installation manuelle</summary><div id="iso-network">Chargement…</div><p>Le réseau n’est pas configuré automatiquement par un ISO arbitraire. Utiliser l’IPv4 en /32 et une route vers sa passerelle hors sous-réseau ; l’IPv6 est en /128 avec sa passerelle link-local. Le pare-feu et l’isolation du VPS restent actifs.</p></details></article>`;}

export class MediaView {
 constructor({api,escape,selected,toast}){this.api=api;this.esc=escape;this.selected=selected;this.toast=toast;this.busy=false;this.pause=false;this.generation=0;}
 endpoint(id){return 'portal/vps/'+id+'/media';}
 async load(v){
  const generation=++this.generation;const result=await this.api(this.endpoint(v.id));
  if(this.selected()!==v.id||!document.querySelector('#iso-state')||generation!==this.generation)return;
  const m=result.media;this.record=m;
  document.querySelector('#iso-state').innerHTML=m?`<p><strong>${this.esc(m.filename)}</strong> · ${(m.size/1e9).toFixed(2)} Go · ${this.esc({uploading:'Envoi en cours',transferring:'Transfert vers Proxmox',ready:'Prêt',failed:'Échec'}[m.state]||m.state)}</p><p>${this.esc(m.message||'')}</p>${m.state==='ready'?`<p>${m.mounted?'CD inséré':'Lecteur vide'}${m.next_boot?' · Boot ISO demandé':''}</p><div class="form-actions"><button data-action="iso-insert">Insérer sans reboot</button><button data-action="iso-eject">Éjecter</button><button data-action="iso-next_boot">Prochain boot sur ISO</button></div>`:''}<button class="danger" data-action="iso-delete" ${m.state==='transferring'?'disabled':''}>Supprimer cet ISO</button>`:'<p>Lecteur vide. Aucun ISO enregistré pour ce VPS.</p>';
  if(m&&['ready','failed'].includes(m.state))document.querySelector('#iso-message').textContent=m.message||'';
  if(m?.state==='transferring')setTimeout(()=>{if(this.selected()===v.id&&document.querySelector('#iso-state'))this.load(v).catch(e=>this.toast(e.message));},1500);
  const cacheKey=v.id+v.ip+v.public_ipv6;this.networks??={};
  const catalogue=this.networks[cacheKey]||await this.api('portal/vps/'+v.id+'/os-images');this.networks[cacheKey]=catalogue;
  if(this.selected()!==v.id||!document.querySelector('#iso-network'))return;
  const n=catalogue.network;
  document.querySelector('#iso-network').innerHTML=`<dl><dt>Hostname</dt><dd>${this.esc(n.hostname)}</dd><dt>IPv4</dt><dd>${this.esc(n.ipv4)}/32</dd><dt>Passerelle IPv4</dt><dd>${this.esc(n.gateway)}</dd>${n.ipv6?`<dt>IPv6</dt><dd>${this.esc(n.ipv6)}/128</dd><dt>Passerelle IPv6</dt><dd>${this.esc(n.gateway_ipv6)} (sur l’interface réseau)</dd>`:''}<dt>DNS</dt><dd>${this.esc(n.dns.join(', '))}</dd><dt>MTU</dt><dd>${this.esc(n.mtu)}</dd></dl>`;
 }
 async action(action,v){
  if(action==='pause'){this.pause=true;return;}
  if(action==='upload')return this.upload(v);
  if(action==='delete'){
   if(!confirm('Éjecter et supprimer uniquement l’ISO privé de ce VPS ? Le disque système est conservé.'))return;
   await this.api(this.endpoint(v.id),'DELETE',{});
  }else await this.api(this.endpoint(v.id)+'/attach','POST',{action});
  await this.load(v);
 }
 async upload(v){
  if(this.busy)throw Error('Un envoi est déjà en cours dans cet onglet.');
  const file=document.querySelector('#client-iso-file').files[0];
  if(!file||!file.name.toLowerCase().endsWith('.iso')||file.size>10_000_000_000||file.size<32774)throw Error('Choisir un ISO de 10 Go maximum.');
  this.busy=true;this.pause=false;
  const message=text=>{if(document.querySelector('#iso-message'))document.querySelector('#iso-message').textContent=text;};
  try{
   let {media,chunk_bytes}=await this.api(this.endpoint(v.id));
   if(!media){const r=await this.api(this.endpoint(v.id),'POST',{filename:file.name,size:file.size});media=r.media;chunk_bytes=r.chunk_bytes;}
   if(media.state!=='uploading'||media.filename!==file.name||media.size!==file.size)throw Error('Un ISO existe déjà. Supprimer le média actuel avant d’en envoyer un autre.');
   let offset=media.offset;
   while(offset<file.size){
    if(this.pause){message('Envoi suspendu. Rechoisir le même fichier pour reprendre.');await this.load(v);return;}
    const bytes=new Uint8Array(await file.slice(offset,offset+chunk_bytes).arrayBuffer());let binary='';
    for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
    const r=await this.api(this.endpoint(v.id)+'/chunk','POST',{id:media.id,offset,data:btoa(binary)});offset=r.offset;
    const p=document.querySelector('#iso-progress');if(p){p.hidden=false;p.value=100*offset/file.size;}
    message(`Envoi : ${(100*offset/file.size).toFixed(1)} % — ${(offset/1e9).toFixed(2)} / ${(file.size/1e9).toFixed(2)} Go`);
   }
   await this.api(this.endpoint(v.id)+'/complete','POST',{id:media.id});message('Envoi terminé. Vérification de l’ISO et transfert vers Proxmox…');await this.load(v);
  }finally{this.busy=false;}
 }
}
