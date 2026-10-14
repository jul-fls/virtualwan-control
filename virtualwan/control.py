from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
from . import domain
from .adapters.provision import fqdn_for
from .providers import OvhAPI,DemoOvh
from .store import ident


class Control:
    def __init__(self,store,demo=False,allow_purchase=False,allow_provision=False,provider_factory=None):
        self.store=store;self.demo=demo;self.allow_purchase=allow_purchase;self.allow_provision=allow_provision
        self.provider_factory=provider_factory or (DemoOvh if demo else OvhAPI)
        self.stop=threading.Event();self.checkout_lock=threading.Lock();self.thread=None

    def provider(self,profile,run):return self.provider_factory(profile['config']['ovh'],self.store,run)

    def add_profile(self,data,key=None):
        item=domain.profile(data);key=key or ident()
        if item['provider'] not in ('ovh','manual'):raise ValueError('Fournisseur non pris en charge.')
        with self.store.tx() as db:
            old=db.execute('SELECT revision FROM profiles WHERE id=?',(key,)).fetchone()
            count=db.execute('SELECT count(*) FROM ip_pool WHERE profile_id=?',(key,)).fetchone()[0]
            if count>item['capacity']:raise domain.Conflict('La capacité est inférieure aux IP déjà inventoriées.')
            # Different routers require distinct routed segments; a shared firewall may be reused.
            for row in db.execute('SELECT id,data FROM profiles WHERE id!=?',(key,)):
                other=json.loads(row['data'])
                primary=item['config']['vps'].get('primary_ip')
                if primary and primary==other['config']['vps'].get('primary_ip'):raise domain.Conflict('Ce routeur primaire possède déjà un profil.')
                service=item['config']['ovh'].get('expected_service')
                if service and service==other['config']['ovh'].get('expected_service') and item['config']['ovh']['endpoint']==other['config']['ovh']['endpoint']:raise domain.Conflict('Ce service OVH possède déjà un profil.')
                same_firewall=item['config']['pfsense']['url']==other['config']['pfsense']['url'] or (item['config']['pfsense'].get('connect_ip') and item['config']['pfsense'].get('connect_ip')==other['config']['pfsense'].get('connect_ip'))
                if same_firewall:
                    same_iface=item['config']['pfsense'].get('public_interface') and item['config']['pfsense'].get('public_interface')==other['config']['pfsense'].get('public_interface')
                    import ipaddress
                    same_net=ipaddress.IPv4Network(item['config']['pfsense']['next_hop_network']).overlaps(ipaddress.IPv4Network(other['config']['pfsense']['next_hop_network']))
                    if same_iface or same_net:raise domain.Conflict('Chaque routeur doit disposer de son propre segment réseau sur ce firewall.')
            revision=(old['revision']+1) if old else 1
            db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?)',(key,json.dumps(item),revision))
        self.store.event(key,'profile.saved',profile=self.store.clean(item),revision=revision)
        return key

    def clone_profile(self,key,name,network,gateway):
        old=self.store.document('profiles',key);item={k:deepcopy(old[k]) for k in ('name','provider','capacity','ready','config')}
        item.update(name=name,ready=False)
        item['config']['pfsense'].update(next_hop_network=network,next_hop=None,public_interface=None,wireguard_interface=None,wireguard_gateway=None)
        item['config']['guest']['gateway']=gateway
        item['config']['vps'].update(host='',primary_ip='')
        item['config']['ovh'].update(expected_service=None,ip_block=None)
        segment='router_'+ident()[:8]
        item['config']['pve'].update(bridge='',isolation_segment=segment)
        item['config']['pfsense'].update(public_alias='VWAN_'+segment.upper(),wireguard_gateway='WG_'+segment.upper())
        item['config']['router']={**item['config'].get('router',{}),'tunnel_address':None,'peer_address':None,'peer_public_key':None}
        item['config']['vps']['password_env']='ROUTER_'+segment.upper()+'_SSH_PASSWORD'
        item['config']['vps']['sudo_password_env']=item['config']['vps']['password_env']
        return self.add_profile(item)

    def add_model(self,data,key=None):
        item=domain.model(data);key=key or ident()
        with self.store.tx() as db:db.execute('INSERT OR REPLACE INTO models VALUES(?,?)',(key,json.dumps(item)))
        self.store.event(key,'model.saved',model=item);return key

    def add_ip(self,profile_id,ip):
        ip=domain.public_ip(ip);p=self.store.document('profiles',profile_id)
        if ip==p['config']['vps'].get('primary_ip'):raise ValueError('L’IP de management ne doit pas entrer dans le stock client.')
        with self.store.tx() as db:
            count=db.execute('SELECT count(*) FROM ip_pool WHERE profile_id=?',(profile_id,)).fetchone()[0]
            if count>=p['capacity']:raise domain.Conflict('Routeur plein. Dupliquer le profil et préparer un nouveau routeur.')
            if db.execute('SELECT 1 FROM ip_pool WHERE ip=?',(ip,)).fetchone():raise domain.Conflict('IP déjà présente dans l’inventaire.')
            db.execute('INSERT INTO ip_pool VALUES(?,?,?,?,?)',(ip,profile_id,'available',None,time.time()))

    def deployments(self):
        return [{**json.loads(row['data']),**{k:v for k,v in row.items() if k!='data'}} for row in self.store.rows('SELECT * FROM deployments ORDER BY rowid DESC')]

    def request_deploy(self,body,model_snapshot=None):
        profile=self.store.document('profiles',body['profile_id']);model=model_snapshot or self.store.document('models',body['model_id'])
        if not profile['ready']:raise domain.Conflict('Le profil doit être configuré et prêt avant un déploiement.')
        if not self.demo and not self.allow_provision:raise domain.Conflict('Déploiement réel désactivé par VWAN_ENABLE_PROVISIONING.')
        auth=body.get('ssh_auth','key')
        if auth not in ('key','password','both'):raise ValueError('Méthode SSH invalide.')
        if auth in ('key','both') and not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+(?: [^\r\n]*)?',body.get('public_key','').strip()):raise ValueError('Clé SSH publique invalide.')
        if auth in ('password','both') and len(body.get('password',''))<12:raise ValueError('Mot de passe invité de 12 caractères minimum.')
        config=deepcopy(profile['config']);config['pve'].update({k:model[k] for k in ('cores','memory_mb','disk_gb','template')})
        config['pve']['vmid']=None;config['pfsense']['next_hop']=None
        config['guest']['ssh_auth']=auth
        config['guest']['username']=body.get('username',config['guest'].get('username','debian'))
        if config['guest']['username']=='root' or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',config['guest']['username']):raise ValueError('Utilisateur Linux non-root valide requis.')
        config['require_client_isolation']=True;config['start_vm']=True
        customer=str(body.get('customer','')).strip()[:150]
        with self.store.tx() as db:
            if body.get('ip'):
                row=db.execute("SELECT * FROM ip_pool WHERE ip=? AND profile_id=? AND state='available'",(body['ip'],profile['id'])).fetchone()
            else:row=db.execute("SELECT * FROM ip_pool WHERE profile_id=? AND state='available' ORDER BY seen_at LIMIT 1",(profile['id'],)).fetchone()
            if not row:raise domain.Conflict('Aucune IP disponible : ajouter du stock ou préparer un achat soumis à approbation.')
            ip=row['ip'];config['public_ip']=ip
            if self.demo:
                fqdn=config.get('hostname_pattern','vps-{ipv4_compact}.{domain}').format(ipv4=ip,ipv4_compact=ip.replace('.',''),ipv4_dashed=ip.replace('.','-'),domain=config['domain'])
            else:fqdn=fqdn_for(ip,config['domain'],config.get('hostname_pattern','vps-{ipv4_compact}.{domain}'))
            payload={'profile_id':profile['id'],'model_id':model['id'],'config':config,'customer':customer,'public_key':body.get('public_key','').strip(),'simulated':self.demo}
            job=self.store.job('deploy',payload,db)
            if auth in ('password','both'):
                name='GUEST_PASSWORD_'+job.upper();config['guest']['password_env']=name
                db.execute('INSERT INTO secrets VALUES(?,?)',(name,self.store.cipher.encrypt(body['password'].encode())))
                payload['config']=config;db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),job))
                self.store.redactions.add(body['password'])
            db.execute("UPDATE ip_pool SET state='reserved',job_id=? WHERE ip=?",(job,ip))
            db.execute('INSERT INTO deployments VALUES(?,?,?,?,?)',(ident(),ip,profile['id'],model['id'],json.dumps({'fqdn':fqdn,'status':'queued','job_id':job,'customer':customer,'mac':None,'next_hop':None,'vmid':None,'simulated':self.demo})))
        self.store.event(job,'deployment.requested',public_ip=ip,profile_id=profile['id'],model=model,customer=customer,simulated=self.demo)
        return job

    def pending_quantity(self,profile_id,exclude=None):
        self.expire_quotes()
        return sum(json.loads(p['data']).get('quantity',0) for p in self.store.rows("SELECT id,data FROM purchases WHERE profile_id=? AND state IN ('preparing','awaiting_approval','checking_out','awaiting_payment','awaiting_delivery','uncertain')",(profile_id,)) if p['id']!=exclude and json.loads(p['data']).get('kind')=='additional_ip')

    def expire_quotes(self):
        with self.store.tx() as db:
            for row in db.execute("SELECT id,data FROM purchases WHERE state='awaiting_approval'").fetchall():
                if json.loads(row['data']).get('expires_at',0)<=time.time():db.execute("UPDATE purchases SET state='expired' WHERE id=? AND state='awaiting_approval'",(row['id'],))

    def capacity_check(self,p,quantity,exclude=None):
        count=self.store.one('SELECT count(*) AS n FROM ip_pool WHERE profile_id=?',(p['id'],))['n']
        if count+self.pending_quantity(p['id'],exclude)+quantity>p['capacity']:raise domain.Conflict('Capacité du routeur atteinte, stock et commandes en attente inclus. Préparer un nouveau routeur.')

    def request_quote(self,body):
        p=self.store.document('profiles',body['profile_id']);kind=body.get('kind','additional_ip')
        if p['provider']!='ovh':raise ValueError('Commande API disponible uniquement pour OVH.')
        if kind not in ('additional_ip','router'):raise ValueError('Type d’achat invalide.')
        quantity=domain.positive(body.get('quantity',1),16 if kind=='additional_ip' else 1)
        if kind=='additional_ip':
            if not p['config']['ovh'].get('expected_service'):raise ValueError('Service OVH du routeur requis.')
        elif p['ready'] or p['config']['ovh'].get('expected_service'):
            raise domain.Conflict('Commander un routeur depuis un nouveau profil vide, issu de Dupliquer, pour ne pas remplacer un routeur existant.')
        required=('planCode','duration','pricingMode')
        if any(not body.get(k) or len(str(body[k]))>200 for k in required):raise ValueError('Offre/durée/tarification du catalogue OVH requises.')
        purchase=ident();payload={k:body[k] for k in required}
        followup=body.get('followup')
        if followup:
            followup={k:v for k,v in followup.items() if k in ('model_id','customer','username','public_key','ssh_auth')}
        payload.update(kind=kind,quantity=quantity,profile_id=p['id'],profile_revision=p['revision'],configurations=body.get('configurations',{}),purchase_id=purchase,followup=followup,simulated=self.demo)
        if payload['followup']:
            if kind!='additional_ip' or quantity!=1:raise ValueError('Achat + déploiement nécessite exactement une IP.')
            follow=payload['followup'];payload['model_snapshot']=self.store.document('models',follow['model_id'])
            if not p['ready']:raise domain.Conflict('Le profil doit être prêt pour l’achat suivi d’un déploiement.')
            if follow.get('ssh_auth','key')!='key':raise ValueError('Le parcours achat + VM utilise une clé SSH publique ; le mot de passe n’est pas conservé dans le devis.')
            if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+(?: [^\r\n]*)?',follow.get('public_key','').strip()):raise ValueError('Clé publique requise pour la VM suivant l’achat.')
        with self.store.tx() as db:
            if kind=='additional_ip':self.capacity_check(p,quantity)
            db.execute('INSERT INTO purchases VALUES(?,?,?,?,?,?)',(purchase,p['id'],'preparing',json.dumps(payload),1,time.time()))
            job=self.store.job('quote',payload,db)
        self.store.event(purchase,'purchase.quote.requested',kind=kind,quantity=quantity,requires_human_approval=True)
        return {'purchase_id':purchase,'job_id':job}

    def approve(self,key,body,user):
        if not self.demo and not self.allow_purchase:raise domain.Conflict('Commandes réelles désactivées par VWAN_ENABLE_PURCHASES.')
        with self.checkout_lock:
            p=self.store.document('purchases',key);profile=self.store.document('profiles',p['profile_id'])
            if profile['revision']!=p['profile_revision']:raise domain.Conflict('Profil modifié depuis le devis ; recommencer.')
            if p['state']!='awaiting_approval':raise domain.Conflict('Devis déjà traité.')
            if self.demo:fresh=p['supplier_quote']
            else:fresh=self.provider(profile,key).call('GET',f'/order/cart/{p["cart_id"]}/checkout')
            domain.approval_valid(p,body,fresh)
            with self.store.tx() as db:
                current=db.execute('SELECT state FROM purchases WHERE id=?',(key,)).fetchone()
                if current['state']!='awaiting_approval':raise domain.Conflict('Devis déjà traité.')
                if p['kind']=='additional_ip':self.capacity_check(profile,p['quantity'],key)
                p['approval']={'user':user,'time':time.time(),'auto_pay':body.get('auto_pay') is True,'quote_digest':p['quote_digest']}
                db.execute("UPDATE purchases SET state='checking_out',data=? WHERE id=?",(json.dumps(p),key))
            self.store.event(key,'purchase.human.approved',user=user,quote_digest=p['quote_digest'],auto_pay=p['approval']['auto_pay'])
            # Once claimed, any exception is uncertain; no automatic POST retry.
            try:order=self.provider(profile,key).checkout(p['cart_id'],p['approval']['auto_pay'])
            except Exception as error:
                with self.store.tx() as db:db.execute("UPDATE purchases SET state='uncertain' WHERE id=?",(key,))
                self.store.event(key,'purchase.checkout.uncertain',error_type=type(error).__name__,recovery='Vérifier les commandes OVH avant toute nouvelle tentative.')
                raise domain.Conflict('Réponse fournisseur incertaine : vérifier OVH, aucun nouvel achat automatique.') from error
            p['order']=order
            state='simulated' if self.demo else 'awaiting_payment'
            with self.store.tx() as db:db.execute('UPDATE purchases SET state=?,data=? WHERE id=?',(state,json.dumps(p),key))
            self.store.event(key,'purchase.order.created',order_id=order.get('orderId'),state=state,simulated=self.demo)
            return {'state':state,'order':order}

    def job_state(self,key,state,result):
        with self.store.tx() as db:db.execute('UPDATE jobs SET state=?,result=?,updated_at=? WHERE id=?',(state,json.dumps(self.store.clean(result)),time.time(),key))

    def reconcile_order(self,key,body,user):
        from decimal import Decimal
        p=self.store.document('purchases',key)
        if p['state']!='uncertain' or not p.get('approval'):raise domain.Conflict('Seul un checkout approuvé mais incertain peut être rapproché.')
        if body.get('confirmation')!='RAPPROCHER '+key[:8]:raise ValueError('Confirmation manuelle de rapprochement requise.')
        order_id=domain.positive(body['order_id'],999999999999)
        profile=self.store.document('profiles',p['profile_id']);supplier=self.provider(profile,key)
        order=supplier.call('GET',f'/me/order/{order_id}')
        actual=domain.amount(order);expected=domain.amount(p['supplier_quote'])
        if actual.get('currencyCode')!=expected.get('currencyCode') or Decimal(str(actual.get('value')))!=Decimal(str(expected.get('value'))):raise domain.Conflict('Le montant/devise de la commande ne correspond pas au devis approuvé.')
        with self.store.tx() as db:
            for row in db.execute('SELECT id,data FROM purchases WHERE id!=?',(key,)):
                if str(json.loads(row['data']).get('order',{}).get('orderId'))==str(order_id):raise domain.Conflict('Cette commande est déjà liée à une autre demande.')
            current=db.execute('SELECT state FROM purchases WHERE id=?',(key,)).fetchone()['state']
            if current!='uncertain':raise domain.Conflict('Demande déjà rapprochée.')
            p['order']={k:v for k,v in order.items() if k in ('orderId','url','priceWithTax','priceWithoutTax','tax','expirationDate')};p['order']['orderId']=order_id
            p['reconciliation']={'user':user,'time':time.time(),'operator_checked_supplier':True}
            db.execute("UPDATE purchases SET state='awaiting_delivery',data=? WHERE id=?",(json.dumps(p),key))
        self.store.event(key,'purchase.manually.reconciled',order_id=order_id,user=user,checkout_retried=False)
        return {'state':'awaiting_delivery'}

    def sync(self,profile_id,run):
        p=self.store.document('profiles',profile_id)
        if self.demo:return {'simulated':True,'added':0}
        if p['provider']!='ovh':raise ValueError('Inventaire automatique disponible pour OVH ; ajouter les autres IP manuellement.')
        service=p['config']['ovh'].get('expected_service')
        if not service:raise ValueError('Nom du service routeur OVH manquant.')
        owned=set(self.provider(p,run).inventory(service));owned.discard(p['config']['vps'].get('primary_ip'))
        if len(owned)>p['capacity']:raise domain.Conflict('La capacité configurée est inférieure à l’inventaire OVH.')
        added=0
        with self.store.tx() as db:
            for ip in owned:
                existing=db.execute('SELECT * FROM ip_pool WHERE ip=?',(ip,)).fetchone()
                if existing and existing['profile_id']!=profile_id:raise domain.Conflict('Une IP a changé de routeur ; réconcilier explicitement les routes et VM.')
                if not existing:
                    db.execute('INSERT INTO ip_pool VALUES(?,?,?,?,?)',(ip,profile_id,'available',None,time.time()));added+=1
                else:db.execute('UPDATE ip_pool SET seen_at=? WHERE ip=?',(time.time(),ip))
            for row in db.execute('SELECT * FROM ip_pool WHERE profile_id=?',(profile_id,)).fetchall():
                if row['ip'] not in owned:db.execute("UPDATE ip_pool SET state='quarantined' WHERE ip=?",(row['ip'],))
        self.store.event(run,'inventory.synchronized',profile_id=profile_id,owned=sorted(owned),added=added)
        return {'added':added,'owned':sorted(owned)}

    def inspect_clients(self,profile_id,run):
        p=self.store.document('profiles',profile_id)
        if self.demo:return {'simulated':True,'clients':len(self.deployments())}
        cfg=deepcopy(p['config']);env=os.environ.copy()
        for section in cfg.values():
            if isinstance(section,dict):
                for name,value in section.items():
                    if name.endswith('_env') and value:
                        secret=self.store.secret_get(value)
                        if secret:env[value]=secret
        env['VWAN_BACKUP_KEY']=(self.store.root/'master.key').read_text().strip();cfg['backup_key_env']='VWAN_BACKUP_KEY'
        cfg['pve']['known_hosts']=str(self.store.root/'ssh-known-hosts')
        from .adapters.provision import require_ssh_host_key
        require_ssh_host_key(cfg['pve'])
        if cfg['pve'].get('private_key'):
            path=(self.store.root/cfg['pve']['private_key']).resolve()
            if not path.is_relative_to(self.store.root):raise ValueError('Clé SSH hors du volume privé.')
            cfg['pve']['private_key']=str(path)
        directory=self.store.root/'runs'/run;directory.mkdir(parents=True,exist_ok=True);cfg['log_directory']=str(directory/'observer')
        path=directory/'inspect.json';path.write_text(json.dumps(cfg))
        result=subprocess.run([sys.executable,'-m','virtualwan.adapters.observe',str(path)],env=env,capture_output=True,text=True,timeout=180)
        self.store.event(run,'inventory.pve.inspect',exit_code=result.returncode,progress=result.stderr)
        if result.returncode:raise RuntimeError('Lecture PVE refusée ; vérifier accès, clés hôtes et registre d’isolation.')
        clients=json.loads(result.stdout)['clients']
        with self.store.tx() as db:
            for client in clients:
                ip=client['public_ip'];existing=db.execute('SELECT * FROM ip_pool WHERE ip=?',(ip,)).fetchone()
                if existing and existing['profile_id']!=profile_id:raise domain.Conflict('IP enregistrée sur un autre profil.')
                record=db.execute('SELECT * FROM deployments WHERE ip=?',(ip,)).fetchone()
                info=json.loads(record['data']) if record else {'fqdn':ip,'job_id':run,'customer':'Discovered existing VM'}
                info.update(vmid=client['vmid'],mac=client['mac'],next_hop=client['next_hop'],node=client['node'],vm_name=client['name'],status='active' if client['status']=='running' else 'stopped',last_verified=time.time(),source='Proxmox live inventory')
                if record:db.execute('UPDATE deployments SET data=? WHERE id=?',(json.dumps(info),record['id']))
                else:db.execute('INSERT INTO deployments VALUES(?,?,?,?,?)',(ident(),ip,profile_id,None,json.dumps(info)))
                if not existing:db.execute('INSERT INTO ip_pool VALUES(?,?,?,?,?)',(ip,profile_id,'assigned',run,time.time()))
                elif existing['state'] not in ('reserved','quarantined'):db.execute("UPDATE ip_pool SET state='assigned' WHERE ip=?",(ip,))
        return {'clients_observed':len(clients)}

    def follow_delivery(self,key,run):
        purchase=self.store.document('purchases',key);p=self.store.document('profiles',purchase['profile_id'])
        if purchase['state'] not in ('awaiting_payment','awaiting_delivery'):raise domain.Conflict('Commande non réconciliable dans cet état.')
        supplier=self.provider(p,run);delivery=supplier.delivery(purchase['order']['orderId'])
        if delivery['status']=='cancelled':
            with self.store.tx() as db:db.execute("UPDATE purchases SET state='cancelled' WHERE id=?",(key,))
            self.store.event(key,'purchase.supplier.cancelled',order_id=purchase['order']['orderId'],checkout_retried=False)
            return {'state':'cancelled','delivery':delivery}
        if delivery['status']!='delivered':
            purchase['delivery_status']=delivery['status']
            state='awaiting_payment' if delivery['status']=='notPaid' else 'awaiting_delivery'
            with self.store.tx() as db:db.execute('UPDATE purchases SET state=?,data=? WHERE id=?',(state,json.dumps(purchase),key))
            return {'state':state,'delivery':delivery}
        domains={item['domain'] for item in delivery['details'] if isinstance(item.get('domain'),str) and item['domain']}
        if purchase['kind']=='router':
            matches=[name for name in domains if name.startswith('vps-')]
            if len(matches)!=1:return {'delivery':delivery,'needs_review':True,'reason':'Service VPS livré non identifié sans ambiguïté.'}
            purchase['delivered_service']=matches[0];state='needs_setup'
            import ipaddress
            addresses=supplier.call('GET','/vps/'+matches[0]+'/ips')
            ipv4=[ip for ip in addresses if ipaddress.ip_address(ip).version==4]
            if len(ipv4)!=1:raise domain.Conflict('L’IP primaire du nouveau routeur est ambiguë ; configuration manuelle requise.')
            config=deepcopy(p['config']);config['ovh']['expected_service']=matches[0]
            config['vps'].update(host=ipv4[0],primary_ip=ipv4[0])
            self.add_profile({**p,'config':config,'ready':False},p['id'])
        else:
            self.sync(p['id'],run)
            owned={x['ip'] for x in self.store.rows('SELECT ip FROM ip_pool WHERE profile_id=?',(p['id'],))}
            live_owned=set(supplier.inventory(p['config']['ovh']['expected_service']))
            live_owned.discard(p['config']['vps'].get('primary_ip'))
            matched=[]
            for name in domains:
                try:ip=domain.public_ip(name.split('/')[0])
                except ValueError:continue
                if ip in owned and ip in live_owned:matched.append(ip)
            if len(set(matched))!=purchase['quantity']:
                return {'delivery':delivery,'needs_review':True,'reason':'IP livrées non liées sans ambiguïté aux lignes de commande ; inventaire mis à jour, aucune VM créée automatiquement.'}
            purchase['delivered_ips']=sorted(set(matched));state='delivered'
            if purchase.get('followup') and not purchase.get('deployment_job'):
                body={**purchase['followup'],'profile_id':p['id'],'ip':purchase['delivered_ips'][0]}
                purchase['deployment_job']=self.request_deploy(body,purchase['model_snapshot'])
        with self.store.tx() as db:db.execute('UPDATE purchases SET state=?,data=? WHERE id=?',(state,json.dumps(purchase),key))
        self.store.event(key,'purchase.delivery.reconciled',state=state,delivery=delivery)
        return {'state':state,'purchase':purchase}

    def request_router_setup(self,key,body):
        p=self.store.document('profiles',key)
        if not self.demo and not self.allow_provision:raise domain.Conflict('Installation désactivée par VWAN_ENABLE_PROVISIONING.')
        if body.get('confirmation')!='CONFIGURER '+key[:8]:raise ValueError('Saisir la phrase CONFIGURER affichée.')
        cfg=p['config']
        if p['ready']:raise domain.Conflict('Installation réservée à un routeur neuf, pas à une mise à jour.')
        if self.store.rows('SELECT id FROM deployments WHERE profile_id=?',(key,)):raise domain.Conflict('Ce routeur possède déjà des VM.')
        domain.public_ip(cfg['vps'].get('primary_ip','')) if not self.demo else None
        if not cfg['ovh'].get('expected_service'):raise ValueError('Service OVH livré requis.')
        if cfg['vps'].get('config_path')!='/etc/virtualwan/virtualwan.json':raise ValueError('Le bootstrap utilise /etc/virtualwan/virtualwan.json.')
        import base64,ipaddress
        router=cfg.get('router',{})
        try:
            tunnel=ipaddress.IPv4Interface(router['tunnel_address']);peer=ipaddress.IPv4Address(router['peer_address'])
            if tunnel.network.prefixlen!=30 or peer not in tunnel.network or peer==tunnel.ip or peer in (tunnel.network.network_address,tunnel.network.broadcast_address):raise ValueError()
            if len(base64.b64decode(router['peer_public_key'],validate=True))!=32:raise ValueError()
        except (KeyError,ValueError,TypeError):raise ValueError('Renseigner un /30 dédié, son peer et la clé publique du nouveau tunnel pfSense.')
        for other in self.store.rows('SELECT id FROM profiles WHERE id!=?',(key,)):
            address=self.store.document('profiles',other['id'])['config'].get('router',{}).get('tunnel_address')
            if address and tunnel.network.overlaps(ipaddress.IPv4Interface(address).network):raise domain.Conflict('Le tunnel chevauche celui d’un autre routeur.')
        with self.store.tx() as db:
            for row in db.execute("SELECT payload FROM jobs WHERE kind='router_setup' AND state IN ('queued','running','interrupted')"):
                if json.loads(row['payload']).get('profile_id')==key:raise domain.Conflict('Installation déjà en cours ou interrompue ; consulter le journal avant toute reprise.')
            job=self.store.job('router_setup',{'profile_id':key,'revision':p['revision'],'simulated':self.demo},db)
        self.store.event(job,'router.setup.requested',profile_id=key,service=cfg['ovh']['expected_service'],primary_ip=cfg['vps']['primary_ip'])
        return job

    def run_router_setup(self,key,payload):
        p=self.store.document('profiles',payload['profile_id'])
        if p['revision']!=payload['revision'] or self.demo!=payload['simulated']:raise domain.Conflict('Profil ou mode modifié après demande.')
        if self.demo:
            result={'simulated':True,'home_network_configured':False,'reconnected':True,'public_key':'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='}
        else:
            if not self.allow_provision:raise domain.Conflict('Installation réelle désactivée.')
            # Verify the primary is actually attached to the specified OVH service.
            from urllib.parse import quote
            actual=self.provider(p,key).call('GET','/vps/'+quote(p['config']['ovh']['expected_service'],safe='')+'/ips')
            if p['config']['vps']['primary_ip'] not in actual:raise domain.Conflict('IP primaire absente du service OVH déclaré.')
            cfg=deepcopy(p['config']);run=self.store.root/'runs'/key;run.mkdir(parents=True,exist_ok=True)
            cfg['log_directory']=str(run/'router');cfg['backup_key_env']='VWAN_BACKUP_KEY'
            cfg['vps']['known_hosts']=str(self.store.root/'ssh-known-hosts')
            if cfg['vps'].get('private_key'):
                private=(self.store.root/cfg['vps']['private_key']).resolve()
                if not private.is_relative_to(self.store.root):raise ValueError('Clé SSH hors data.')
                cfg['vps']['private_key']=str(private)
            cfg['router_setup']={**cfg['router'],'primary_ip':cfg['vps']['primary_ip'],'wireguard_interface':'wg-vwan',
                                 'additional_ips':[r['ip'] for r in self.store.rows('SELECT ip FROM ip_pool WHERE profile_id=?',(p['id'],))]}
            # Keep only fields accepted by the router installer.
            cfg['router_setup']={k:cfg['router_setup'][k] for k in ('primary_ip','wan_interface','wireguard_interface','listen_port','mtu','tunnel_address','peer_address','peer_public_key','additional_ips')}
            env=os.environ.copy()
            for section in cfg.values():
                if isinstance(section,dict):
                    for name,value in section.items():
                        if name.endswith('_env') and value:
                            credential=self.store.secret_get(value)
                            if credential:env[value]=credential
            env['VWAN_BACKUP_KEY']=(self.store.root/'master.key').read_text().strip()
            path=run/'router-setup.json';path.write_text(json.dumps(cfg));os.chmod(path,0o600)
            proc=subprocess.Popen([sys.executable,'-m','virtualwan.adapters.router_setup',str(path)],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            for line in proc.stdout:self.store.event(key,'router.setup.progress',message=line.strip())
            if proc.wait():raise RuntimeError('Installation interrompue ; vérifier le journal et le rollback avant une nouvelle action.')
            result=json.loads(path.with_suffix('.result.json').read_text())
        # Network at home must be verified separately; never mark it ready by SSH success.
        self.store.event(key,'router.setup.complete',**result)
        return result

    def run_deploy(self,key,payload):
        if bool(payload.get('simulated'))!=self.demo:raise domain.Conflict('Mode du job différent de celui du serveur ; ne pas rejouer une simulation en production.')
        cfg=deepcopy(payload['config']);ip=cfg['public_ip'];run=self.store.root/'runs'/key;run.mkdir(parents=True,exist_ok=True)
        config_path=run/'config.json';cfg['log_directory']=str(run/'provisioning');cfg['backup_key_env']='VWAN_BACKUP_KEY'
        if self.demo:
            cfg['pve']['vmid']=1000+len(self.deployments());cfg['pfsense']['next_hop']='10.254.251.'+str(10+len(self.deployments()))
            mac='02:00:'+':'.join(key[i:i+2] for i in (0,2,4,6))
            result={'simulated':True,'mac':mac,'vmid':cfg['pve']['vmid'],'next_hop':cfg['pfsense']['next_hop']}
        else:
            env=os.environ.copy()
            for section in cfg.values():
                if isinstance(section,dict):
                    for name,value in section.items():
                        if name.endswith('_env') and value:
                            credential=self.store.secret_get(value)
                            if credential:env[value]=credential
            env['VWAN_BACKUP_KEY']=(self.store.root/'master.key').read_text().strip()
            keyfile=run/'guest-key.pub';keyfile.write_text(payload.get('public_key',''));cfg['guest']['ssh_public_key_file']=str(keyfile)
            for item in ('vps','pve'):
                cfg[item]['known_hosts']=str(self.store.root/'ssh-known-hosts')
                if cfg[item].get('private_key'):
                    path=(self.store.root/cfg[item]['private_key']).resolve()
                    if not path.is_relative_to(self.store.root):raise ValueError('La clé privée doit être montée sous le répertoire data.')
                    cfg[item]['private_key']=str(path)
            if config_path.exists():
                saved=json.loads(config_path.read_text())
                if saved.get('public_ip')!=ip:raise domain.Conflict('Configuration du run en conflit.')
                cfg=saved
            config_path.write_text(json.dumps(cfg,indent=2))
            self.store.event(key,'provisioning.process.begin',configuration=cfg)
            process=subprocess.Popen([sys.executable,'-m','virtualwan.adapters.provision','apply','--config',str(config_path)],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            tail=[]
            for line in process.stdout:
                tail.append(line.strip());tail=tail[-30:]
                self.store.event(key,'provisioning.progress',message=line.strip())
            code=process.wait()
            cfg=json.loads(config_path.read_text())
            if code:raise RuntimeError('Provisionnement interrompu ; consulter le journal et les snapshots chiffrés. L’IP reste en quarantaine.')
            journals=list((run/'provisioning').glob('*/run.jsonl'))
            events=[json.loads(line) for line in journals[-1].read_text().splitlines()]
            created=next(e for e in events if e['action']=='remote.end' and e.get('operation')=='pve_create')
            # Remote journal payload uses result; fail closed if the adapter changes its schema.
            mac=created['result']['mac']
            result={'mac':mac,'vmid':cfg['pve']['vmid'],'next_hop':cfg['pfsense']['next_hop'],'journal':str(journals[-1].relative_to(self.store.root))}
        with self.store.tx() as db:
            record=db.execute('SELECT data FROM deployments WHERE ip=?',(ip,)).fetchone();info=json.loads(record['data'])
            info.update(result,status='simulated' if self.demo else 'active')
            db.execute('UPDATE deployments SET data=? WHERE ip=?',(json.dumps(info),ip))
            db.execute("UPDATE ip_pool SET state='assigned' WHERE ip=? AND job_id=?",(ip,key))
        self.store.event(key,'deployment.complete',public_ip=ip,**result);return result

    def process_one(self):
        with self.store.tx() as db:
            row=db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:return False
            row=dict(row);db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?",(time.time(),row['id']))
        key=row['id'];payload=json.loads(row['payload']);self.store.event(key,'job.started',kind=row['kind'])
        try:
            if row['kind']=='deploy':result=self.run_deploy(key,payload)
            elif row['kind']=='router_setup':result=self.run_router_setup(key,payload)
            elif row['kind']=='sync':result=self.sync(payload['profile_id'],key)
            elif row['kind']=='inspect':result=self.inspect_clients(payload['profile_id'],key)
            elif row['kind']=='delivery':result=self.follow_delivery(payload['purchase_id'],key)
            elif row['kind']=='quote':
                if self.store.document('purchases',payload['purchase_id'])['state']!='preparing':raise domain.Conflict('Devis annulé avant préparation.')
                p=self.store.document('profiles',payload['profile_id']);result=self.provider(p,key).prepare(p,payload)
                domain.amount(result['supplier_quote'])
                if result['quote_digest']!=domain.digest(domain.quote_binding(result['supplier_quote'])):raise domain.Conflict('Empreinte du devis fournisseur invalide.')
                purchase=self.store.document('purchases',payload['purchase_id']);purchase.update(result)
                with self.store.tx() as db:
                    if db.execute('SELECT state FROM purchases WHERE id=?',(purchase['id'],)).fetchone()['state']!='preparing':raise domain.Conflict('Devis annulé pendant préparation.')
                    db.execute("UPDATE purchases SET state='awaiting_approval',data=? WHERE id=?",(json.dumps(purchase),purchase['id']))
                self.store.event(purchase['id'],'purchase.quote.ready',amount=domain.amount(result['supplier_quote']),requires_human_approval=True)
                self.notify(purchase['id'])
            else:raise ValueError('Job non pris en charge.')
            self.job_state(key,'succeeded',result)
        except Exception as error:
            self.store.event(key,'job.failed',error_type=type(error).__name__,error=str(error))
            self.job_state(key,'failed',{'error':str(error),'recovery':'Consulter les événements. Aucun retrait ou nouvel achat automatique.'})
            with self.store.tx() as db:
                if row['kind']=='deploy':
                    db.execute("UPDATE ip_pool SET state='quarantined' WHERE job_id=?",(key,))
                    for r in db.execute('SELECT id,data FROM deployments').fetchall():
                        data=json.loads(r['data'])
                        if data.get('job_id')==key:data['status']='failed';db.execute('UPDATE deployments SET data=? WHERE id=?',(json.dumps(data),r['id']))
                if row['kind']=='quote':db.execute("UPDATE purchases SET state='failed' WHERE id=? AND state='preparing'",(payload['purchase_id'],))
        return True

    def notify(self,purchase_id):
        host=os.environ.get('VWAN_SMTP_HOST');recipient=os.environ.get('VWAN_APPROVAL_EMAIL')
        if not host or not recipient:return
        import smtplib
        from email.message import EmailMessage
        message=EmailMessage();message['From']=os.environ['VWAN_SMTP_FROM'];message['To']=recipient;message['Subject']='Virtualwan : achat en attente de confirmation'
        message.set_content('Un devis est prêt. Connectez-vous à l’interface pour examiner les prix/contrats et confirmer.\n'+os.environ.get('VWAN_PUBLIC_URL','http://localhost:8080')+'/#purchases\nAucun achat n’est approuvé depuis un lien GET. Référence : '+purchase_id)
        try:
            with smtplib.SMTP(host,int(os.environ.get('VWAN_SMTP_PORT','587')),timeout=20) as smtp:
                smtp.starttls()
                if os.environ.get('VWAN_SMTP_USER'):smtp.login(os.environ['VWAN_SMTP_USER'],os.environ['VWAN_SMTP_PASSWORD'])
                smtp.send_message(message)
            self.store.event(purchase_id,'approval.email.sent',recipient=recipient)
        except Exception as error:self.store.event(purchase_id,'approval.email.failed',error_type=type(error).__name__)

    def start(self):
        def loop():
            last_poll=0
            while not self.stop.is_set():
                if not self.demo and time.time()-last_poll>300:
                    last_poll=time.time()
                    for purchase in self.store.rows("SELECT id FROM purchases WHERE state IN ('awaiting_payment','awaiting_delivery')"):
                        pending=self.store.rows("SELECT payload FROM jobs WHERE kind='delivery' AND state IN ('queued','running')")
                        if not any(json.loads(j['payload']).get('purchase_id')==purchase['id'] for j in pending):self.store.job('delivery',{'purchase_id':purchase['id']})
                try:worked=self.process_one()
                except Exception:worked=False
                if not worked:self.stop.wait(1)
        self.thread=threading.Thread(target=loop,name='virtualwan-worker',daemon=True);self.thread.start()
