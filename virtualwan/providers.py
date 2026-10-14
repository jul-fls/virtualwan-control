import hashlib
import ipaddress
import json
import time
from urllib.parse import quote
import requests
from .domain import Conflict,digest,quote_binding,amount


class OvhAPI:
    def __init__(self,config,store,run):
        self.cfg=config;self.store=store;self.run=run;self.base=config['endpoint'].rstrip('/')
        if self.base not in ('https://eu.api.ovh.com/1.0','https://ca.api.ovh.com/1.0','https://us.api.ovh.com/1.0'):
            raise ValueError('Endpoint OVH non pris en charge.')
        self.keys=[store.secret_get(config[x]) for x in ('application_key_env','application_secret_env','consumer_key_env')]
        if not all(self.keys):raise ValueError('Identifiants API OVH manquants dans le coffre/environnement.')
        response=requests.get(self.base+'/auth/time',timeout=20);response.raise_for_status();self.offset=int(response.json())-int(time.time())

    def call(self,method,path,data=None):
        body='' if data is None else json.dumps(data,separators=(',',':'));url=self.base+path
        stamp=str(int(time.time())+self.offset);ak,sk,ck=self.keys
        signature='$1$'+hashlib.sha1('+'.join((sk,ck,method,url,body,stamp)).encode()).hexdigest()
        self.store.event(self.run,'ovh.request.begin',method=method,path=path,parameters=data)
        response=requests.request(method,url,headers={'X-Ovh-Application':ak,'X-Ovh-Consumer':ck,'X-Ovh-Timestamp':stamp,'X-Ovh-Signature':signature,'Content-Type':'application/json'},data=body,timeout=30)
        self.store.event(self.run,'ovh.request.end',status=response.status_code,path=path)
        response.raise_for_status()
        return response.json() if response.content else None

    def inventory(self,service):
        result=[]
        # Only individually routed /32s: never expand a foreign subnet or count the primary.
        for block in self.call('GET','/ip?routedTo.serviceName='+quote(service,safe='')):
            network=ipaddress.ip_network(block,strict=False)
            if network.version!=4 or network.prefixlen!=32:continue
            record=self.call('GET','/ip/'+quote(block,safe=''))
            if (record.get('routedTo') or {}).get('serviceName')==service:
                ip=str(network.network_address);result.append(ip)
        return result

    def catalogue(self,profile,kind):
        from .catalogue import enrich,is_ip_offer
        cart=self.call('POST','/order/cart',{'ovhSubsidiary':profile['config']['ovh'].get('subsidiary','FR'),'description':'Virtualwan catalogue only'})
        if kind=='additional_ip':
            if not profile['config']['ovh'].get('expected_service'):raise ValueError('Service VPS cible requis pour acheter une IP.')
            return [o for o in enrich(self.call('GET','/order/cart/'+cart['cartId']+'/ip'),{}) if is_ip_offer(o)]
        public=self.public_catalogue(profile);plans={p['planCode'] for p in public.get('plans',[])}
        return enrich([o for o in self.call('GET','/order/cart/'+cart['cartId']+'/vps') if o['planCode'] in plans],public)

    def public_catalogue(self,profile):
        response=requests.get(self.base+'/order/catalog/public/vps',params={'ovhSubsidiary':profile['config']['ovh'].get('subsidiary','FR')},timeout=30)
        response.raise_for_status();return response.json()

    def option_catalogue(self,profile,plan):
        from .catalogue import enrich
        cart=self.call('POST','/order/cart',{'ovhSubsidiary':profile['config']['ovh'].get('subsidiary','FR'),'description':'Virtualwan options non payantes'})
        return enrich(self.call('GET',f"/order/cart/{cart['cartId']}/vps/options?planCode="+quote(plan,safe='')),self.public_catalogue(profile))

    def describe(self,profile,payload):
        from .catalogue import describe
        return describe(self,profile,payload)

    def prepare(self,profile,payload):
        if payload.get('draft_id'):
            from .catalogue import configure_draft
            return configure_draft(self,profile,payload)
        cart=self.call('POST','/order/cart',{'ovhSubsidiary':profile['config']['ovh'].get('subsidiary','FR'),'description':'Virtualwan quote '+payload['purchase_id']})
        cid=cart['cartId'];self.call('POST',f'/order/cart/{cid}/assign')
        item={key:payload[key] for key in ('planCode','duration','pricingMode','quantity')}
        if payload['kind']=='additional_ip':
            path=f'/order/cart/{cid}/ip'
            result=self.call('POST',path,item)
        else:result=self.call('POST',f'/order/cart/{cid}/vps',item)
        iid=result['itemId']
        required=self.call('GET',f'/order/cart/{cid}/item/{iid}/requiredConfiguration')
        supplied=dict(payload.get('configurations',{}))
        if payload['kind']=='additional_ip':supplied['destination']=profile['config']['ovh']['expected_service']
        missing=[r['label'] for r in required if r.get('required',True) and r['label'] not in supplied]
        if missing:raise ValueError('Configurations OVH requises : '+', '.join(missing))
        for label,value in supplied.items():
            self.call('POST',f'/order/cart/{cid}/item/{iid}/configuration',{'label':label,'value':str(value)})
        quotation=self.call('GET',f'/order/cart/{cid}/checkout')
        amount(quotation)
        ttl=min(time.time()+900,time.time()+max(60,int(profile['config']['ovh'].get('quote_ttl',900))))
        return {'cart_id':cid,'supplier_quote':quotation,'quote_digest':digest(quote_binding(quotation)),
                'expires_at':ttl,'required_configurations':required}

    def checkout(self,cart_id,auto_pay=False):
        # Call exactly once; any ambiguous transport failure must be reconciled manually.
        return self.call('POST',f'/order/cart/{cart_id}/checkout',{'autoPayWithPreferredPaymentMethod':auto_pay,'waiveRetractationPeriod':False})

    def delivery(self,order_id):
        status=self.call('GET',f'/me/order/{order_id}/status')
        details=[]
        for item in self.call('GET',f'/me/order/{order_id}/details'):
            details.append(self.call('GET',f'/me/order/{order_id}/details/{item}'))
        return {'status':status,'details':details}


class DemoOvh:
    """Explicit offline demo. Its output is always marked simulated and cannot enter live jobs."""
    def __init__(self,config,store,run):self.store=store;self.run=run
    def catalogue(self,profile,kind):
        return [{'planCode':'DEMO-ip' if kind=='additional_ip' else 'DEMO-router','productName':'Simulation — aucun achat','name':'Additional IPv4 — démo' if kind=='additional_ip' else 'VPS SSD — démo',
                 'technical':{} if kind=='additional_ip' else {'cpu':{'cores':4},'memory':{'size':8},'storage':{'disks':[{'capacity':75,'technology':'SSD','interface':'NVMe'}]}},
                 'pricings':[{'duration':'P1M','pricingMode':'default','price':{'currencyCode':'EUR','value':2.00}}]}]
    def option_catalogue(self,profile,plan):
        return [{'planCode':'DEMO-disk','family':'additionalDisk','exclusive':True,'mandatory':False,'name':'Disque supplémentaire 50 Go — démo','pricings':[{'duration':'P1M','pricingMode':'default','price':{'currencyCode':'EUR','value':1}}]}]
    def describe(self,profile,payload):
        from .store import ident
        fields=[{'item_id':1,'label':'country' if payload['kind']=='additional_ip' else 'vps_datacenter','required':True,'values':['FR','DE','UK'] if payload['kind']=='additional_ip' else ['GRA','SBG','DE']}]
        if payload['kind']=='router':fields.append({'item_id':1,'label':'vps_os','required':False,'values':['Debian 13','Ubuntu 24.04']})
        key=ident();draft={'profile_id':profile['id'],'fields':fields,'selection':{k:payload[k] for k in ('planCode','duration','pricingMode','quantity')},'kind':payload['kind'],'profile_revision':profile['revision'],'cart_id':'DEMO-'+key,'options':payload.get('options',[]),'expires_at':time.time()+900,'used':False}
        with self.store.tx() as db:db.execute('INSERT INTO settings VALUES(?,?)',('catalogue-draft:'+key,json.dumps(draft)))
        return {'draft_id':key,'fields':fields,'offer':self.catalogue(profile,payload['kind'])[0],'options':draft['options'],'expires_at':draft['expires_at']}
    def prepare(self,profile,payload):
        if payload.get('draft_id'):
            from .catalogue import configure_draft
            return configure_draft(self,profile,payload)
        value=2.0*payload['quantity'];quote={'prices':{'withTax':{'value':value,'currencyCode':'EUR'},'withoutTax':{'value':value,'currencyCode':'EUR'},'tax':{'value':0,'currencyCode':'EUR'}},'contracts':[],
                                         'details':[{'description':'Simulation hors ligne','quantity':payload['quantity']}],'simulated':True}
        return {'cart_id':'DEMO-'+payload['purchase_id'],'supplier_quote':quote,'quote_digest':digest(quote_binding(quote)),'expires_at':time.time()+900,'required_configurations':[]}
    def call(self,method,path,data=None):
        if path.startswith('/order/cart/DEMO-'):
            if method=='POST' and path.endswith('/configuration'):return {'simulated':True}
            if method=='GET' and path.endswith('/checkout'):
                nonce=path.split('/')[3].removeprefix('DEMO-')
                records=self.store.rows('SELECT value FROM settings WHERE name=?',('catalogue-draft:'+nonce,))
                draft=json.loads(records[0]['value']) if records else {}
                value=2*(draft.get('selection') or {}).get('quantity',1)+len(draft.get('options',[]))
                quote={'prices':{'withTax':{'value':value,'currencyCode':'EUR'}},'contracts':[],'details':[{'description':'Simulation hors ligne'}],'simulated':True}
                return quote
        raise Conflict('Le fournisseur de démonstration ne dispose pas d’API réelle.')
    def checkout(self,cart_id,auto_pay=False):return {'orderId':'DEMO-'+cart_id,'url':None,'simulated':True}
