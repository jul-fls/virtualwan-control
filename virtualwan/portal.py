"""Client routes: a distinct session and ownership check precede every VM action."""
from contextlib import contextmanager
import hashlib
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import quote
import dns.resolver
import requests
from flask import abort,g,jsonify,request,render_template
from . import domain
from .accounts import Accounts,email,password_hash
from .adapters.client_pve import ClientPVE,validate_rules
from .store import ident


def install_portal(app,store,control,origin):
    accounts=Accounts(store,origin);app.extensions['accounts']=accounts
    locks={};lock_guard=threading.Lock();attempts={}
    def throttle(name):
        key=(request.remote_addr,hashlib.sha256(str(name).lower().encode()).hexdigest());now=time.time()
        with lock_guard:
            if len(attempts)>5000:
                for k in list(attempts):
                    if not attempts[k] or attempts[k][-1]<now-300:attempts.pop(k,None)
            times=[t for t in attempts.get(key,[]) if t>now-300]
            ipkey=(request.remote_addr,'all');alltimes=[t for t in attempts.get(ipkey,[]) if t>now-300]
            if len(times)>=8 or len(alltimes)>=50:abort(429)
            attempts[key]=times+[now];attempts[ipkey]=alltimes+[now]
    def body():return request.get_json()
    def owner(key):
        rows=store.rows('SELECT d.*,v.display_name,v.description FROM deployments d JOIN client_vms v ON v.deployment_id=d.id WHERE d.id=? AND v.account_id=?',(key,g.client['account_id']))
        if not rows:abort(404)
        row=rows[0];return {**json.loads(row['data']),**{k:v for k,v in row.items() if k!='data'}}
    def account_view():
        row=store.one('SELECT id,email,name,totp FROM accounts WHERE id=?',(g.client['account_id'],))
        row['mfa_enabled']=bool(row.pop('totp'));row['passkeys']=store.rows('SELECT id,name,created_at FROM passkeys WHERE account_id=?',(row['id'],))
        return row
    def public_vm(d):
        return {k:d.get(k) for k in ('id','fqdn','ip','public_ipv6','status','display_name','description','reverse_dns','rescue_state','memory_mb','cores','disk_gb')}
    def login_response(result):
        if not result:abort(401)
        response=jsonify(csrf=result['csrf']);response.set_cookie('vwan_client',result['token'],max_age=28800,httponly=True,secure=origin.startswith('https://'),samesite='Strict',path='/');return response
    @contextmanager
    def operation(key,action):
        d=owner(key)
        if not control.demo and not control.allow_provision:raise domain.Conflict('Les opérations sur les VM sont désactivées par l’administrateur.')
        if d.get('status') not in ('active','stopped','imported','simulated'):raise domain.Conflict('Ce VPS est actuellement indisponible pour cette opération.')
        with lock_guard:lock=locks.setdefault(key,threading.Lock())
        if not lock.acquire(blocking=False):raise domain.Conflict('Une autre opération est en cours sur ce VPS.')
        run=ident();account=g.client['account_id']
        try:
            d=owner(key)
            if d.get('status') not in ('active','stopped','imported','simulated'):raise domain.Conflict('Une opération d’administration est en cours.')
            with store.tx() as db:db.execute('INSERT INTO client_operations VALUES(?,?,?,?,?,?,?)',(run,account,key,action,'running','{}',time.time()))
            store.event(run,'client.operation.started',account_id=account,deployment_id=key,operation=action)
            d['_operation_run']=run
            yield d,run
        except BaseException as error:
            with store.tx() as db:db.execute("UPDATE client_operations SET state='failed',result=? WHERE id=?",(json.dumps({'error':'Opération refusée ou interrompue.'}),run))
            store.event(run,'client.operation.failed',error_type=type(error).__name__)
            raise
        else:
            with store.tx() as db:
                result=json.loads(db.execute('SELECT result FROM client_operations WHERE id=?',(run,)).fetchone()['result'])
                state='submitted' if result.get('task') else 'succeeded'
                db.execute('UPDATE client_operations SET state=? WHERE id=?',(state,run))
            store.event(run,'client.operation.'+('submitted' if state=='submitted' else 'complete'),operation=action)
        finally:lock.release()
    @contextmanager
    def backend(d):
        factory=app.config.get('CLIENT_PVE_FACTORY',ClientPVE)
        try:
            with factory(store,store.document('profiles',d['profile_id']),d) as remote:yield remote
        except domain.Conflict:raise
        except Exception as error:
            trace=d.get('_operation_run') or ident()
            store.event(trace,'client.infrastructure.failed',deployment_id=d['id'],error_type=type(error).__name__,error=str(error))
            raise domain.Conflict('Accès à l’infrastructure indisponible. Contacter l’hébergeur avec la référence '+trace+'.') from error
    def update_vm(key,**values):
        with store.tx() as db:
            row=db.execute('SELECT data FROM deployments WHERE id=?',(key,)).fetchone()
            if not row:raise KeyError(key)
            data=json.loads(row['data']);data.update(values);db.execute('UPDATE deployments SET data=? WHERE id=?',(json.dumps(data),key))
    @app.get('/client')
    @app.get('/client/')
    def client_index():return render_template('client.html')
    @app.post('/api/portal/login')
    def client_login():
        b=body();throttle(b.get('email'));return login_response(accounts.login(b.get('email'),b.get('password'),b.get('code')))
    @app.post('/api/portal/passkey/login/begin')
    def client_passkey_begin():
        b=body();throttle(b.get('email'))
        try:address=email(b.get('email'))
        except ValueError:address=''
        rows=store.rows('SELECT id FROM accounts WHERE email=? AND enabled=1',(address,))
        return jsonify(accounts.passkey_begin(rows[0]['id'] if rows else 'unknown'))
    @app.post('/api/portal/passkey/login/complete')
    def client_passkey_complete():
        throttle('passkey-complete')
        try:return login_response(accounts.passkey_complete(body()))
        except (ValueError,domain.Conflict,KeyError):abort(401)
    @app.get('/api/portal/state')
    def client_state():
        clients=[public_vm({**json.loads(r['data']),**{k:v for k,v in r.items() if k!='data'}}) for r in store.rows('SELECT d.*,v.display_name,v.description FROM deployments d JOIN client_vms v ON v.deployment_id=d.id WHERE v.account_id=?',(g.client['account_id'],))]
        return jsonify(account=account_view(),csrf=g.client['csrf'],vps=clients,demo=control.demo,rescue_available=bool(os.environ.get('VWAN_RESCUE_ISO')),operations=store.rows('SELECT id,deployment_id,action,state,created_at FROM client_operations WHERE account_id=? ORDER BY created_at DESC LIMIT 50',(g.client['account_id'],)))
    @app.post('/api/portal/logout')
    def client_logout():
        with store.tx() as db:db.execute('DELETE FROM client_sessions WHERE token=?',(g.client['token'],))
        r=jsonify(ok=True);r.delete_cookie('vwan_client',path='/');return r
    @app.put('/api/portal/account')
    def client_account():
        b=body();throttle('reauth:'+g.client['account_id']);accounts.reauth(g.client,b);new_email=email(b['email']);name=domain.label(b['name'])
        with store.tx() as db:
            if db.execute('SELECT 1 FROM accounts WHERE email=? AND id!=?',(new_email,g.client['account_id'])).fetchone():raise domain.Conflict('Cet e-mail est déjà utilisé.')
            db.execute('UPDATE accounts SET email=?,name=? WHERE id=?',(new_email,name,g.client['account_id']))
        store.event(ident(),'client.account.updated',account_id=g.client['account_id']);return jsonify(ok=True)
    @app.put('/api/portal/password')
    def client_password():
        b=body();throttle('reauth:'+g.client['account_id']);accounts.reauth(g.client,b);hashed=password_hash(b.get('password'))
        with store.tx() as db:
            db.execute('UPDATE accounts SET password_hash=? WHERE id=?',(hashed,g.client['account_id']));db.execute('DELETE FROM client_sessions WHERE account_id=?',(g.client['account_id'],))
        store.event(ident(),'client.password.changed',account_id=g.client['account_id']);return jsonify(ok=True,login_required=True)
    @app.post('/api/portal/sessions/revoke')
    def revoke_sessions():
        with store.tx() as db:db.execute('DELETE FROM client_sessions WHERE account_id=? AND token!=?',(g.client['account_id'],g.client['token']))
        return jsonify(ok=True)
    @app.post('/api/portal/mfa/begin')
    def client_mfa_begin():throttle('reauth:'+g.client['account_id']);return jsonify(accounts.begin_totp(g.client,body()))
    @app.post('/api/portal/mfa/complete')
    def client_mfa_complete():throttle('mfa:'+g.client['account_id']);return jsonify(accounts.complete_totp(g.client,body()))
    @app.delete('/api/portal/mfa')
    def client_mfa_remove():
        b=body();throttle('reauth:'+g.client['account_id']);accounts.reauth(g.client,b)
        with store.tx() as db:
            db.execute("UPDATE accounts SET totp=NULL,last_totp=-1,recovery='[]' WHERE id=?",(g.client['account_id'],));db.execute('DELETE FROM client_sessions WHERE account_id=? AND token!=?',(g.client['account_id'],g.client['token']))
        store.event(ident(),'client.mfa.disabled',account_id=g.client['account_id']);return jsonify(ok=True)
    @app.post('/api/portal/passkey/register/begin')
    def register_begin():
        throttle('reauth:'+g.client['account_id']);accounts.reauth(g.client,body());return jsonify(accounts.passkey_begin(g.client['account_id'],g.client))
    @app.post('/api/portal/passkey/register/complete')
    def register_complete():return jsonify(accounts.passkey_complete(body(),g.client))
    @app.delete('/api/portal/passkeys/<key>')
    def delete_passkey(key):
        throttle('reauth:'+g.client['account_id']);accounts.reauth(g.client,body())
        with store.tx() as db:db.execute('DELETE FROM passkeys WHERE id=? AND account_id=?',(key,g.client['account_id']))
        store.event(ident(),'client.passkey.removed',account_id=g.client['account_id']);return jsonify(ok=True)
    @app.put('/api/portal/vps/<key>')
    def client_metadata(key):
        owner(key);b=body();name=domain.label(b.get('display_name') or 'Mon VPS');description=str(b.get('description','')).strip()
        if len(description)>2000:raise ValueError('Description : 2000 caractères maximum.')
        with store.tx() as db:db.execute('UPDATE client_vms SET display_name=?,description=? WHERE deployment_id=? AND account_id=?',(name,description,key,g.client['account_id']))
        store.event(ident(),'client.vps.metadata.updated',account_id=g.client['account_id'],deployment_id=key,display_name=name,description=description)
        return jsonify(ok=True)
    @app.get('/api/portal/vps/<key>/status')
    def client_status(key):
        d=owner(key)
        if control.demo:return jsonify(status='running' if d.get('power_state','running')=='running' else 'stopped',uptime=68400,cpu=.04,mem=512*1024**2,maxmem=2048*1024**2,cpus=2,netin=4211264,netout=2123456)
        with backend(d) as pve:return jsonify(pve.status())
    @app.post('/api/portal/vps/<key>/power')
    def client_power(key):
        b=body();action=b.get('action')
        if action not in ('start','shutdown','reboot','stop','reset'):raise ValueError('Action inconnue.')
        if action in ('stop','reset') and b.get('confirmation')!='CONFIRMER':raise ValueError('Confirmer cette action matérielle.')
        with operation(key,'power.'+action) as (d,run):
            if control.demo:result={'simulated':True};update_vm(key,power_state='stopped' if action in ('shutdown','stop') else 'running')
            else:
                with backend(d) as pve:result=pve.task(action)
            with store.tx() as db:db.execute('UPDATE client_operations SET result=? WHERE id=?',(json.dumps(result),run))
        return jsonify(operation_id=run,task=bool(result.get('task')),simulated=bool(result.get('simulated')))
    @app.get('/api/portal/operations/<run>')
    def operation_status(run):
        r=store.one('SELECT * FROM client_operations WHERE id=? AND account_id=?',(run,g.client['account_id']));result=json.loads(r['result'])
        if result.get('task'):
            d=owner(r['deployment_id'])
            with backend(d) as pve:result.update(pve.task_status(result['task']))
        state=r['state']
        if result.get('status')=='stopped':
            state='succeeded' if result.get('exitstatus')=='OK' else 'failed'
            with store.tx() as db:db.execute('UPDATE client_operations SET state=?,result=? WHERE id=?',(state,json.dumps(result),run))
            if r['state']!=state:store.event(run,'client.task.finished',state=state,exitstatus=result.get('exitstatus'))
        return jsonify(state=state,result={k:result[k] for k in ('status','exitstatus','simulated') if k in result})
    @app.get('/api/portal/vps/<key>/firewall')
    def get_firewall(key):
        d=owner(key)
        if control.demo:return jsonify(d.get('client_firewall',{'enabled':False,'cluster_enabled':True,'nic_enabled':False,'policy_in':'ACCEPT','rules':[],'other_rules':0}))
        with backend(d) as pve:return jsonify(pve.firewall())
    @app.put('/api/portal/vps/<key>/firewall')
    def put_firewall(key):
        b=body();rules=validate_rules(b.get('rules',[]))
        if b.get('confirmation')!='APPLIQUER':raise ValueError('Confirmer le changement de pare-feu.')
        with operation(key,'firewall') as (d,run):
            if control.demo:
                result={**b,'rules':rules,'cluster_enabled':True,'other_rules':0};result.pop('confirmation',None);update_vm(key,client_firewall=result)
            else:
                with backend(d) as pve:result=pve.set_firewall(b)
        return jsonify(result)
    @app.put('/api/portal/vps/<key>/reverse')
    def client_reverse(key):
        b=body();name=str(b.get('hostname','')).lower().rstrip('.')
        if len(name)>253 or '.' not in name or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',v) for v in name.split('.')):raise ValueError('Nom DNS invalide.')
        import ipaddress
        try:ipaddress.ip_address(name)
        except ValueError:pass
        else:raise ValueError('Un nom DNS est requis, pas une adresse IP.')
        with operation(key,'reverse_dns') as (d,run):
            if not d.get('ip'):raise domain.Conflict('Aucune IPv4 publique affectée.')
            if not control.demo:
                resolver=dns.resolver.Resolver(configure=False);resolver.nameservers=['1.1.1.1','9.9.9.9']
                try:answers={str(v) for v in resolver.resolve(name,'A',lifetime=8)}
                except dns.exception.DNSException:raise domain.Conflict('Créer d’abord un enregistrement DNS A vers votre IPv4.')
                if d['ip'] not in answers:raise domain.Conflict('Le DNS A doit pointer vers votre IPv4 avant de changer le reverse.')
                p=store.document('profiles',d['profile_id']);ovh=control.provider(p,run)
                block=next((v for v in ovh.call('GET','/ip') if v==d['ip']+'/32' or v==d['ip']),None)
                if not block:raise domain.Conflict('Additional IP /32 introuvable.')
                path='/ip/'+quote(block,safe='');info=ovh.call('GET',path)
                if (info.get('routedTo') or {}).get('serviceName')!=p['config']['ovh'].get('expected_service'):raise domain.Conflict('L’IP a changé de routeur.')
                ptr=path+'/reverse/'+quote(d['ip'],safe='')
                try:old=ovh.call('GET',ptr)
                except requests.HTTPError as e:
                    if e.response.status_code!=404:raise
                    old=None
                if old:ovh.call('DELETE',ptr)
                try:
                    ovh.call('POST',path+'/reverse',{'ipReverse':d['ip'],'reverse':name})
                except Exception:
                    if old:ovh.call('POST',path+'/reverse',{'ipReverse':d['ip'],'reverse':old['reverse']})
                    raise
                if ovh.call('GET',ptr).get('reverse','').rstrip('.')!=name:raise domain.Conflict('OVH n’a pas confirmé le nouveau reverse.')
            update_vm(key,reverse_dns=name);store.event(run,'client.reverse.updated',hostname=name,ip=d['ip'])
        return jsonify(ok=True)
    @app.post('/api/portal/vps/<key>/graphical')
    def graphical(key):
        with operation(key,'graphical') as (d,run):
            if control.demo:result={'ok':True,'simulated':True}
            else:
                with backend(d) as pve:result=pve.graphical()
        return jsonify(result)
    @app.post('/api/portal/vps/<key>/rescue')
    def rescue(key):
        b=body()
        if b.get('confirmation')!='CONFIRMER':raise ValueError('Confirmer le changement de démarrage.')
        with operation(key,'rescue') as (d,run):
            disable=b.get('disable') is True;saved=d.get('rescue_state')
            if disable and not saved:raise domain.Conflict('Mode rescue inactif.')
            if not disable and saved:raise domain.Conflict('Mode rescue déjà actif.')
            if control.demo:result=None if disable else {'boot':'order=scsi0','ide0':'demo:iso/rescue.iso,media=cdrom'}
            else:
                with backend(d) as pve:result=pve.rescue(os.environ.get('VWAN_RESCUE_ISO',''),disable,saved)
            update_vm(key,rescue_state=None if disable else result)
        return jsonify(ok=True)
    @app.post('/api/portal/vps/<key>/console')
    def console_ticket(key):
        d=owner(key)
        if not control.demo and not control.allow_provision:raise domain.Conflict('Console désactivée par l’administrateur.')
        ticket=accounts.challenge(g.client['account_id'],'console',{'deployment_id':key,'session':g.client['token'],'password':secrets.token_urlsafe(24)},60)
        data=json.loads(store.one('SELECT data FROM client_challenges WHERE id=?',(ticket,))['data'])
        return jsonify(ticket=ticket,password=data['password'],simulated=control.demo)
    @app.get('/api/accounts')
    def account_list():return jsonify(accounts=accounts.list(),assignments=store.rows('SELECT * FROM client_vms'))
    @app.post('/api/accounts')
    def account_create():return jsonify(id=accounts.save(body())),201
    @app.put('/api/accounts/<key>')
    def account_update(key):store.one('SELECT id FROM accounts WHERE id=?',(key,));return jsonify(id=accounts.save(body(),key))
    @app.post('/api/accounts/<key>/reset-auth')
    def reset_auth(key):
        if body().get('confirmation')!='REINITIALISER':raise ValueError('Saisir REINITIALISER pour confirmer.')
        accounts.reset_mfa(key);return jsonify(ok=True)
    @app.put('/api/deployments/<key>/account')
    def assign(key):
        account=body().get('account_id')
        with store.tx() as db:
            deployment=store.one('SELECT id FROM deployments WHERE id=?',(key,))
            if db.execute("SELECT 1 FROM client_operations WHERE deployment_id=? AND state='running'",(key,)).fetchone():raise domain.Conflict('Une opération cliente est en cours.')
            if account:store.one('SELECT id FROM accounts WHERE id=?',(account,))
            db.execute('DELETE FROM client_vms WHERE deployment_id=?',(key,))
            if account:db.execute('INSERT INTO client_vms(deployment_id,account_id) VALUES(?,?)',(key,account))
            db.execute("DELETE FROM client_challenges WHERE purpose='console'")
        store.event(ident(),'deployment.account.assigned',deployment_id=key,account_id=account);return jsonify(ok=True)
