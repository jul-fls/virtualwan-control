/* Pure catalogue presentation helpers: no API calls or order mutations. */
(function(root){
 'use strict';
 function prices(o){return o.purchase_prices||(o.prices||o.pricings||[]).filter(p=>!['P0D','PT0S'].includes(p.duration)&&(!p.capacities?.length||p.capacities.includes('renew')||p.capacities.includes('installation')));}
 function basePrice(o){
  const candidates=prices(o).flatMap(p=>{const m=/^P(\d+)(M|Y)$/.exec(p.duration||'');const value=p.price?.value;if(!m||typeof value!=='number'||!Number.isFinite(value)||value<0)return [];const months=Number(m[1])*(m[2]==='Y'?12:1);return months>0?[{value:value/months,currency:p.price.currencyCode||'EUR',months,mode:p.pricingMode||'default',duration:p.duration}]:[];});
  candidates.sort((a,b)=>(a.mode==='default'?0:1)-(b.mode==='default'?0:1)||(a.months===1?0:1)-(b.months===1?0:1)||a.value-b.value);
  return candidates[0]||null;
 }
 function hardware(o){const t=o.technical||{};const positive=v=>Number.isFinite(Number(v))&&Number(v)>0?Number(v):null;const disks=t.storage?.disks||[];const capacities=disks.map(d=>positive(d.capacity));return {ram:positive(t.memory?.size),cpu:positive(t.cpu?.cores),storage:capacities.length&&capacities.every(v=>v!==null)?disks.reduce((n,d)=>n+Number(d.capacity)*(positive(d.number??d.quantity??d.count)||1),0):null};}
 function selectOffers(offers,filters={}){
  const result=offers.filter(o=>{const h=hardware(o);return (!filters.ssd||(o.technical?.storage?.disks||[]).some(d=>/ssd|nvme/i.test(`${d.technology||''} ${d.interface||''}`)))&&['ram','cpu','storage'].every(k=>!filters[k]||String(h[k])===String(filters[k]));});
  const key=filters.sort||'price',direction=filters.order==='desc'?-1:1;
  return result.sort((a,b)=>{const av=key==='price'?basePrice(a)?.value:hardware(a)[key],bv=key==='price'?basePrice(b)?.value:hardware(b)[key];if(av==null&&bv!=null)return 1;if(bv==null&&av!=null)return -1;return (av==null||bv==null?0:(av-bv)*direction)||String(a.planCode).localeCompare(String(b.planCode));});
 }
 const api={prices,basePrice,hardware,selectOffers};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.VwanOffers=api;
})(globalThis);
