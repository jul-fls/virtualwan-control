"""Restricted Proxmox operations over the already pinned management SSH channel."""
import json
from pathlib import Path
import re
import secrets
import shlex
import time
import paramiko
from .provision import require_ssh_host_key
from .. import domain
from ..store import ident


class GraphicalPreparationError(domain.Conflict):
    """An actionable, customer-safe failure of a specific preparation phase."""


class ClientPVE:
    def __init__(self,store,profile,client):
        self.store=store;self.profile=profile;self.client=client;self.ssh=None
    def __enter__(self):
        cfg=dict(self.profile['config']['pve']);cfg['known_hosts']=str(self.store.root/'ssh-known-hosts');require_ssh_host_key(cfg)
        self.ssh=paramiko.SSHClient();self.ssh.load_host_keys(cfg['known_hosts']);self.ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        private=cfg.get('private_key')
        if private:
            private=(self.store.root/private).resolve()
            if not private.is_relative_to(self.store.root):raise ValueError('Clé SSH hors du volume privé.')
        try:
            self.ssh.connect(cfg['host'],port=int(cfg['port']),username=cfg['username'],password=self.store.secret_get(cfg.get('password_env')) if not private else None,key_filename=str(private) if private else None,passphrase=self.store.secret_get(cfg.get('key_passphrase_env')),look_for_keys=False,allow_agent=False,timeout=15,banner_timeout=15,auth_timeout=15)
            self.prefix=['sudo','-n'] if cfg.get('sudo') else []
            self.check_identity();return self
        except BaseException:
            self.ssh.close();raise
    def __exit__(self,*args):
        if self.ssh:self.ssh.close()
    def command(self,args):
        timeout=150 if len(args)>2 and args[0]=='pvesh' and args[1] in ('create','set','delete') else 45
        _,out,err=self.ssh.exec_command(shlex.join(self.prefix+args),timeout=timeout)
        raw=out.read(2*1024*1024).decode();errors=err.read(65536).decode()
        code=out.channel.recv_exit_status()
        if code:
            self.store.event(self.client.get('_operation_run',self.client.get('id') or ident()),'client.pve.command.failed',exit_code=code,stderr=errors)
            raise domain.Conflict('Proxmox a refusé cette opération. Consulter le journal administrateur.')
        return raw
    def api(self,method,suffix='',**params):
        path=suffix if suffix.startswith(('/nodes/','/cluster/')) else self.base+suffix
        args=['pvesh',method,path,'--output-format','json']
        for k,v in params.items():args+=['--'+k,str(int(v) if isinstance(v,bool) else v)]
        run=self.client.get('_operation_run',self.client.get('id') or ident())
        self.store.event(run,'client.pve.request',method=method,path=path,parameters=params)
        try:
            raw=self.command(args)
            # pvesh config updates may print a progress line instead of JSON.
            # Only accept the known notification on a successful config mutation.
            if method=='set' and suffix=='/config' and raw.startswith('update VM '+str(int(self.client['vmid']))+':'):
                # Description values can contain newlines in this notification.
                raw=''
            try:result=json.loads(raw) if raw.strip() else None
            except json.JSONDecodeError:
                # Forked PVE workers can inherit stdout and precede their JSON
                # task ID with progress output. Reads remain strict JSON.
                if method not in ('create','delete'):raise
                lines=raw.strip().splitlines()
                result=json.loads(lines[-1])
                if not isinstance(result,str) or not re.fullmatch(r'UPID:[A-Za-z0-9_.:@!+-]{10,250}',result):raise
                self.store.event(run,'client.pve.task.output',message='\n'.join(lines[:-1]))
        except Exception as error:
            self.store.event(run,'client.pve.failed',method=method,path=path,error_type=type(error).__name__);raise
        self.store.event(run,'client.pve.complete',method=method,path=path)
        return result
    def check_identity(self):
        vmid=domain.positive(self.client['vmid'],999999999)
        resources=self.api('get','/cluster/resources',type='vm')
        vm=next((v for v in resources if v.get('type')=='qemu' and v.get('vmid')==vmid),None)
        if not vm:raise domain.Conflict('VM introuvable sur ce cluster.')
        with self.ssh.open_sftp() as sftp:
            with sftp.file('/etc/pve/virtualwan-isolation.json') as stream:registry=json.loads(stream.read(2*1024*1024))
        segment=self.profile['config']['pve'].get('isolation_segment');reg=registry.get('segments',{}).get(segment) if segment else registry
        if not reg or vm['node'] not in reg['nodes']:raise domain.Conflict('VM hors des nœuds gérés.')
        known=next((c for c in reg['clients'] if c.get('vmid')==vmid),None)
        if not known or known.get('mac','').lower()!=self.client.get('mac','').lower() or known.get('public_ip')!=self.client.get('ip'):raise domain.Conflict('Identité réseau de la VM différente du registre.')
        self.node=vm['node'];self.reg=reg;self.base=f'/nodes/{self.node}/qemu/{vmid}'
        self.config=self.api('get','/config');net=self.config.get('net0','')
        if self.config.get('template') or not re.search(r'(?:^|,)virtio='+re.escape(self.client['mac'])+r'(?:,|$)',net,re.I) or not re.search(r'(?:^|,)bridge='+re.escape(reg['nodes'][self.node]['bridge'])+r'(?:,|$)',net):raise domain.Conflict('Identité Proxmox différente ; opération refusée.')
    def status(self):
        r=self.api('get','/status/current')
        return {k:r.get(k) for k in ('status','uptime','cpu','mem','maxmem','cpus','disk','maxdisk','netin','netout','diskread','diskwrite')}
    def task(self,action):
        if action not in ('start','shutdown','reboot','stop','reset'):raise ValueError('Action inconnue.')
        result=self.api('create','/status/'+action)
        return {'task':result,'node':self.node}
    def task_status(self,upid):
        if not re.fullmatch(r'UPID:[A-Za-z0-9_.:@!+-]{10,250}',upid):raise ValueError('Tâche invalide.')
        node=upid.split(':')[1]
        if node not in self.reg['nodes']:raise ValueError('Tâche hors des nœuds gérés.')
        result=self.api('get',f'/nodes/{node}/tasks/{upid}/status')
        return {k:result.get(k) for k in ('status','exitstatus')}
    def firewall(self):
        options=self.api('get','/firewall/options') or {};rules=self.api('get','/firewall/rules') or []
        cluster=self.api('get','/cluster/firewall/options') or {}
        rules=[{**r,'comment':re.sub(r'^VWAN-CLIENT [a-f0-9]{8}: ','VWAN-CLIENT ',r.get('comment',''))} for r in rules]
        return {'enabled':bool(options.get('enable')),'cluster_enabled':bool(cluster.get('enable')),'nic_enabled':'firewall=1' in self.config.get('net0',''),'policy_in':options.get('policy_in','ACCEPT'),'rules':[r for r in rules if r.get('comment','').startswith('VWAN-CLIENT ')],'other_rules':sum(not r.get('comment','').startswith('VWAN-CLIENT ') for r in rules)}
    def set_firewall(self,body):
        rules=validate_rules(body.get('rules',[]));enabled=body.get('enabled')
        if not isinstance(enabled,bool) or body.get('policy_in') not in ('ACCEPT','DROP'):raise ValueError('Politique de pare-feu invalide.')
        old=self.firewall()
        if enabled and not old['cluster_enabled']:raise domain.Conflict('Le pare-feu Proxmox global doit être activé par l’administrateur. Aucun réglage cluster/nœud n’est modifié ici.')
        # Stage a new disabled generation while the previous rules remain active.
        # Proxmox POST prepends; explicitly move each rule behind administrator rules.
        current=self.api('get','/firewall/rules') or []
        admin=lambda rows:[{k:v for k,v in r.items() if k not in ('digest','pos')} for r in rows if not r.get('comment','').startswith('VWAN-CLIENT ')]
        baseline=admin(current);count=len(baseline)
        if any(r.get('comment','').startswith('VWAN-CLIENT ') for r in current[:count]):raise domain.Conflict('Les règles administrateur doivent précéder les règles clientes ; demander leur réorganisation à l’hébergeur.')
        run=self.client.get('_operation_run',self.client.get('id') or ident())
        generation='VWAN-CLIENT '+ident()[:8]+': '
        self.store.event(run,'client.firewall.before',rules=current,options=old)
        for index,rule in enumerate(rules):
            staged={**rule,'enable':0,'comment':generation+rule['comment'].removeprefix('VWAN-CLIENT ')}
            self.api('create','/firewall/rules',**staged)
            current=self.api('get','/firewall/rules') or []
            if admin(current)!=baseline or not current or current[0].get('comment')!=staged['comment']:raise domain.Conflict('Les règles ont changé pendant leur mise à jour ; relire avant de réessayer.')
            position=count+index
            if position:self.api('set','/firewall/rules/0',moveto=position,digest=current[0]['digest'])
        for index in range(len(rules)):
            current=self.api('get','/firewall/rules') or [];position=count+index
            if admin(current)!=baseline or not current[position].get('comment','').startswith(generation):raise domain.Conflict('Les règles ont changé pendant leur mise à jour.')
            self.api('set','/firewall/rules/'+str(position),enable=1,digest=current[position]['digest'])
        while True:
            current=self.api('get','/firewall/rules') or []
            if admin(current)!=baseline:raise domain.Conflict('Les règles administrateur ont changé ; relire avant de réessayer.')
            old_rules=[r for r in current if r.get('comment','').startswith('VWAN-CLIENT ') and not r['comment'].startswith(generation)]
            if not old_rules:break
            row=old_rules[-1];self.api('delete','/firewall/rules/'+str(row['pos']),digest=row['digest'])
        if enabled and not old['nic_enabled']:
            net=re.sub(r',firewall=[01](?=,|$)','',self.config['net0'])+',firewall=1'
            self.api('set','/config',net0=net,digest=self.config['digest'])
        self.api('set','/firewall/options',enable=enabled,policy_in=body['policy_in'])
        return self.firewall()
    def wait_task(self,upid,timeout,message):
        if not isinstance(upid,str) or not upid.startswith('UPID:'):raise GraphicalPreparationError(message)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            status=self.task_status(upid)
            if status['status']=='stopped':
                if status.get('exitstatus')!='OK':raise GraphicalPreparationError(message)
                return
            time.sleep(1)
        raise GraphicalPreparationError(message)
    def wait_power(self,expected,timeout,message):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if self.status()['status']==expected:return
            time.sleep(1)
        raise GraphicalPreparationError(message)
    def graphical(self,progress=lambda phase,message:None):
        progress('checking','Vérification de l’état du VPS…')
        current=self.status()['status'];was_running=current=='running';stopped=False;configured=False
        if current not in ('running','stopped'):raise GraphicalPreparationError('Le VPS est occupé ou suspendu. Reprendre son fonctionnement normal avant de préparer la console.')
        try:
            if was_running:
                progress('shutdown','Arrêt propre du VPS en cours…')
                task=self.api('create','/status/shutdown',timeout=120,forceStop=0)
                message='Le VPS ne s’est pas arrêté proprement dans le délai prévu. Aucun arrêt forcé ni changement VGA n’a été effectué. Éteignez-le depuis son système ou utilisez « Forcer l’arrêt », puis relancez la préparation.'
                self.wait_task(task,135,message);self.wait_power('stopped',15,message)
            stopped=True
            progress('configuring','VPS arrêté. Configuration de l’affichage VGA…')
            # Refresh the node, ownership and config digest after the shutdown task.
            self.check_identity()
            if self.status()['status']!='stopped':raise GraphicalPreparationError('Le VPS a été rallumé pendant la préparation. Relancer la préparation sans autre action d’alimentation en parallèle.')
            params={'vga':'std','digest':self.config['digest']}
            if 'serial0' in self.config:params['delete']='serial0'
            self.api('set','/config',**params)
            actual=self.api('get','/config')
            if actual.get('vga','').split(',')[0]!='std' or 'serial0' in actual:raise GraphicalPreparationError('La modification VGA n’a pas été confirmée par Proxmox. La configuration précédente doit être vérifiée avant une nouvelle tentative.')
            configured=True
            progress('starting','Affichage VGA prêt. Démarrage du VPS…')
            self.check_identity()
            task=self.api('create','/status/start')
            message='L’affichage VGA est configuré, mais Proxmox n’a pas confirmé le démarrage. Utilisez « Démarrer » pour réessayer ; aucune réinstallation n’a été effectuée.'
            self.wait_task(task,90,message);self.wait_power('running',30,message)
            progress('ready','Console VGA prête. Le VPS est allumé.')
            return {'ok':True,'configured':True,'restarted':True}
        except Exception as error:
            self.store.event(self.client.get('_operation_run',ident()),'client.graphical.phase.failed',error_type=type(error).__name__,error=str(error))
            # If configuration failed after we stopped a running guest, restore power
            # when possible rather than silently leaving it down. Never force-stop.
            if was_running and stopped and not configured:
                progress('recovering','Préparation interrompue. Tentative de remise en marche du VPS…')
                try:
                    self.check_identity()
                    if self.status()['status']=='stopped':
                        task=self.api('create','/status/start');self.wait_task(task,90,'Le redémarrage de secours n’a pas été confirmé.')
                    self.wait_power('running',15,'Le redémarrage de secours n’a pas été confirmé.')
                except Exception as recovery:
                    self.store.event(self.client.get('_operation_run',ident()),'client.graphical.recovery.failed',error=str(recovery))
                    raise GraphicalPreparationError('La conversion VGA et la remise en marche n’ont pas abouti. Utilisez « Démarrer » ; les détails sont conservés dans le journal de cette opération.') from error
                raise GraphicalPreparationError('La conversion VGA a échoué ; le VPS a été remis en marche. Vous pouvez relancer la préparation. Les détails techniques sont conservés dans le journal.') from error
            if isinstance(error,GraphicalPreparationError):raise
            if configured:raise GraphicalPreparationError('L’affichage VGA est configuré, mais le démarrage n’a pas été confirmé. Vérifiez l’état du VPS et utilisez « Démarrer » si nécessaire.') from error
            raise GraphicalPreparationError('La préparation VGA a été interrompue avant confirmation de la modification. Les détails sont conservés dans le journal de cette opération.') from error
    @staticmethod
    def system_boot(config):
        boot=config.get('boot','')
        order=re.search(r'(?:^|,)order=([^,]+)',boot)
        disks=[k for k,v in config.items() if re.fullmatch(r'(?:scsi|virtio|sata|ide)\d+',k) and 'media=cdrom' not in str(v) and str(v)!='none']
        devices=[d for d in (order.group(1).split(';') if order else [config.get('bootdisk','')]) if d in disks]
        return 'order='+';'.join(dict.fromkeys(devices+sorted(disks))) if disks else None
    def rescue(self,iso,disable=False,saved=None,password=None,progress=lambda *a:None,checkpoint=lambda *a:None):
        progress('checking','Vérification du démarrage et des médias du VPS…')
        if not disable:
            if not re.fullmatch(r'[A-Za-z0-9_-]+:iso/[A-Za-z0-9_.-]+\.iso',iso):raise domain.Conflict('ISO rescue non configurée par l’administrateur.')
            if not password:raise domain.Conflict('Les identifiants rescue doivent être générés par le portail.')
            volumes=self.api('get',f'/nodes/{self.node}/storage/{iso.split(":")[0]}/content',content='iso')
            if not any(v.get('volid')==iso for v in volumes):raise domain.Conflict('ISO rescue absente de ce nœud.')
        current=self.status()['status']
        if current not in ('running','stopped'):raise domain.Conflict('Le VPS est suspendu ou occupé. Reprendre son fonctionnement avant le basculement rescue.')
        if current=='running':
            progress('shutdown','Arrêt automatique du VPS en cours…')
            task=self.api('create','/status/shutdown',timeout=60,forceStop=1)
            message='L’arrêt du VPS n’a pas été confirmé. Réessayez le basculement ; aucun média ne sera modifié tant que le VPS tourne.'
            self.wait_task(task,80,message);self.wait_power('stopped',15,message)
        self.check_identity()
        if self.status()['status']!='stopped':raise domain.Conflict('Le VPS a été rallumé pendant le basculement. Réessayez sans autre action d’alimentation.')
        progress('configuring','VPS arrêté. Configuration des médias et de l’ordre de démarrage…')
        result=self.configure_rescue(iso,disable,saved,password,checkpoint)
        progress('starting','Démarrage du VPS sur '+('son disque système…' if disable else 'le système rescue…'))
        self.check_identity()
        task=self.api('create','/status/start')
        message='Le démarrage est configuré et enregistré, mais le VPS n’a pas confirmé son redémarrage. Réessayez ou utilisez Démarrer.'
        self.wait_task(task,90,message);self.wait_power('running',30,message)
        progress('ready','VPS redémarré '+('sur son disque système.' if disable else 'en mode rescue.'))
        return result
    def configure_rescue(self,iso,disable=False,saved=None,password=None,checkpoint=lambda *a:None):
        from .rescue_media import media
        saved=dict(saved or {})
        if self.status()['status']!='stopped':raise domain.Conflict('Le VPS doit être arrêté avant de modifier son démarrage.')
        def refresh():
            self.config=self.api('get','/config');return self.config
        def cd_matches(key,value):
            current=self.config.get(key,'')
            return current.split(',')[0]==value.split(',')[0] and 'media=cdrom' in current.split(',')
        if disable:
            if not saved.get('ide0'):raise domain.Conflict('La configuration normale à restaurer est absente.')
            auth=saved.get('credential_volid');detached=not self.config.get('ide0') and (not auth or not self.config.get('ide1'))
            if not detached:
                if not cd_matches('ide0',saved['ide0']) or (auth and not cd_matches('ide1',auth)):raise domain.Conflict('Les lecteurs rescue ont changé ; intervention administrateur requise.')
                # Restore boot before detaching devices referenced by its old order.
                params={'digest':self.config['digest']}
                if saved.get('boot'):params['boot']=saved['boot']
                else:params['delete']='boot'
                self.api('set','/config',**params);refresh()
                self.api('set','/config',delete='ide0'+(',ide1' if auth else ''),digest=self.config['digest']);refresh()
            if self.config.get('ide0') or (auth and self.config.get('ide1')) or self.config.get('boot')!=saved.get('boot'):raise domain.Conflict('Le retour au disque système n’a pas été confirmé. Réessayez.')
            checkpoint(saved,True)
            if auth:media(self,'delete',saved=saved)
            return saved
        if not re.fullmatch(r'[A-Za-z0-9_-]+:iso/[A-Za-z0-9_.-]+\.iso',iso):raise domain.Conflict('ISO rescue non configurée par l’administrateur.')
        if not password:raise domain.Conflict('Les identifiants rescue doivent être générés par le portail.')
        volumes=self.api('get',f'/nodes/{self.node}/storage/{iso.split(":")[0]}/content',content='iso')
        if not any(v.get('volid')==iso for v in volumes):raise domain.Conflict('ISO rescue absente de ce nœud.')
        old=None
        if not saved:
            if self.config.get('ide0') or self.config.get('ide1'):
                # Recover the previous bug only when both CDs demonstrably belong
                # to this VM and this configured shared image. Never adopt arbitrary CDs.
                if not cd_matches('ide0',iso):raise domain.Conflict('Le lecteur ide0 est occupé par un autre périphérique.')
                match=re.fullmatch(re.escape(iso.split(':')[0])+r':iso/virtualwan-rescue-auth-'+str(int(self.client['vmid']))+r'-([a-f0-9]{32})\.iso',self.config.get('ide1','').split(',')[0])
                if not match:raise domain.Conflict('Le lecteur ide1 ne possède pas un média rescue vérifiable.')
                old={'credential_volid':self.config['ide1'].split(',')[0],'credential_session':match.group(1)}
                media(self,'inspect',saved=old)
            boot=self.system_boot(self.config) if old else self.config.get('boot')
            credential=media(self,'create',iso=iso,password=password)
            saved={'boot':boot,'ide0':iso+',media=cdrom','credential_volid':credential['volid'],'credential_session':credential['session'],'rescue_boot':'order=ide0'+(';' + self.system_boot(self.config).removeprefix('order=') if self.system_boot(self.config) else '')}
            if old:saved['replaced_credential']=old
            checkpoint(saved,False)
        else:
            if saved.get('ide0','').split(',')[0]!=iso:raise domain.Conflict('L’ISO configurée a changé depuis le début du basculement.')
            old=saved.get('replaced_credential')
            if self.config.get('ide0') and not cd_matches('ide0',iso):raise domain.Conflict('Le lecteur ide0 a changé pendant le basculement.')
            allowed=[saved['credential_volid']]+([old['credential_volid']] if old else [])
            if self.config.get('ide1') and self.config['ide1'].split(',')[0] not in allowed:raise domain.Conflict('Le lecteur ide1 a changé pendant le basculement.')
            media(self,'create',iso=iso,password=password,saved=saved)
        self.store.event(self.client.get('_operation_run',self.client.get('id') or ident()),'client.rescue.plan',saved=saved)
        self.api('set','/config',ide0=saved['ide0'],ide1=saved['credential_volid']+',media=cdrom',digest=self.config['digest']);refresh()
        if not cd_matches('ide0',saved['ide0']) or not cd_matches('ide1',saved['credential_volid']):raise domain.Conflict('Les médias rescue n’ont pas été confirmés. Réessayez.')
        self.api('set','/config',boot=saved['rescue_boot'],digest=self.config['digest']);refresh()
        if self.config.get('boot')!=saved['rescue_boot']:raise domain.Conflict('L’ordre de démarrage rescue n’a pas été confirmé. Réessayez.')
        checkpoint(saved,True)
        if old:media(self,'delete',saved=old)
        return saved
    def set_notes(self,text):
        self.config=self.api('get','/config')
        self.api('set','/config',description=text,digest=self.config['digest'])
        self.config=self.api('get','/config')
        if self.config.get('description')!=text:raise domain.Conflict('Les notes Proxmox n’ont pas été confirmées.')
    def set_network(self,blocked):
        if not isinstance(blocked,bool):raise ValueError('État réseau invalide.')
        net=re.sub(r',link_down=[01](?=,|$)','',self.config['net0'])+(',link_down=1' if blocked else '')
        self.api('set','/config',net0=net,digest=self.config['digest']);self.config=self.api('get','/config')
        if ('link_down=1' in self.config.get('net0',''))!=blocked:raise domain.Conflict('La suspension réseau n’a pas été confirmée.')
    def set_onboot(self,enabled):
        if not isinstance(enabled,bool):raise ValueError('État de démarrage invalide.')
        self.api('set','/config',onboot=enabled,digest=self.config['digest']);self.config=self.api('get','/config')
        if bool(self.config.get('onboot'))!=enabled:raise domain.Conflict('Le démarrage automatique n’a pas été confirmé.')
    def hardware(self):
        disk=self.system_boot(self.config)
        disk=disk.removeprefix('order=').split(';')[0] if disk else None
        value=self.config.get(disk,'') if disk else ''
        size=re.search(r'(?:^|,)size=([0-9.]+)([KMGT]?)(?:,|$)',value)
        if not disk or not size:raise domain.Conflict('Le disque système doit avoir une taille vérifiable dans Proxmox.')
        import math
        gb=float(size.group(1))*{'':1/(1024**3),'K':1/(1024**2),'M':1/1024,'G':1,'T':1024}[size.group(2)]
        return {'cores':int(self.config.get('cores',1))*int(self.config.get('sockets',1)),'memory_mb':int(self.config.get('memory',512)),'disk_gb':math.ceil(gb),'disk':disk,'onboot':bool(self.config.get('onboot')),'network_suspended':'link_down=1' in self.config.get('net0','')}
    def resize(self,model,pending=None,progress=lambda *a:None,checkpoint=lambda *a:None):
        target=domain.model(model);actual=self.hardware();pending=dict(pending or {})
        current=self.status()['status']
        if current not in ('running','stopped'):raise domain.Conflict('Le VPS est occupé ou suspendu.')
        if not pending:
            pending={'was_running':current=='running','model':target,'disk_gb':max(actual['disk_gb'],target['disk_gb'])}
            checkpoint(pending,False)
        elif pending['model']!=target:raise domain.Conflict('Reprendre le changement de gamme déjà commencé.')
        wanted=pending['disk_gb']
        if wanted>actual['disk_gb']:
            storage=self.config[actual['disk']].split(':',1)[0]
            space=self.api('get',f'/nodes/{self.node}/storage/{storage}/status')
            if int(space.get('avail',0))<(wanted-actual['disk_gb'])*1024**3:raise domain.Conflict('Le stockage ne dispose pas de la capacité libre nécessaire à cette extension.')
        if current=='running':
            progress('shutdown','Arrêt du VPS avant changement de gamme…')
            self.wait_task(self.api('create','/status/shutdown',timeout=60,forceStop=1),80,'Arrêt du VPS non confirmé.')
            self.wait_power('stopped',15,'Arrêt du VPS non confirmé.')
        self.check_identity()
        if self.status()['status']!='stopped':raise domain.Conflict('Le VPS a été rallumé pendant le changement.')
        progress('configuring','Application des vCPU et de la mémoire…')
        self.api('set','/config',cores=target['cores'],sockets=1,memory=target['memory_mb'],balloon=0,digest=self.config['digest'])
        self.config=self.api('get','/config');actual=self.hardware()
        if actual['disk_gb']<wanted:
            progress('disk','Extension du disque virtuel…')
            # Resize is a scoped, validated pvesh mutation; verify the new size.
            raw=self.command(['pvesh','set',self.base+'/resize','--disk',actual['disk'],'--size',str(wanted)+'G','--output-format','json'])
            self.store.event(self.client.get('_operation_run',ident()),'admin.resize.disk.output',message=raw)
            self.config=self.api('get','/config');actual=self.hardware()
        if actual['cores']!=target['cores'] or actual['memory_mb']!=target['memory_mb'] or actual['disk_gb']<wanted:raise domain.Conflict('La nouvelle configuration matérielle n’a pas été confirmée.')
        checkpoint({**pending,**actual},True)
        if pending['was_running']:
            progress('starting','Redémarrage du VPS avec sa nouvelle gamme…')
            self.wait_task(self.api('create','/status/start'),90,'Nouvelle gamme appliquée ; démarrage non confirmé. Réessayez.')
            self.wait_power('running',30,'Nouvelle gamme appliquée ; démarrage non confirmé. Réessayez.')
        return actual
    def console(self,password):
        if self.status()['status']!='running':raise domain.Conflict('La VM doit être allumée pour ouvrir la console.')
        if self.config.get('vga','std').split(',')[0] in ('serial0','none'):raise domain.Conflict('Préparer la console graphique depuis la fiche du VPS.')
        args=['env','LC_PVE_TICKET='+password,'qm','vncproxy',str(self.client['vmid'])]
        if self.node!=self.profile['config']['pve']['node']:
            address=self.reg['nodes'][self.node]['address']
            args=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','HostKeyAlias='+self.node,'-o',f'UserKnownHostsFile=/etc/pve/nodes/{self.node}/ssh_known_hosts','root@'+address,shlex.join(args)]
        channel=self.ssh.get_transport().open_session(timeout=15);channel.exec_command(shlex.join(self.prefix+args));channel.settimeout(2)
        return channel


def validate_rules(rows):
    import ipaddress
    if not isinstance(rows,list) or len(rows)>50:raise ValueError('50 règles maximum.')
    result=[]
    for r in rows:
        if r.get('action') not in ('ACCEPT','DROP','REJECT') or r.get('proto') not in ('tcp','udp','icmp','ipv6-icmp',''):raise ValueError('Règle invalide.')
        rule={'type':'in','action':r['action'],'enable':1,'iface':'net0','comment':'VWAN-CLIENT '+domain.label(r.get('label') or 'Règle client')}
        if r['proto']:rule['proto']=r['proto']
        if r.get('source'):rule['source']=str(ipaddress.ip_network(r['source'],strict=False))
        port=str(r.get('dport','')).strip()
        if port:
            if r['proto'] not in ('tcp','udp') or not re.fullmatch(r'\d{1,5}(?::\d{1,5})?',port):raise ValueError('Port invalide.')
            nums=list(map(int,port.split(':')))
            if any(not 1<=n<=65535 for n in nums) or nums[0]>nums[-1]:raise ValueError('Port invalide.')
            rule['dport']=port
        result.append(rule)
    return result
