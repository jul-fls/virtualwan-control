"""Live catalogue enrichment and unpaid, profile-bound configuration drafts."""
import json
import time
from urllib.parse import quote
from . import domain
from .store import ident


def enrich(offers,catalog):
    products={p['name']:p for p in catalog.get('products',[])}
    plans={p['planCode']:p for p in catalog.get('plans',[])+catalog.get('addons',[])}
    result=[]
    for offer in offers:
        plan=plans.get(offer['planCode'],{});product=products.get(plan.get('product'),{})
        blobs=product.get('blobs') or {}
        configs=plan.get('configurations',[])+product.get('configurations',[])
        result.append({**offer,'name':plan.get('invoiceName') or offer.get('productName') or offer['planCode'],
                       'technical':blobs.get('technical',{}),'configurations':configs,
                       'configuration_metadata':(blobs.get('meta') or {}).get('configurations',[])})
    return result


def pricing_valid(offer,selection):
    prices=offer.get('prices',offer.get('pricings',[]))
    if not any(p.get('duration')==selection.get('duration') and p.get('pricingMode')==selection.get('pricingMode') for p in prices):
        raise ValueError('Tarification non disponible dans le catalogue pour '+offer['planCode'])


def describe(api,profile,payload):
    kind=payload['kind'];selection={k:payload[k] for k in ('planCode','duration','pricingMode','quantity')}
    offers=api.catalogue(profile,kind);offer=next((o for o in offers if o['planCode']==selection['planCode']),None)
    if not offer:raise ValueError('Offre absente du catalogue disponible pour ce service.')
    pricing_valid(offer,selection)
    if kind=='additional_ip' and not is_ip_offer(offer):raise ValueError('Choisir une offre Additional IP, pas une autre option du VPS.')
    cart=api.call('POST','/order/cart',{'ovhSubsidiary':profile['config']['ovh'].get('subsidiary','FR'),'description':'Virtualwan configuration non payante'})
    cid=cart['cartId'];api.call('POST',f'/order/cart/{cid}/assign')
    path='/order/cartServiceOption/vps/'+quote(profile['config']['ovh']['expected_service'],safe='') if kind=='additional_ip' else f'/order/cart/{cid}/vps'
    main=api.call('POST',path,{**selection,**({'cartId':cid} if kind=='additional_ip' else {})})
    items=[main['itemId']]
    options=payload.get('options',[])
    if kind=='additional_ip' and options:raise ValueError('Options de VPS non applicables aux IP.')
    if kind=='router':
        available=api.option_catalogue(profile,selection['planCode'])
        selected=[];families={}
        for option in options:
            found=next((o for o in available if o['planCode']==option['planCode']),None)
            if not found or found['planCode'] in selected:raise ValueError('Option non disponible ou dupliquée.')
            pricing_valid(found,option);selected.append(found['planCode'])
            families[found['family']]=families.get(found['family'],0)+1
            item=api.call('POST',f'/order/cart/{cid}/vps/options',{'itemId':main['itemId'],**{k:option[k] for k in ('planCode','duration','pricingMode')},'quantity':1})
            items.append(item['itemId'])
        for option in available:
            count=families.get(option['family'],0)
            if option.get('mandatory') and not count:raise ValueError('Choisir une option dans '+option['family'])
            if option.get('exclusive') and count>1:raise ValueError('Options incompatibles dans '+option['family'])
    fields=[]
    for iid in items:
        for required in api.call('GET',f'/order/cart/{cid}/item/{iid}/requiredConfiguration'):
            meta=next((c for c in offer.get('configurations',[]) if c['name']==required['label']),{})
            values=required.get('fields') or meta.get('values') or []
            # RequiredConfiguration is authoritative; catalog metadata only supplies labels.
            if required.get('fields'):values=required['fields']
            fields.append({**required,'item_id':iid,'values':values})
    nonce=ident();draft={'cart_id':cid,'item_ids':items,'fields':fields,'profile_id':profile['id'],
                        'profile_revision':profile['revision'],'selection':selection,'options':options,'kind':kind,
                        'expires_at':time.time()+900,'used':False}
    with api.store.tx() as db:db.execute('INSERT INTO settings VALUES(?,?)',('catalogue-draft:'+nonce,json.dumps(draft)))
    return {'draft_id':nonce,'fields':fields,'offer':offer,'options':options,'expires_at':draft['expires_at']}


def configure_draft(api,profile,payload):
    key='catalogue-draft:'+payload['draft_id']
    with api.store.tx() as db:
        row=db.execute('SELECT value FROM settings WHERE name=?',(key,)).fetchone()
        if not row:raise ValueError('Sélection expirée ; recharger les choix.')
        draft=json.loads(row['value'])
        if draft['used'] or draft['expires_at']<time.time():raise ValueError('Sélection déjà utilisée ou expirée.')
        if draft['profile_id']!=profile['id'] or draft['profile_revision']!=profile['revision']:raise domain.Conflict('Profil modifié après sélection.')
        if draft['kind']!=payload['kind'] or any(draft['selection'][k]!=payload[k] for k in draft['selection']):raise domain.Conflict('Offre modifiée ; recharger les choix.')
        values=payload.get('item_configurations',{})
        expected={(str(f['item_id']),f['label']) for f in draft['fields']}
        if any((str(iid),label) not in expected for iid,fields in values.items() for label in fields):raise ValueError('Champ OVH inconnu.')
        for field in draft['fields']:
            value=values.get(str(field['item_id']),{}).get(field['label'],'')
            if not value and field['required']:raise ValueError('Renseigner '+field['label'])
            if value and field['values'] and value not in field['values']:raise ValueError('Valeur non disponible pour '+field['label'])
            if len(str(value))>1024:raise ValueError('Champ OVH trop long.')
        draft['used']=True;db.execute('UPDATE settings SET value=? WHERE name=?',(json.dumps(draft),key))
    for field in draft['fields']:
        value=values.get(str(field['item_id']),{}).get(field['label'])
        if value:api.call('POST',f"/order/cart/{draft['cart_id']}/item/{field['item_id']}/configuration",{'label':field['label'],'value':str(value)})
    result=api.call('GET',f"/order/cart/{draft['cart_id']}/checkout");domain.amount(result)
    return {'cart_id':draft['cart_id'],'supplier_quote':result,'quote_digest':domain.digest(domain.quote_binding(result)),
            'expires_at':min(draft['expires_at'],time.time()+900),'required_configurations':draft['fields']}


def is_ip_offer(offer):
    import re
    text=' '.join(str(offer.get(k,'')) for k in ('family','planCode','productName','name')).lower()
    if re.search(r'ipv6|ip[-_ ]?v6|ip[-_ ]?block',text):return False
    return bool(re.search(r'(^|[^a-z])ip([^a-z]|$)|additional.?ip|ipv4|failover|floating',text))
