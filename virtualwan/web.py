from collections import defaultdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlparse
from flask import Flask,abort,g,jsonify,render_template,request,Response
from werkzeug.security import check_password_hash,generate_password_hash
from . import __version__,domain
from .control import Control
from .store import Store,ident


def create_app(test_config=None):
    app=Flask(__name__);app.config.update(MAX_CONTENT_LENGTH=1024*1024)
    app.config.update(test_config or {})
    demo=app.config.get('DEMO',os.environ.get('VWAN_DEMO','false').lower()=='true')
    password=app.config.get('ADMIN_PASSWORD',os.environ.get('VWAN_ADMIN_PASSWORD',''))
    if len(password)<12 or password.startswith('REPLACE_'):raise ValueError('VWAN_ADMIN_PASSWORD doit être personnalisé et contenir au moins 12 caractères.')
    username=os.environ.get('VWAN_ADMIN_USER','admin')
    store=Store(app.config.get('DATA_DIR',os.environ.get('VWAN_DATA_DIR','data')))
    with store.tx() as db:
        previous=db.execute("SELECT value FROM settings WHERE name='admin_auth'").fetchone()
        prior=json.loads(previous['value']) if previous else None
        if prior and prior['username']==username and check_password_hash(prior['hash'],password):
            hashed=prior['hash']
        else:
            hashed=generate_password_hash(password)
            db.execute('DELETE FROM sessions')
            db.execute("INSERT OR REPLACE INTO settings VALUES('admin_auth',?)",(json.dumps({'username':username,'hash':hashed}),))
        current=db.execute("SELECT value FROM settings WHERE name='mode'").fetchone()
        mode='demo' if demo else 'live'
        if current and current['value']!=mode:raise ValueError('Utiliser un volume distinct pour les modes démo et réel.')
        db.execute("INSERT OR REPLACE INTO settings VALUES('mode',?)",(mode,))
    store.redactions.add(password)
    control=Control(store,demo,os.environ.get('VWAN_ENABLE_PURCHASES')=='true',os.environ.get('VWAN_ENABLE_PROVISIONING')=='true',app.config.get('PROVIDER_FACTORY'))
    from .environment import apply_environment
    apply_environment(control)
    app.extensions.update(store=store,control=control)
    url=os.environ.get('VWAN_PUBLIC_URL','http://localhost:8080').rstrip('/')
    public=urlparse(url)
    if public.scheme not in ('http','https') or not public.hostname or public.username or public.password or public.query or public.fragment or public.path not in ('','/'):
        raise ValueError('VWAN_PUBLIC_URL doit être une URL HTTP(S) sans chemin ni identifiants.')
    allowed={public.hostname,'localhost','127.0.0.1'}
    secure=public.scheme=='https';attempts=defaultdict(list)
    def session_id():return hashlib.sha256(request.cookies.get('vwan_session','').encode()).hexdigest()
    def origin_check():
        origin=request.headers.get('Origin')
        if origin and origin.rstrip('/') not in (url,request.host_url.rstrip('/')):abort(403)
        if request.headers.get('Sec-Fetch-Site')=='cross-site':abort(403)

    @app.before_request
    def guard():
        if urlparse('http://'+request.host).hostname not in allowed:abort(400)
        if request.method not in ('GET','HEAD','OPTIONS'):origin_check()
        if not request.path.startswith('/api/') or request.path=='/api/login':return
        rows=store.rows('SELECT * FROM sessions WHERE token=? AND expires>?',(session_id(),time.time()))
        if not rows:abort(401)
        g.session=rows[0]
        if request.method not in ('GET','HEAD'):
            csrf=request.headers.get('X-CSRF-Token','')
            if not csrf or not hmac.compare_digest(csrf,g.session['csrf']):abort(403)
            if not request.is_json:abort(415)

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
                                 'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if secure:response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    @app.errorhandler(domain.Conflict)
    def conflict(error):return jsonify(error=store.clean(str(error))),409
    @app.errorhandler(ValueError)
    def invalid(error):return jsonify(error=store.clean(str(error))),400
    @app.errorhandler(KeyError)
    def missing(error):return jsonify(error='Objet ou champ requis introuvable.'),404
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(400)
    @app.errorhandler(415)
    @app.errorhandler(429)
    def http_error(error):return jsonify(error=error.description),error.code
    @app.errorhandler(Exception)
    def server_error(error):
        from werkzeug.exceptions import HTTPException
        if isinstance(error,HTTPException):return jsonify(error=error.description),error.code
        trace=ident();store.event(trace,'api.failed',error_type=type(error).__name__)
        return jsonify(error='Opération refusée ou fournisseur indisponible.',reference=trace),502

    @app.get('/')
    def index():return render_template('index.html')
    @app.get('/health')
    def health():return jsonify(ok=True,version=__version__)
    @app.post('/api/login')
    def login():
        address=request.remote_addr or 'unknown';now=time.time()
        attempts[address]=[t for t in attempts[address] if t>now-300]
        if len(attempts[address])>=8:abort(429)
        body=request.get_json();attempts[address].append(now)
        supplied=body.get('password','')
        valid=check_password_hash(hashed,supplied)
        if not valid or body.get('username')!=username:abort(401)
        attempts.pop(address,None);token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
        with store.tx() as db:
            db.execute('DELETE FROM sessions WHERE expires<?',(now,))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,now+28800,username))
        response=jsonify(csrf=csrf,user=username);response.set_cookie('vwan_session',token,max_age=28800,httponly=True,secure=secure,samesite='Strict',path='/')
        store.event(ident(),'admin.login',user=username);return response
    @app.post('/api/logout')
    def logout():
        with store.tx() as db:db.execute('DELETE FROM sessions WHERE token=?',(session_id(),))
        response=jsonify(ok=True);response.delete_cookie('vwan_session');return response
    @app.get('/api/session')
    def session():return jsonify(user=g.session['user'],csrf=g.session['csrf'])

    @app.get('/api/state')
    def state():
        with store.tx():
            control.expire_quotes()
            profiles=[store.document('profiles',r['id']) for r in store.rows('SELECT id FROM profiles')]
            models=[store.document('models',r['id']) for r in store.rows('SELECT id FROM models')]
            jobs=store.rows('SELECT id,kind,state,result,created_at,updated_at FROM jobs ORDER BY created_at DESC LIMIT 100')
            for job in jobs:job['result']=json.loads(job['result'])
            purchases=[store.document('purchases',r['id']) for r in store.rows('SELECT id FROM purchases ORDER BY created_at DESC LIMIT 100')]
            return jsonify(version=__version__,demo=demo,features={'purchases':control.allow_purchase or demo,'provisioning':control.allow_provision or demo},profiles=profiles,models=models,
                           ips=store.rows('SELECT * FROM ip_pool ORDER BY profile_id,ip'),deployments=control.deployments(),jobs=jobs,purchases=purchases,secret_names=[r['name'] for r in store.rows('SELECT name FROM secrets')])
    @app.post('/api/profiles')
    def profile_create():return jsonify(id=control.add_profile(request.get_json())),201
    @app.put('/api/profiles/<key>')
    def profile_update(key):store.document('profiles',key);return jsonify(id=control.add_profile(request.get_json(),key))
    @app.post('/api/profiles/<key>/clone')
    def profile_clone(key):
        body=request.get_json();return jsonify(id=control.clone_profile(key,body['name'],body['network'],body['gateway'])),201
    @app.post('/api/models')
    def model_create():return jsonify(id=control.add_model(request.get_json())),201
    @app.put('/api/models/<key>')
    def model_update(key):store.document('models',key);return jsonify(id=control.add_model(request.get_json(),key))
    @app.post('/api/ips')
    def ip_create():
        body=request.get_json();control.add_ip(body['profile_id'],body['ip']);return jsonify(ok=True),201
    @app.post('/api/profiles/<key>/sync')
    def sync(key):store.document('profiles',key);return jsonify(job_id=store.job('sync',{'profile_id':key})),202
    @app.post('/api/profiles/<key>/inspect')
    def inspect(key):store.document('profiles',key);return jsonify(job_id=store.job('inspect',{'profile_id':key})),202
    @app.post('/api/profiles/<key>/setup-router')
    def setup_router(key):return jsonify(job_id=control.request_router_setup(key,request.get_json())),202
    @app.post('/api/deployments')
    def deploy():return jsonify(job_id=control.request_deploy(request.get_json())),202
    @app.post('/api/deployments/<key>/retire')
    def retire(key):return jsonify(job_id=control.request_retire(key,request.get_json())),202
    @app.post('/api/ips/<ip>/prepare')
    def prepare_ip(ip):return jsonify(job_id=control.request_stock(ip)),202
    @app.put('/api/secrets/<name>')
    def secret_save(name):
        store.secret_set(name,request.get_json()['value']);store.event(ident(),'secret.saved',name=name);return jsonify(ok=True)
    @app.get('/api/catalogue/<key>/<kind>')
    def catalogue(key,kind):
        if kind not in ('router','additional_ip'):raise ValueError('Type invalide.')
        p=store.document('profiles',key);run=ident()
        return jsonify(offers=control.provider(p,run).catalogue(p,kind))
    @app.post('/api/purchases')
    def purchase():return jsonify(**control.request_quote(request.get_json())),202
    @app.get('/api/catalogue/<key>/router/options')
    def catalogue_options(key):
        p=store.document('profiles',key)
        return jsonify(options=control.provider(p,ident()).option_catalogue(p,request.args['planCode']))
    @app.post('/api/catalogue/<key>/configure')
    def catalogue_configure(key):
        p=store.document('profiles',key);body=request.get_json()
        if body.get('kind') not in ('router','additional_ip'):raise ValueError('Type de produit invalide.')
        body['quantity']=domain.positive(body.get('quantity',1),16 if body['kind']=='additional_ip' else 1)
        return jsonify(**control.provider(p,ident()).describe(p,body))
    @app.post('/api/purchases/<key>/approve')
    def approve(key):return jsonify(**control.approve(key,request.get_json(),g.session['user']))
    @app.post('/api/purchases/<key>/cancel')
    def cancel(key):
        with store.tx() as db:
            row=db.execute('SELECT state FROM purchases WHERE id=?',(key,)).fetchone()
            if not row:raise KeyError(key)
            if row['state'] not in ('preparing','awaiting_approval'):raise domain.Conflict('Une commande soumise à OVH ne peut pas être annulée localement.')
            db.execute("UPDATE purchases SET state='cancelled' WHERE id=?",(key,))
        store.event(key,'purchase.cancelled',user=g.session['user']);return jsonify(ok=True)
    @app.post('/api/purchases/<key>/refresh-delivery')
    def delivery(key):store.document('purchases',key);return jsonify(job_id=store.job('delivery',{'purchase_id':key})),202
    @app.post('/api/purchases/<key>/reconcile-order')
    def reconcile(key):return jsonify(**control.reconcile_order(key,request.get_json(),g.session['user']))
    @app.post('/api/purchases/<key>/resolve-no-order')
    def resolve_no_order(key):
        body=request.get_json()
        if body.get('confirmation')!='AUCUNE COMMANDE '+key[:8] or body.get('checked_supplier') is not True:raise ValueError('Vérification explicite chez OVH et confirmation requises.')
        with store.tx() as db:
            row=db.execute('SELECT state,data FROM purchases WHERE id=?',(key,)).fetchone()
            if not row:raise KeyError(key)
            if row['state']!='uncertain':raise domain.Conflict('Demande non incertaine.')
            data=json.loads(row['data']);data['reconciliation']={'user':g.session['user'],'time':time.time(),'operator_verified_no_supplier_order':True}
            db.execute("UPDATE purchases SET state='cancelled',data=? WHERE id=?",(json.dumps(data),key))
        store.event(key,'purchase.no-order.human-verified',user=g.session['user'],checkout_retried=False)
        return jsonify(ok=True)
    @app.get('/api/events/<key>')
    def events(key):
        if not domain.re.fullmatch(r'[a-f0-9]{32}',key):raise ValueError('ID invalide.')
        rows=store.rows('SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT 250',(key,))
        for row in rows:row['data']=json.loads(row['data'])
        from datetime import datetime
        subevents=[]
        for source in sorted((store.root/'runs'/key/'provisioning').glob('*/run.jsonl')):
            for line in source.read_text(encoding='utf-8').splitlines()[-250:]:
                record=json.loads(line)
                subevents.append({'time':datetime.fromisoformat(record['time']).timestamp(),'action':'provisioning.'+record['action'],
                                  'data':store.clean({k:v for k,v in record.items() if k not in ('time','action')})})
        return jsonify(events=sorted(rows[::-1]+subevents,key=lambda x:x['time']))
    @app.post('/api/jobs/<key>/retry')
    def retry(key):return jsonify(control.recover_job(key,'retry',request.get_json().get('confirmation'))),202
    @app.post('/api/jobs/<key>/cancel')
    def cancel_job(key):return jsonify(control.recover_job(key,'cancel',request.get_json().get('confirmation'))),200
    @app.get('/api/profile-example')
    def example():return jsonify(json.loads((Path(__file__).parent.parent/'examples'/'profile.json').read_text()))
    @app.get('/api/profiles/<key>/bootstrap')
    def bootstrap(key):
        from .bootstrap import bundle
        p=store.document('profiles',key);ips=store.rows('SELECT ip FROM ip_pool WHERE profile_id=?',(key,))
        return Response(bundle(p,ips),mimetype='application/zip',headers={'Content-Disposition':f'attachment; filename="virtualwan-onboarding-{key[:8]}.zip"'})

    if demo and not store.rows('SELECT id FROM profiles'):
        from .seed import seed
        seed(control)

    @app.post('/api/profiles/<key>/ssh-host-key')
    def host_key(key):
        import base64
        import paramiko
        p=store.document('profiles',key);body=request.get_json();target=body['target']
        if target not in ('vps','pve'):raise ValueError('Cible invalide.')
        cfg=p['config'][target];transport=paramiko.Transport((cfg['host'],int(cfg.get('port',22))))
        try:transport.start_client(timeout=10);public_key=transport.get_remote_server_key()
        finally:transport.close()
        fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(public_key.asbytes()).digest()).decode().rstrip('=')
        if not body.get('fingerprint'):return jsonify(fingerprint=fingerprint,algorithm=public_key.get_name(),saved=False)
        if body['fingerprint']!=fingerprint:raise domain.Conflict('L’empreinte du serveur a changé.')
        known=store.root/'ssh-known-hosts';hosts=paramiko.HostKeys()
        if known.exists():hosts.load(str(known))
        name=cfg['host'] if int(cfg.get('port',22))==22 else f'[{cfg["host"]}]:{cfg["port"]}'
        hosts.add(name,public_key.get_name(),public_key);hosts.save(str(known));os.chmod(known,0o600)
        store.event(key,'ssh.host-key.enrolled',target=target,fingerprint=fingerprint,user=g.session['user'])
        return jsonify(saved=True,fingerprint=fingerprint)

    if app.config.get('START_WORKER',True):control.start()
    return app


if __name__=='__main__':
    create_app().run(host='127.0.0.1',port=int(os.environ.get('PORT','8080')),debug=False)
