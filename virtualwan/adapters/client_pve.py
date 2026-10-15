"""Restricted Proxmox operations over the already pinned management SSH channel."""
import json
from pathlib import Path
import re
import secrets
import shlex
import paramiko
from .provision import require_ssh_host_key
from .. import domain
from ..store import ident


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
        _,out,err=self.ssh.exec_command(shlex.join(self.prefix+args),timeout=45)
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
            raw=self.command(args);result=json.loads(raw) if raw.strip() else None
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
    def graphical(self):
        if self.status()['status']!='stopped':raise domain.Conflict('Arrêter la VM avant de préparer sa console graphique.')
        # No guest disk or network change. Guest kernel must also enable tty0.
        params={'vga':'std','digest':self.config['digest']}
        if 'serial0' in self.config:params['delete']='serial0'
        self.api('set','/config',**params)
        return {'ok':True,'restart_required':True}
    def rescue(self,iso,disable=False,saved=None):
        if self.status()['status']!='stopped':raise domain.Conflict('Arrêter la VM avant de changer son démarrage rescue.')
        if disable:
            current=self.config.get('ide0','')
            if current.split(',')[0]!=saved['ide0'].split(',')[0] or 'media=cdrom' not in current.split(','):raise domain.Conflict('Le lecteur rescue a changé ; intervention administrateur requise.')
            params={'delete':'ide0','digest':self.config['digest']}
            if saved.get('boot'):params['boot']=saved['boot']
            else:params['delete']+=',boot'
            self.api('set','/config',**params);return {'ok':True}
        if not re.fullmatch(r'[A-Za-z0-9_-]+:iso/[A-Za-z0-9_.-]+\.iso',iso):raise domain.Conflict('ISO rescue non configurée par l’administrateur.')
        if self.config.get('ide0'):raise domain.Conflict('Le lecteur ide0 est occupé ; aucun périphérique existant ne sera remplacé.')
        volumes=self.api('get',f'/nodes/{self.node}/storage/{iso.split(":")[0]}/content',content='iso')
        if not any(v.get('volid')==iso for v in volumes):raise domain.Conflict('ISO rescue absente de ce nœud.')
        saved={'boot':self.config.get('boot'),'ide0':iso+',media=cdrom'}
        self.store.event(self.client.get('_operation_run',self.client.get('id') or ident()),'client.rescue.before',boot=saved['boot'],iso=iso)
        self.api('set','/config',ide0=saved['ide0'],boot='order=ide0;scsi0',digest=self.config['digest']);return saved
    def console(self,password):
        if self.status()['status']!='running':raise domain.Conflict('La VM doit être allumée pour ouvrir la console.')
        if self.config.get('vga','std').split(',')[0] in ('serial0','none'):raise domain.Conflict('Préparer la console graphique depuis la fiche du VPS (VM arrêtée).')
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
