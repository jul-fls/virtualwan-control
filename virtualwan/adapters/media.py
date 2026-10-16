"""One owned, read-only SCSI CD per VPS; no client-supplied PVE volume IDs."""
from pathlib import Path,PurePosixPath
import hashlib,os,re
from .. import domain

SLOT='scsi1'
def identifier(pve,upload):
    storage=os.environ.get('VWAN_CLIENT_ISO_STORAGE') or pve.profile['config']['pve']['snippet_storage']
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}',storage) or not re.fullmatch(r'[a-f0-9]{32}',upload):raise ValueError('Invalid media identity')
    return storage+':iso/virtualwan-'+str(int(pve.client['vmid']))+'-client-'+upload+'.iso'

def owned(pve,volume):
    # Volume IDs come only from the server's per-VM record. Keep old media
    # manageable after the operator changes the storage used for new uploads.
    return bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}:iso/virtualwan-'+str(int(pve.client['vmid']))+r'-client-[a-f0-9]{32}\.iso',volume))

def location(pve,volume):
    if not owned(pve,volume):raise domain.Conflict('ISO hors de ce VPS.')
    storage=volume.split(':')[0];cfg=pve.api('get','/storage/'+storage)
    if 'iso' not in str(cfg.get('content','')).split(','):raise domain.Conflict('Le stockage ISO ne permet pas les ISO.')
    if pve.node!=pve.profile['config']['pve']['node'] and not cfg.get('shared'):raise domain.Conflict('ISO local indisponible après migration ; utiliser un stockage ISO partagé entre les nœuds.')
    path=PurePosixPath(pve.command(['pvesm','path',volume]).strip())
    if not path.is_absolute() or path.name!=volume.split('/')[-1]:raise domain.Conflict('Chemin ISO Proxmox invalide.')
    return str(path)

def set_cd(pve,volume=None,next_boot=False):
    pve.check_identity();c=pve.api('get','/config',current=1)
    old=c.get(SLOT)
    if old and 'media=cdrom' not in old:raise domain.Conflict('scsi1 est un disque existant ; aucun remplacement automatique.')
    if old and old.split(',')[0]!='none' and not owned(pve,old.split(',')[0]):raise domain.Conflict('Le lecteur contient un média administrateur non géré.')
    if volume:
        path=location(pve,volume)
        with pve.ssh.open_sftp() as sftp:
            if not 0<sftp.stat(path).st_size<=10_000_000_000:raise domain.Conflict('ISO vide ou hors quota.')
    if next_boot and pve.client.get('rescue_state'):raise domain.Conflict('Revenir au disque système avant de choisir le prochain boot ISO.')
    options={SLOT:(volume or 'none')+',media=cdrom', 'digest':pve.config['digest']}
    if next_boot:options['boot']='order=scsi1;scsi0'
    elif not volume and SLOT in c.get('boot',''):options['boot']='order=scsi0'
    pve.api('set','/config',**options)
    current=pve.api('get','/config',current=1)
    if current.get(SLOT,'').split(',')[0]!=(volume or 'none'):
        raise domain.Conflict('Le changement CD est en attente dans Proxmox. Activer le hotplug disque pour cette VM ; aucun redémarrage automatique n’a été lancé.')
    return {'mounted':bool(volume),'next_boot':next_boot}

def upload(pve,local,upload_id,size,sha256):
    volume=identifier(pve,upload_id);path=location(pve,volume);temporary=path+'.part'
    status=pve.api('get',f'/nodes/{pve.node}/storage/{volume.split(":")[0]}/status')
    if not status.get('active') or status.get('avail',0)<size+64*1024**2:raise domain.Conflict('Stockage ISO insuffisant.')
    with pve.ssh.open_sftp() as sftp:
        sftp.get_channel().settimeout(60)
        # Generated names and exclusive creation prevent replacing any shared ISO.
        try:
            existing=sftp.stat(path)
        except FileNotFoundError:existing=None
        if existing:raise domain.Conflict('Cet ISO existe déjà ; vérifier l’opération précédente.')
        try:
            with sftp.file(temporary,'wx') as target,open(local,'rb') as source:
                target.set_pipelined(True)
                digest=hashlib.sha256();total=0
                while block:=source.read(256*1024):target.write(block);digest.update(block);total+=len(block)
                target.flush()
            if total!=size or digest.hexdigest()!=sha256:raise ValueError('ISO local modifié pendant le transfert.')
            # PVE reads ISO as root; private permissions prevent other host users.
            sftp.chmod(temporary,0o600);sftp.rename(temporary,path)
        except BaseException:
            try:sftp.remove(temporary)
            except FileNotFoundError:pass
            raise
    # Verify remote bytes before allowing attachment, not just the local source.
    digest=pve.command(['sha256sum',path],timeout=1800).split()[0]
    if digest!=sha256:
        with pve.ssh.open_sftp() as sftp:sftp.remove(path)
        raise domain.Conflict('Empreinte ISO distante différente ; média retiré.')
    return volume

def remove(pve,volume):
    path=location(pve,volume)
    pve.check_identity();current=pve.api('get','/config',current=1)
    if current.get(SLOT,'').split(',')[0]==volume:set_cd(pve)
    # A foreign CD may remain; deletion is still restricted to the owned file.
    with pve.ssh.open_sftp() as sftp:
        for owned_path in (path,path+'.part'):
            try:sftp.remove(owned_path)
            except FileNotFoundError:pass
