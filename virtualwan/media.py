"""Authenticated resumable ISO upload: one slot per VM, 10 GB including staging."""
import base64,binascii,hashlib,json,os,re,shutil,threading,time
from pathlib import Path
from flask import jsonify,request
from . import domain
from .store import ident

MAX_BYTES=10_000_000_000
CHUNK_BYTES=512*1024

def media_record(store,key):
    rows=store.rows('SELECT data FROM vm_media WHERE deployment_id=?',(key,))
    return json.loads(rows[0]['data']) if rows else None

def save_media(store,key,data):
    with store.tx() as db:db.execute('INSERT OR REPLACE INTO vm_media VALUES(?,?)',(key,json.dumps(data)))

def media_view(data):
    return {k:data.get(k) for k in ('id','filename','size','offset','state','mounted','next_boot','sha256','message')} if data else None

def install_media(app,store,control,locks,lock_guard,owner,backend,actor_id):
    root=store.root/'client-media';root.mkdir(mode=0o700,exist_ok=True)
    concurrency=int(os.environ.get('VWAN_CLIENT_ISO_TRANSFERS','2'))
    if not 1<=concurrency<=8:raise ValueError('VWAN_CLIENT_ISO_TRANSFERS doit être compris entre 1 et 8.')
    transfers=threading.BoundedSemaphore(concurrency)
    with store.tx() as db:db.execute('CREATE TABLE IF NOT EXISTS vm_media(deployment_id TEXT PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,data TEXT NOT NULL)')
    # A restart must never silently repeat a transfer or an attachment.
    for row in store.rows('SELECT deployment_id,data FROM vm_media'):
        data=json.loads(row['data'])
        if data['state']=='transferring':data.update(state='failed',message='Transfert interrompu ; supprimer ce média puis renvoyer l’ISO.');save_media(store,row['deployment_id'],data)
    def path(key,data):
        if not re.fullmatch(r'[a-f0-9]{32}',key) or not re.fullmatch(r'[a-f0-9]{32}',data['id']):raise ValueError('Invalid media path')
        return root/(key+'-'+data['id']+'.part')
    def lock_for(key):
        with lock_guard:lock=locks.setdefault(key,threading.Lock())
        if not lock.acquire(blocking=False):raise domain.Conflict('Une opération est en cours sur ce VPS.')
        return lock
    def writable(key):
        d=owner(key)
        if not control.demo and not control.allow_provision:raise domain.Conflict('Les opérations infrastructure sont désactivées.')
        if d.get('status') not in ('active','stopped','imported','simulated'):raise domain.Conflict('VPS indisponible.')
        if d.get('reinstall_pending') or d.get('rescue_pending') or d.get('resize_pending'):raise domain.Conflict('Terminer la transition du VPS avant de modifier un ISO.')
        return d
    def erase(d):
        key=d['id'];data=media_record(store,key)
        if not data:return
        if data.get('volume') and not control.demo:
            from .adapters.media import remove
            with backend(d) as pve:remove(pve,data['volume'])
        path(key,data).unlink(missing_ok=True)
        with store.tx() as db:db.execute('DELETE FROM vm_media WHERE deployment_id=?',(key,))
        store.event(data['id'],'media.deleted',deployment_id=key,upload_id=data['id'])
    def retirement_media(key):
        with lock_guard:
            if key in locks and locks[key].locked():raise domain.Conflict('Une opération est en cours sur ce VPS.')
        data=media_record(store,key)
        if data and data['state'] in ('uploading','transferring'):raise domain.Conflict('Terminer ou supprimer l’envoi ISO avant de supprimer le VPS.')
        return data
    control.retirement_media=retirement_media

    @app.get('/api/admin/vps/<key>/media')
    @app.get('/api/portal/vps/<key>/media')
    def get_media(key):
        owner(key)
        return jsonify(media=media_view(media_record(store,key)),max_bytes=MAX_BYTES,chunk_bytes=CHUNK_BYTES)

    @app.post('/api/admin/vps/<key>/media')
    @app.post('/api/portal/vps/<key>/media')
    def begin_upload(key):
        writable(key);b=request.get_json();size=b.get('size');filename=b.get('filename','')
        if type(size) is not int or not 32774<=size<=MAX_BYTES:raise ValueError('ISO de 10 Go maximum (10 000 000 000 octets).')
        if not isinstance(filename,str) or not 1<=len(filename)<=160 or not filename.lower().endswith('.iso') or any(ord(c)<32 for c in filename) or '/' in filename or '\\' in filename:raise ValueError('Nom de fichier ISO invalide.')
        lock=lock_for(key)
        try:
            writable(key)
            if media_record(store,key):raise domain.Conflict('Un seul ISO par VPS. Supprimer le média existant avant son remplacement.')
            if shutil.disk_usage(root).free<size+64*1024**2:raise domain.Conflict('Espace de transfert insuffisant.')
            data={'id':ident(),'filename':filename,'size':size,'offset':0,'state':'uploading','mounted':False,'next_boot':False,'updated_at':time.time()}
            with open(path(key,data),'xb'):pass
            os.chmod(path(key,data),0o600);save_media(store,key,data)
            store.event(data['id'],'media.upload.started',deployment_id=key,actor=actor_id(),filename=filename,size=size)
        finally:lock.release()
        return jsonify(media=media_view(data),chunk_bytes=CHUNK_BYTES),201

    @app.post('/api/admin/vps/<key>/media/chunk')
    @app.post('/api/portal/vps/<key>/media/chunk')
    def upload_chunk(key):
        writable(key);b=request.get_json();lock=lock_for(key)
        try:
            writable(key)
            data=media_record(store,key)
            if not data or data['state']!='uploading' or b.get('id')!=data['id']:raise domain.Conflict('Transfert introuvable.')
            if type(b.get('offset')) is not int or b['offset']!=data['offset']:raise domain.Conflict('Position de transfert différente ; reprendre au dernier octet confirmé.')
            try:block=base64.b64decode(b['data'],validate=True)
            except (ValueError,TypeError,KeyError,binascii.Error):raise ValueError('Bloc ISO invalide.') from None
            if not 1<=len(block)<=CHUNK_BYTES or data['offset']+len(block)>data['size']:raise ValueError('Bloc hors quota.')
            with open(path(key,data),'r+b') as stream:
                stream.seek(data['offset']);stream.write(block);stream.truncate();stream.flush();os.fsync(stream.fileno())
            data.update(offset=data['offset']+len(block),updated_at=time.time());save_media(store,key,data)
        finally:lock.release()
        return jsonify(offset=data['offset'])

    @app.post('/api/admin/vps/<key>/media/complete')
    @app.post('/api/portal/vps/<key>/media/complete')
    def complete_upload(key):
        d=writable(key);b=request.get_json();lock=lock_for(key)
        acquired=False
        try:
            d=writable(key)
            data=media_record(store,key)
            if not data or data['state']!='uploading' or b.get('id')!=data['id'] or data['offset']!=data['size']:raise domain.Conflict('Transfert incomplet.')
            if not transfers.acquire(blocking=False):raise domain.Conflict('Les transferts ISO sont occupés ; réessayer la finalisation dans quelques instants.')
            acquired=True
            data.update(state='transferring',message='Vérification puis transfert vers Proxmox…');save_media(store,key,data)
            actor=actor_id()
            def execute():
                try:
                    source=path(key,data)
                    with source.open('rb') as stream:
                        stream.seek(32768);header=stream.read(65536)
                        if not any(sig in header for sig in (b'CD001',b'BEA01',b'NSR02',b'NSR03')):raise ValueError('Image ISO9660/UDF non reconnue.')
                        stream.seek(0);digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    data['sha256']=digest
                    if control.demo:data['volume']='demo:iso/'+data['id']+'.iso'
                    else:
                        from .adapters.media import upload
                        with backend(d) as pve:
                            # Store the destination before I/O so an interrupted
                            # copy remains owned and can be cleaned, never orphaned.
                            from .adapters.media import identifier
                            data['volume']=identifier(pve,data['id']);data['node']=pve.node;save_media(store,key,data)
                            store.event(data['id'],'media.transfer.started',deployment_id=key,node=pve.node,volume=data['volume'],size=data['size'],sha256=digest)
                            upload(pve,source,data['id'],data['size'],digest)
                    data.update(state='ready',message='ISO prêt. Insérer le média ou choisir le prochain boot.');save_media(store,key,data);source.unlink(missing_ok=True)
                    store.event(data['id'],'media.upload.complete',deployment_id=key,actor=actor,size=data['size'],sha256=digest,volume=data.get('volume'))
                except Exception as error:
                    data.update(state='failed',message=str(error) if isinstance(error,(ValueError,domain.Conflict)) else 'Transfert ISO interrompu ; consulter le journal.');save_media(store,key,data)
                    store.event(data['id'],'media.upload.failed',deployment_id=key,error_type=type(error).__name__,error=str(error))
                finally:transfers.release();lock.release()
            runner=app.config.get('CLIENT_TASK_RUNNER')
            if runner:runner(execute)
            else:threading.Thread(target=execute,name='vwan-iso-'+key[:8],daemon=True).start()
        except BaseException:
            if acquired:transfers.release()
            lock.release();raise
        return jsonify(media=media_view(media_record(store,key))),202

    @app.post('/api/admin/vps/<key>/media/attach')
    @app.post('/api/portal/vps/<key>/media/attach')
    def attach_media(key):
        d=writable(key);b=request.get_json();lock=lock_for(key)
        try:
            d=writable(key)
            data=media_record(store,key)
            if not data or data['state']!='ready':raise domain.Conflict('ISO non prêt.')
            action=b.get('action')
            if action not in ('insert','eject','next_boot'):raise ValueError('Action ISO invalide.')
            if d.get('rescue_state') and action=='next_boot':raise domain.Conflict('Quitter le rescue avant de changer le prochain boot.')
            if not control.demo:
                from .adapters.media import set_cd
                with backend(d) as pve:set_cd(pve,data['volume'] if action!='eject' else None,action=='next_boot')
            data.update(mounted=action!='eject',next_boot=action=='next_boot');save_media(store,key,data)
            store.event(data['id'],'media.'+action,deployment_id=key,actor=actor_id(),upload_id=data['id'])
        finally:lock.release()
        return jsonify(media=media_view(data))

    @app.delete('/api/admin/vps/<key>/media')
    @app.delete('/api/portal/vps/<key>/media')
    def delete_media(key):
        d=writable(key);lock=lock_for(key)
        try:erase(writable(key))
        finally:lock.release()
        return jsonify(ok=True)
