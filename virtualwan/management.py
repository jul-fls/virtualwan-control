"""Administrator VM management, durable hardware transitions and managed PVE notes."""
import html,json,re,threading,time
from flask import g,jsonify,request
from . import domain
from .adapters.client_pve import ClientPVE
from .store import ident


def deployment(store,key):
    row=store.one('SELECT * FROM deployments WHERE id=?',(key,))
    data={**json.loads(row['data']),**{k:v for k,v in row.items() if k!='data'}}
    rows=store.rows('SELECT v.*,a.name AS account_name,a.email AS account_email FROM client_vms v JOIN accounts a ON a.id=v.account_id WHERE v.deployment_id=?',(key,))
    return {**data,**(rows[0] if rows else {})}


def notes(store,d,current=''):
    models=store.rows('SELECT data FROM models WHERE id=?',(d.get('model_id'),))
    model=json.loads(models[0]['data']) if models else {}
    fields={'Client':d.get('account_name') or d.get('customer') or 'Non affecté','Compte':d.get('account_email') or '—','VPS':d.get('fqdn'),'Référence Virtualwan':d['id'],'VMID':d.get('vmid'),'Gamme':model.get('range'),'Modèle':model.get('name'),'vCPU':d.get('cores'),'RAM (Mo)':d.get('memory_mb'),'Disque réel (Go)':d.get('disk_gb'),'IPv4':d.get('ip'),'IPv6':d.get('public_ipv6'),'Next-hop pfSense':d.get('next_hop'),'MAC':d.get('mac'),'Profil routeur':d.get('profile_id'),'Gestion client':'VERROUILLÉE' if d.get('management_locked') else 'Autorisée','Réseau':'Suspendu' if d.get('network_suspended') else 'Autorisé'}
    def safe(value):return re.sub(r'([\\`*_{}\[\]()#+!|])',r'\\\1',html.escape(str(value or '—').replace('\r',' ').replace('\n',' ')))
    section='<!-- virtualwan:begin -->\n## Virtualwan\n'+'\n'.join('- **'+key+'** : '+safe(value) for key,value in fields.items())+'\n<!-- virtualwan:end -->'
    current=re.sub(r'<!-- virtualwan:begin -->.*?<!-- virtualwan:end -->','',current,flags=re.S).strip()
    marker='Virtualwan managed: '+str(d.get('fqdn',''))
    if not current:current=marker
    result=current+'\n\n'+section
    if len(result)>60000:raise ValueError('Notes Proxmox trop longues.')
    return result


def install_management(app,store,control,locks,lock_guard,public_vm,update_vm):
    def backend(d):return app.config.get('CLIENT_PVE_FACTORY',ClientPVE)(store,store.document('profiles',d['profile_id']),d)
    def spawn(key,action,work,actor=None,system=False):
        d=deployment(store,key)
        if not control.demo and not control.allow_provision:raise domain.Conflict('Les opérations infrastructure sont désactivées.')
        with lock_guard:lock=locks.setdefault(key,threading.Lock())
        if not lock.acquire(blocking=False):raise domain.Conflict('Une opération est en cours sur ce VPS.')
        run=ident()
        try:
            with store.tx() as db:
                d=deployment(store,key)
                if d.get('reinstall_pending'):raise domain.Conflict('Terminer la réinstallation avant une autre modification.')
                if db.execute("SELECT count(*) FROM client_operations WHERE state='running'").fetchone()[0]>=8:raise domain.Conflict('Plusieurs opérations sont en cours. Réessayez dans quelques instants.')
                if d.get('status') not in ('active','stopped','imported','simulated'):raise domain.Conflict('VPS indisponible pour cette opération.')
                db.execute('INSERT INTO client_operations VALUES(?,?,?,?,?,?,?)',(run,actor or 'admin:system',key,action,'running',json.dumps({'phase':'checking','message':'Préparation de l’opération…'}),time.time()))
        except BaseException:lock.release();raise
        d['_operation_run']=run
        def progress(phase,message):
            with store.tx() as db:db.execute('UPDATE client_operations SET result=? WHERE id=?',(json.dumps({'phase':phase,'message':message}),run))
            store.event(run,'admin.vm.progress',phase=phase,message=message)
        def execute():
            try:
                store.event(run,'admin.vm.started',deployment_id=key,operation=action,actor=actor or 'system')
                result=work(d,progress) or {};result.update(phase='ready',message='Opération terminée. Synchronisation des notes à refaire.' if result.get('notes_pending') else 'Opération terminée.')
                with store.tx() as db:db.execute("UPDATE client_operations SET state='succeeded',result=? WHERE id=?",(json.dumps(result),run))
                store.event(run,'admin.vm.complete',operation=action)
            except Exception as error:
                message=str(error) if isinstance(error,(ValueError,domain.Conflict)) else 'Opération interrompue. Consulter le journal puis réessayer.'
                with store.tx() as db:db.execute("UPDATE client_operations SET state='failed',result=? WHERE id=?",(json.dumps({'error':message,'message':message,'reference':run}),run))
                store.event(run,'admin.vm.failed',error_type=type(error).__name__,error=str(error))
            finally:lock.release()
        try:
            runner=app.config.get('CLIENT_TASK_RUNNER')
            if runner:runner(execute)
            else:threading.Thread(target=execute,name='vwan-admin-'+run[:8],daemon=True).start()
        except BaseException:
            lock.release()
            with store.tx() as db:db.execute("UPDATE client_operations SET state='interrupted' WHERE id=?",(run,))
            raise
        return run
    def write_notes(d,pve=None):
        if control.demo:update_vm(d['id'],pve_notes=notes(store,d),notes_pending=False);return
        if pve is None:
            with backend(d) as remote:return write_notes(d,remote)
        pve.config=pve.api('get','/config')
        try:
            hw=pve.hardware();update_vm(d['id'],**{k:hw[k] for k in ('cores','memory_mb','disk_gb','onboot','network_suspended')})
        except domain.Conflict:pass
        pve.set_notes(notes(store,deployment(store,d['id']),pve.config.get('description','')))
        update_vm(d['id'],notes_pending=False)
    def schedule_notes(key):
        update_vm(key,notes_pending=True)
        if control.demo:
            write_notes(deployment(store,key));return None
        try:return spawn(key,'admin.notes',lambda d,p:write_notes(d),system=True)
        except domain.Conflict:return None
    app.extensions['sync_vm_notes']=schedule_notes
    control.notes_sync=schedule_notes
    @app.get('/api/admin/state')
    def admin_state():
        vps=[]
        for row in store.rows('SELECT id FROM deployments'):
            d=deployment(store,row['id']);v=public_vm(d)
            v.update({k:d.get(k) for k in ('account_id','account_name','account_email','customer','vmid','mac','next_hop','profile_id','model_id','management_lock_reason','network_suspended','notes_pending','resize_pending','onboot')})
            for action,field in [('rescue','rescue_operation'),('graphical','graphical_operation'),('reinstall','reinstall_operation')]:
                jobs=store.rows("SELECT id,result FROM client_operations WHERE deployment_id=? AND action=? AND state='running'",(d['id'],action))
                if jobs:v[field]={'id':jobs[0]['id'],**json.loads(jobs[0]['result'])}
            jobs=store.rows("SELECT id,result FROM client_operations WHERE deployment_id=? AND action LIKE 'admin.%' AND state='running'",(d['id'],))
            if jobs:v['admin_operation']={'id':jobs[0]['id'],**json.loads(jobs[0]['result'])}
            vps.append(v)
        return jsonify(account={'name':'Administration · '+g.session['user']},csrf=g.session['csrf'],vps=vps,demo=control.demo,admin=True,models=[store.document('models',r['id']) for r in store.rows('SELECT id FROM models')],rescue_available=bool(__import__('os').environ.get('VWAN_RESCUE_ISO')),operations=store.rows('SELECT id,deployment_id,action,state,created_at FROM client_operations ORDER BY created_at DESC LIMIT 100'))
    @app.post('/api/admin/notes/sync')
    def admin_sync_all_notes():
        accepted=[];pending=[]
        for row in store.rows('SELECT id FROM deployments'):
            d=deployment(store,row['id'])
            if d.get('status') not in ('active','stopped','imported','simulated'):continue
            job=schedule_notes(row['id'])
            (accepted if job or control.demo else pending).append(row['id'])
        return jsonify(accepted=accepted,pending=pending)
    @app.get('/api/admin/vps/<key>/hardware')
    def admin_hardware(key):
        d=deployment(store,key)
        if control.demo:return jsonify(cores=d.get('cores') or 2,memory_mb=d.get('memory_mb') or 2048,disk_gb=d.get('disk_gb') or 32,onboot=bool(d.get('onboot')),network_suspended=bool(d.get('network_suspended')))
        with backend(d) as pve:return jsonify(pve.hardware())
    @app.put('/api/admin/vps/<key>/lock')
    def admin_lock_client(key):
        b=request.get_json();locked=b.get('locked')
        if not isinstance(locked,bool) or b.get('confirmation')!='CONFIRMER':raise ValueError('Confirmer le verrouillage/déverrouillage.')
        reason=str(b.get('reason','')).strip()
        if len(reason)>2000:raise ValueError('Motif trop long.')
        run=ident()
        with store.tx() as db:
            deployment(store,key);update_vm(key,management_locked=locked,management_lock_reason=reason,management_locked_at=time.time() if locked else None)
            db.execute('INSERT INTO client_operations VALUES(?,?,?,?,?,?,?)',(run,'admin:'+g.session['user'],key,'admin.lock','succeeded',json.dumps({'locked':locked}),time.time()))
            for row in db.execute("SELECT id,data FROM client_challenges WHERE purpose='console'").fetchall():
                data=json.loads(row['data'])
                if data.get('deployment_id')==key and not data.get('admin'):db.execute('DELETE FROM client_challenges WHERE id=?',(row['id'],))
        store.event(run,'admin.vm.management_lock',deployment_id=key,locked=locked,reason=reason,actor=g.session['user'])
        schedule_notes(key)
        return jsonify(ok=True)
    @app.post('/api/admin/vps/<key>/notes')
    def admin_sync_notes(key):return jsonify(operation_id=spawn(key,'admin.notes',lambda d,p:write_notes(d),actor='admin:'+g.session['user'])),202
    @app.post('/api/admin/vps/<key>/network')
    def admin_network(key):
        b=request.get_json();blocked=b.get('blocked')
        if not isinstance(blocked,bool) or b.get('confirmation')!='CONFIRMER':raise ValueError('Confirmer la suspension/restauration réseau.')
        def work(d,progress):
            if not control.demo:
                with backend(d) as pve:pve.set_network(blocked);update_vm(key,network_suspended=blocked);write_notes(d,pve)
            else:update_vm(key,network_suspended=blocked);write_notes(deployment(store,key))
        return jsonify(operation_id=spawn(key,'admin.network',work,actor='admin:'+g.session['user'])),202
    @app.post('/api/admin/vps/<key>/onboot')
    def admin_onboot(key):
        enabled=request.get_json().get('enabled')
        if not isinstance(enabled,bool):raise ValueError('État invalide.')
        def work(d,p):
            if not control.demo:
                with backend(d) as remote:remote.set_onboot(enabled)
            update_vm(key,onboot=enabled)
        return jsonify(operation_id=spawn(key,'admin.onboot',work,actor='admin:'+g.session['user'])),202
    @app.post('/api/admin/vps/<key>/resize')
    def admin_resize(key):
        b=request.get_json()
        if b.get('confirmation')!='CONFIRMER':raise ValueError('Confirmer le changement de gamme et son redémarrage.')
        model=store.document('models',b['model_id']);target=domain.model(model)
        def work(d,progress):
            pending=d.get('resize_pending')
            if d.get('rescue_pending'):raise domain.Conflict('Terminer le basculement rescue avant le changement de gamme.')
            if pending and pending['model_id']!=model['id']:raise domain.Conflict('Reprendre le changement de gamme précédent.')
            def checkpoint(value,confirmed):
                with store.tx() as db:
                    values={'resize_pending':{**value,'model_id':model['id']}}
                    if confirmed:
                        values.update(cores=value['cores'],memory_mb=value['memory_mb'],disk_gb=value['disk_gb'])
                        db.execute('UPDATE deployments SET model_id=? WHERE id=?',(model['id'],key))
                    update_vm(key,**values)
            selected=pending['model'] if pending else target
            if control.demo:
                actual={'cores':selected['cores'],'memory_mb':selected['memory_mb'],'disk_gb':max(d.get('disk_gb') or 32,selected['disk_gb'])};checkpoint({**actual,'was_running':True,'model':selected},True);write_notes(deployment(store,key))
            else:
                with backend(d) as pve:
                    actual=pve.resize(selected,pending,progress,checkpoint)
                    update_vm(key,resize_pending=None)
                    try:write_notes(deployment(store,key),pve)
                    except Exception as error:
                        update_vm(key,notes_pending=True);actual['notes_pending']=True
                        store.event(d['_operation_run'],'admin.notes.pending',error_type=type(error).__name__)
            update_vm(key,resize_pending=None)
            return actual
        return jsonify(operation_id=spawn(key,'admin.resize',work,actor='admin:'+g.session['user'])),202
