"""Replace only a verified managed scsi0; checkpoint every irreversible boundary."""
import json,re
from pathlib import PurePosixPath
from .. import domain
from ..ipv6 import ipv6_cloud_files
from ..guest_os import os_user_data,os_network_data


def volume(spec):return str(spec).split(',',1)[0]


def fresh_install(pve,plan,access,progress,checkpoint):
    plan=dict(plan);vmid=int(pve.client['vmid']);cfg=pve.profile['config']['pve'];token='vwan'+plan['id'][:20]
    image=plan.get('os_image',{});custom=bool(plan.get('custom_iso'));boot='order=scsi1;scsi0' if custom else 'order=scsi0'
    guest={**pve.profile['config']['guest'],**({'os_family':image['family'],'interface':image['interface']} if image else {})}
    v6={**pve.profile['config'].get('ipv6',{}),'address':pve.client['public_ipv6']} if pve.client.get('public_ipv6') else None
    if custom:
        from .media import location
        location(pve,plan['custom_iso'])
    def save(**values):plan.update(values);checkpoint(dict(plan))
    def read():
        pve.check_identity();current=dict(pve.config)
        if plan.get('node') and pve.node!=plan['node']:raise domain.Conflict('Le VPS a changé de nœud pendant la réinstallation ; intervention administrateur requise.')
        if plan.get('cicustom') and (current.get('cicustom')!=plan['cicustom'] or volume(current.get('ide2',''))!=plan['cloudinit_drive']):raise domain.Conflict('Les médias cloud-init ont changé ; intervention administrateur requise.')
        return current
    def set_config(**values):
        c=read();pve.api('set','/config',digest=c['digest'],**values);return read()
    def fail(message):raise domain.Conflict(message)
    def own(vol):
        return bool(re.fullmatch(r'[A-Za-z0-9_-]+:(?:'+str(vmid)+r'/)?vm-'+str(vmid)+r'-[A-Za-z0-9_.-]+',vol))
    c=read()
    if c.get('scsi1') and 'media=cdrom' in c['scsi1'] and c['scsi1'].split(',')[0]!='none':
        from .media import owned
        if not owned(pve,c['scsi1'].split(',')[0]):fail('Retirer le CD administrateur avant réinstallation.')
    # Reinstall never consumes an arbitrary extra disk, snapshot, passthrough or rescue CD.
    if c.get('lock'):fail('Le VPS est verrouillé par Proxmox. Terminer la tâche précédente avant de reprendre.')
    snapshots=pve.api('get','/snapshot') or []
    if any(s.get('name')!='current' for s in snapshots):fail('Supprimer ou exporter les snapshots avant une réinstallation.')
    if c.get('bios','seabios')!='seabios' or any(re.fullmatch(r'(?:efidisk|tpmstate|hostpci|usb)\d+',k) for k in c):fail('Matériel EFI/TPM ou passthrough non pris en charge pour la réinstallation automatique.')
    disks={k:v for k,v in c.items() if re.fullmatch(r'(?:scsi|virtio|sata|ide|unused)\d+',k) and 'media=cdrom' not in str(v) and str(v)!='none'}
    allowed={plan.get('old_volume'),plan.get('new_volume')}-{None}
    for k,v in disks.items():
        if k=='scsi0' and (not allowed or volume(v) in allowed):continue
        if k=='scsi30' and ',serial='+token in v:continue
        if k.startswith('unused') and volume(v) in allowed:continue
        fail('Disque supplémentaire ou identité disque différente : intervention administrateur requise, aucun effacement automatique.')
    if not plan.get('old_volume'):
        if 'scsi0' not in c or not own(volume(c['scsi0'])):fail('Disque système scsi0 géré introuvable.')
        if any(k in c for k in ('ide0','ide1')):fail('Retirer les médias additionnels avant de réinstaller.')
        refs=dict(part.split('=',1) for part in c.get('cicustom','').split(',') if '=' in part)
        expected={s:cfg['snippet_storage']+f':snippets/virtualwan-{vmid}-{s}.yaml' for s in ('user','network')}
        if refs!=expected or 'cloudinit' not in c.get('ide2',''):fail('Configuration cloud-init différente de la configuration gérée.')
        tc={}
        if not custom:
            template=int(plan['template']);resources=pve.api('get','/cluster/resources',type='vm')
            t=next((x for x in resources if x.get('vmid')==template and x.get('type')=='qemu'),None)
            if not t or t['node']!=pve.node:fail('Le template de réinstallation doit être disponible sur le même nœud que ce VPS.')
            tc=pve.api('get',f"/nodes/{pve.node}/qemu/{template}/config")
            match=re.search(r'(?:^|,)size=([0-9.]+)([KMGT])(?:,|$)',tc.get('scsi0',''))
            if not match or float(match[1])*{'K':1/1024**2,'M':1/1024,'G':1,'T':1024}[match[2]]>pve.hardware()['disk_gb']:fail('Le disque du template est plus grand que le disque alloué au VPS.')
            if not tc.get('template') or 'scsi0' not in tc or tc.get('bios','seabios')!='seabios':fail('Template cloud-init scsi0 BIOS compatible requis.')
        hw=pve.hardware();storage=cfg['disk_storage']
        available=pve.api('get',f'/nodes/{pve.node}/storage/{storage}/status')
        if not available.get('active') or available.get('avail',0)<hw['disk_gb']*1024**3:fail('Stockage insuffisant pour préparer un nouveau disque avant suppression de l’ancien.')
        with pve.ssh.open_sftp() as sftp:
            directory=PurePosixPath(cfg['snippet_directory'])
            if not directory.is_absolute():fail('Répertoire de snippets absolu requis.')
            with sftp.file(str(directory/f'virtualwan-{vmid}-network.yaml')) as f:network=json.loads(f.read(131072))
        physical=network.get('config',[{}])[0]
        addresses=[s.get('address') for s in physical.get('subnets',[])]
        if physical.get('mac_address','').lower()!=pve.client['mac'].lower() or pve.client['ip']+'/32' not in addresses or (pve.client.get('public_ipv6') and pve.client['public_ipv6']+'/128' not in addresses):fail('Le snippet réseau ne correspond pas aux IP et à la MAC du VPS.')
        if image and (image['family']!='debian' or physical.get('name')!=guest['interface']):
            network=os_network_data(guest,pve.client['ip'],pve.client['mac'],v6)
        save(snippet_directory=str(directory),cicustom=c['cicustom'],cloudinit_drive=volume(c['ide2']),old_volume=volume(c['scsi0']),source_volume=volume(tc.get('scsi0','')),disk_gb=hw['disk_gb'],node=pve.node,storage=storage,network=network,phase='prepared')
    if pve.node!=plan['node']:fail('Le VPS a changé de nœud pendant la réinstallation ; intervention administrateur requise.')
    if pve.status()['status']!='stopped' and plan['phase']!='starting':
        progress('shutdown','Arrêt du VPS avant remplacement du disque…')
        pve.wait_task(pve.api('create','/status/shutdown',timeout=60,forceStop=1),90,'Arrêt du VPS non confirmé.')
        pve.wait_power('stopped',30,'Arrêt du VPS non confirmé.')
    if plan['phase']!='starting' and pve.status()['status']!='stopped':fail('VPS non arrêté ; remplacement refusé.')
    c=read()
    if not plan.get('new_volume'):
        progress('importing','Préparation du disque neuf depuis le template…');save(phase='importing')
        if 'scsi30' not in c:
            spec=plan['storage']+(':'+str(plan['disk_gb']) if custom else ':0,import-from='+plan['source_volume'])+',serial='+token
            c=set_config(scsi30=spec)
        if ',serial='+token not in c.get('scsi30',''):fail('Disque de préparation non reconnu.')
        new=volume(c['scsi30'])
        if not own(new) or new==plan['old_volume']:fail('Identité du nouveau disque invalide.')
        save(new_volume=new,phase='imported')
    if plan['phase']!='starting':
        progress('configuring','Configuration des nouveaux accès et du réseau public…')
        user={'name':access['username'],'shell':'/bin/bash','groups':['sudo'],'sudo':['ALL=(ALL) NOPASSWD:ALL'],'lock_passwd':access['ssh_auth']=='key'}
        if access['public_key']:user['ssh_authorized_keys']=[access['public_key']]
        if access['password']:
            stdin,stdout,stderr=pve.ssh.exec_command('openssl passwd -6 -stdin',timeout=20)
            stdin.write(access['password']+'\n');stdin.flush();stdin.channel.shutdown_write()
            hashed=stdout.read(4096).decode().strip()
            if stdout.channel.recv_exit_status() or not re.fullmatch(r'\$6\$[./A-Za-z0-9$]+',hashed):fail('Impossible de préparer le mot de passe invité.')
            user['hashed_passwd']=hashed
        data={'users':[user],'disable_root':True,'ssh_pwauth':access['ssh_auth']!='key','chpasswd':{'expire':False},'ssh_deletekeys':True,'fqdn':pve.client['fqdn'],'hostname':pve.client['fqdn'],'prefer_fqdn_over_hostname':True,'packages':['keyboard-configuration','console-setup'],'write_files':[{'path':'/etc/default/keyboard','permissions':'0644','content':'XKBMODEL="pc105"\nXKBLAYOUT="fr"\nXKBVARIANT=""\nXKBOPTIONS=""\n'},{'path':'/etc/default/grub.d/99-virtualwan-console.cfg','permissions':'0644','content':'GRUB_TERMINAL=console\nGRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX} console=tty0"\n'}], 'runcmd':[['setupcon','--force'],['sh','-c','command -v update-grub >/dev/null && update-grub || true'],['systemctl','enable','--now','getty@tty1.service']]}
        if pve.client.get('public_ipv6'):data['write_files']+=ipv6_cloud_files(guest['interface'])
        data=os_user_data(guest,pve.client['fqdn'],pve.client['ip'],pve.client['mac'],access,user.get('hashed_passwd',''),v6)
        # Only overwrite the canonical files already verified before any disk mutation.
        with pve.ssh.open_sftp() as sftp:
            for suffix,content in [('network',json.dumps(plan['network'])),('user','#cloud-config\n'+json.dumps(data))]:
                dest=str(PurePosixPath(plan['snippet_directory'])/f'virtualwan-{vmid}-{suffix}.yaml');tmp=dest+'.'+plan['id']+'.tmp'
                with sftp.file(tmp,'w') as f:f.write(content+'\n')
                sftp.chmod(tmp,0o600);sftp.posix_rename(tmp,dest)
        save(phase='replacing')
        c=read()
        if volume(c.get('scsi0',''))==plan['old_volume']:c=set_config(delete='scsi0')
        if 'scsi30' in c:
            if volume(c['scsi30'])!=plan['new_volume']:fail('Disque de préparation modifié.')
            c=set_config(delete='scsi30')
        if not c.get('scsi0'):c=set_config(scsi0=plan['new_volume'])
        if volume(c.get('scsi0',''))!=plan['new_volume']:fail('Le nouveau disque système n’a pas été confirmé.')
        pve.command(['pvesh','set',pve.base+'/resize','--disk','scsi0','--size',str(plan['disk_gb'])+'G'])
        c=read()
        if c.get('scsi1') and 'media=cdrom' not in c['scsi1']:fail('scsi1 est un disque existant ; aucun remplacement automatique.')
        if c.get('scsi1','none').split(',')[0]!='none':
            from .media import owned
            if not owned(pve,c['scsi1'].split(',')[0]):fail('Retirer le CD administrateur avant réinstallation.')
        options={'boot':boot,'vga':'std','keyboard':'fr','ciuser':access['username'],'scsi1':(plan['custom_iso'] if custom else 'none')+',media=cdrom'}
        obsolete=[key for key in ('serial0','cipassword','sshkeys') if key in c]
        if obsolete:options['delete']=','.join(obsolete)
        c=set_config(**options)
        # This endpoint prints a human progress line on success, not JSON.
        pve.command(['pvesh','set',pve.base+'/cloudinit'])
        # Persist replacement identity before deleting only the exact old owned volume.
        for key,spec in list(c.items()):
            if re.fullmatch(r'unused\d+',key) and volume(spec)==plan['old_volume']:
                if not own(volume(spec)):fail('Ancien disque hors de la VM.')
                c=set_config(delete=key)
        if any(volume(v)==plan['old_volume'] for v in c.values() if isinstance(v,str)):fail('La suppression de l’ancien disque n’a pas été confirmée.')
        if pve.hardware()['disk_gb']!=plan['disk_gb'] or c.get('boot')!=boot:fail('Ressources ou démarrage non confirmés.')
        save(phase='starting')
    c=read()
    if volume(c.get('scsi0',''))!=plan['new_volume'] or c.get('boot')!=boot:fail('L’identité du système neuf a changé ; démarrage refusé.')
    progress('starting','Disque neuf prêt. Démarrage du VPS…')
    if pve.status()['status']!='running':
        pve.wait_task(pve.api('create','/status/start'),120,'Disque neuf installé, mais démarrage non confirmé. Reprendre la réinstallation sans nouvel effacement.')
    pve.wait_power('running',30,'Démarrage non confirmé. Reprendre sans nouvel effacement.')
