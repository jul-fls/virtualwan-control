"""Confirmed, restartable fresh installs without changing a customer's VM identity."""
import json,re,threading,time
from flask import jsonify,request
from . import domain
from .store import ident


def credentials(body,guest):
    auth=body.get('ssh_auth','key');username=body.get('username',guest.get('username','debian'))
    if auth not in ('key','password','both'):raise ValueError('Méthode SSH invalide.')
    if not isinstance(username,str) or username=='root' or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',username):raise ValueError('Utilisateur Linux non-root valide requis.')
    key=body.get('public_key','').strip();password=body.get('password','')
    if auth in ('key','both') and not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+(?: [^\r\n]*)?',key):raise ValueError('Clé SSH publique invalide.')
    if auth in ('password','both') and (not isinstance(password,str) or not 12<=len(password)<=256 or '\n' in password or '\x00' in password):raise ValueError('Mot de passe invité de 12 à 256 caractères, sur une seule ligne.')
    return {'username':username,'ssh_auth':auth,'public_key':key if auth in ('key','both') else '', 'password':password if auth in ('password','both') else ''}


def install_reinstall(app,store,control,locks,lock_guard,owner,actor_id,backend,update_vm):
    @app.before_request
    def protect_reinstall_transition():
        if request.method not in ('POST','PUT','DELETE','PATCH'):return
        match=re.fullmatch(r'/api/(?:admin|portal)/vps/([^/]+)(/.*)?',request.path)
        if match and match[2] not in ('/reinstall','/reinstall/cancel','/lock'):
            rows=store.rows('SELECT data FROM deployments WHERE id=?',(match[1],))
            if rows and json.loads(rows[0]['data']).get('reinstall_pending'):
                owner(match[1])
                raise domain.Conflict('Terminer ou reprendre la réinstallation avant une autre modification du VPS.')
    @app.post('/api/admin/vps/<key>/reinstall/cancel')
    @app.post('/api/portal/vps/<key>/reinstall/cancel')
    def cancel_reinstall(key):
        owner(key)
        if request.get_json().get('confirmation')!='ANNULER':raise ValueError('Confirmer l’annulation.')
        with lock_guard:lock=locks.setdefault(key,threading.Lock())
        if not lock.acquire(blocking=False):raise domain.Conflict('La tâche est encore en cours ; attendre sa fin avant d’annuler.')
        try:
            with store.tx() as db:
                d=owner(key);pending=d.get('reinstall_pending')
                if not pending or pending['phase'] not in ('checking','prepared'):raise domain.Conflict('Le remplacement a commencé. Reprendre la réinstallation pour terminer sans nouvel effacement.')
                update_vm(key,reinstall_pending=None)
                db.execute('DELETE FROM secrets WHERE name=?',(pending['access_env'],))
            store.event(ident(),'reinstall.cancelled',deployment_id=key,actor=actor_id())
        finally:lock.release()
        return jsonify(ok=True)
    @app.post('/api/admin/vps/<key>/reinstall')
    @app.post('/api/portal/vps/<key>/reinstall')
    def reinstall_vm(key):
        d=owner(key);b=request.get_json();actor=actor_id()
        if b.get('confirmation')!='REINSTALLER' or b.get('hostname')!=d.get('fqdn') or b.get('erase_system_disk') is not True:raise ValueError('Confirmer la suppression des données du disque système en saisissant REINSTALLER et le nom exact du VPS.')
        if not control.demo and not control.allow_provision:raise domain.Conflict('Les opérations infrastructure sont désactivées.')
        with lock_guard:lock=locks.setdefault(key,threading.Lock())
        if not lock.acquire(blocking=False):raise domain.Conflict('Une opération est en cours sur ce VPS.')
        run=ident()
        try:
            with store.tx() as db:
                d=owner(key)
                if d.get('status') not in ('active','stopped','imported','simulated') or not d.get('ip'):raise domain.Conflict('VPS indisponible pour une réinstallation.')
                if d.get('rescue_state') or d.get('rescue_pending') or d.get('resize_pending'):raise domain.Conflict('Revenir au disque système et terminer le changement de gamme avant de réinstaller.')
                if db.execute("SELECT count(*) FROM client_operations WHERE state='running'").fetchone()[0]>=8:raise domain.Conflict('Plusieurs opérations sont en cours. Réessayer plus tard.')
                pending=d.get('reinstall_pending')
                if not pending:
                    profile=store.document('profiles',d['profile_id']);guest=profile['config']['guest']
                    access=credentials(b,guest)
                    models=store.rows('SELECT data FROM models WHERE id=?',(d.get('model_id'),))
                    model=json.loads(models[0]['data']) if models else {}
                    pending={'id':ident(),'template':domain.positive(model.get('template') or profile['config']['pve']['template'],999999999),'phase':'checking','username':access['username'],'ssh_auth':access['ssh_auth']}
                    name='REINSTALL_'+pending['id'].upper()
                    store.secret_set(name,json.dumps(access));pending['access_env']=name
                    update_vm(key,reinstall_pending=pending)
                access=json.loads(store.secret_get(pending['access_env']))
                if access.get('password'):store.redactions.add(access['password'])
                db.execute('INSERT INTO client_operations VALUES(?,?,?,?,?,?,?)',(run,actor,key,'reinstall','running',json.dumps({'phase':'checking','message':'Préparation de la réinstallation…'}),time.time()))
        except BaseException:lock.release();raise
        d['_operation_run']=run
        def progress(phase,message):
            with store.tx() as db:db.execute('UPDATE client_operations SET result=? WHERE id=?',(json.dumps({'phase':phase,'message':message}),run))
            store.event(run,'reinstall.progress',phase=phase,message=message)
        def checkpoint(plan):
            update_vm(key,reinstall_pending=plan)
        def execute():
            complete=False
            try:
                store.event(run,'reinstall.started',deployment_id=key,actor=actor,template=pending['template'],resuming=pending['phase']!='checking')
                if control.demo:checkpoint({**pending,'phase':'starting'})
                else:
                    with backend(d) as pve:
                        from .adapters.reinstall import fresh_install
                        fresh_install(pve,pending,access,progress,checkpoint)
                with store.tx() as db:
                    update_vm(key,reinstall_pending=None,reinstalled_at=time.time(),power_state='running',notes_pending=True)
                    db.execute('DELETE FROM secrets WHERE name=?',(pending['access_env'],))
                    db.execute('DELETE FROM rescue_credentials WHERE deployment_id=?',(key,))
                    db.execute("UPDATE client_operations SET state='succeeded',result=? WHERE id=?",(json.dumps({'phase':'ready','message':'Système neuf installé. VPS démarré ; cloud-init initialise les nouveaux accès. Les clés hôtes SSH ont changé.'}),run))
                complete=True
            except Exception as error:
                message=str(error) if isinstance(error,(domain.Conflict,ValueError)) else 'Réinstallation interrompue. Réessayer pour reprendre la même opération ; consulter le journal administrateur.'
                with store.tx() as db:db.execute("UPDATE client_operations SET state='failed',result=? WHERE id=?",(json.dumps({'error':message,'message':message,'reference':run}),run))
                store.event(run,'reinstall.failed',error_type=type(error).__name__,error=str(error))
            finally:lock.release()
            if complete:
                try:store.event(run,'reinstall.complete',deployment_id=key)
                except Exception:pass
                try:app.extensions['sync_vm_notes'](key)
                except Exception:store.event(run,'reinstall.notes.pending',deployment_id=key)
        try:
            runner=app.config.get('CLIENT_TASK_RUNNER')
            if runner:runner(execute)
            else:threading.Thread(target=execute,name='vwan-reinstall-'+run[:8],daemon=True).start()
        except BaseException:
            lock.release()
            with store.tx() as db:db.execute("UPDATE client_operations SET state='interrupted' WHERE id=?",(run,))
            raise
        return jsonify(operation_id=run),202
