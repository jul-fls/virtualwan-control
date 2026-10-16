import io,json,os,tempfile,unittest
from unittest.mock import MagicMock,patch
import test_admin_management as fixtures
from virtualwan.adapters.client_pve import ClientPVE
from virtualwan.adapters.reinstall import fresh_install
from virtualwan.management import deployment
from virtualwan import domain

class ReinstallPortal(unittest.TestCase):
    setUp=fixtures.AdminManagement.setUp
    tearDown=fixtures.AdminManagement.tearDown
    def payload(self,**extra):return {'confirmation':'REINSTALLER','hostname':deployment(self.store,self.vm)['fqdn'],'erase_system_disk':True,'ssh_auth':'password','password':'FreshGuestPassword123',**extra}
    def job(self,client,headers,body=None):
        r=client.post(('/api/admin' if client is self.admin else '/api/portal')+'/vps/'+self.vm+'/reinstall',json=body or self.payload(),headers=headers)
        self.assertEqual(r.status_code,202,r.json);return self.store.one('SELECT * FROM client_operations WHERE id=?',(r.json['operation_id'],))
    def test_confirm_auth_and_permission_before_recording_destructive_intent(self):
        for body in [self.payload(confirmation=''),self.payload(hostname='wrong'),self.payload(erase_system_disk=False),self.payload(password='short'),self.payload(ssh_auth='key',public_key='bad'),self.payload(username='root')]:
            r=self.client.post('/api/portal/vps/'+self.vm+'/reinstall',json=body,headers=self.client_headers);self.assertEqual(r.status_code,400,r.json)
        self.assertFalse(deployment(self.store,self.vm).get('reinstall_pending'))
        self.assertEqual(self.client.post(self.base+'/reinstall',json=self.payload(),headers=self.client_headers).status_code,401)
        self.assertEqual(self.admin.post(self.base+'/reinstall',json=self.payload()).status_code,403)
    def test_both_roles_reinstall_keep_identity_firewall_lock_and_no_plaintext_secrets(self):
        before=deployment(self.store,self.vm)
        for client,headers in [(self.client,self.client_headers),(self.admin,self.headers)]:
            job=self.job(client,headers);self.assertEqual(job['state'],'succeeded',job)
            after=deployment(self.store,self.vm)
            for k in ('vmid','mac','ip','public_ipv6','fqdn','model_id','profile_id','account_id'):self.assertEqual(after.get(k),before.get(k),k)
            self.assertFalse(after.get('reinstall_pending'));self.assertNotIn('FreshGuestPassword123',json.dumps(after));self.assertTrue(after['reinstalled_at'])
            self.assertFalse(self.store.rows("SELECT name FROM secrets WHERE name LIKE 'REINSTALL_%'"))
        self.admin.put(self.base+'/lock',json={'locked':True,'confirmation':'CONFIRMER'},headers=self.headers)
        self.assertEqual(self.client.post('/api/portal/vps/'+self.vm+'/reinstall',json=self.payload(),headers=self.client_headers).status_code,409)
        self.assertEqual(self.job(self.admin,self.headers)['state'],'succeeded');self.assertTrue(deployment(self.store,self.vm)['management_locked'])
    def test_failed_install_resume_uses_encrypted_original_access_and_blocks_other_mutations(self):
        self.control.demo=False;self.control.allow_provision=True
        backend=MagicMock();backend.__enter__.return_value=backend;self.app.config['CLIENT_PVE_FACTORY']=lambda *args:backend
        calls=[]
        def fail(pve,plan,access,progress,checkpoint):
            calls.append((dict(plan),dict(access)));checkpoint({**plan,'phase':'starting','new_volume':'raid1-ssd:vm-120-disk-1'});raise domain.Conflict('Démarrage à reprendre')
        with patch('virtualwan.adapters.reinstall.fresh_install',side_effect=fail):job=self.job(self.client,self.client_headers)
        self.assertEqual(job['state'],'failed');pending=deployment(self.store,self.vm)['reinstall_pending'];self.assertEqual(pending['phase'],'starting')
        self.assertNotIn('FreshGuestPassword123',self.store.one('SELECT data FROM deployments WHERE id=?',(self.vm,))['data'])
        self.assertNotIn('FreshGuestPassword123',str(self.store.rows('SELECT * FROM events')))
        self.assertTrue(self.client.get('/api/portal/state').json['vps'][0]['reinstall_pending']);self.assertNotIn('new_volume',self.client.get('/api/portal/state').text)
        for suffix in ('power','rescue','graphical','console'):
            self.assertEqual(self.client.post('/api/portal/vps/'+self.vm+'/'+suffix,json={},headers=self.client_headers).status_code,409)
        for suffix in ('resize','network','onboot','notes'):
            self.assertEqual(self.admin.post(self.base+'/'+suffix,json={},headers=self.headers).status_code,409)
        self.assertEqual(self.client.post('/api/portal/vps/'+self.vm+'/reinstall/cancel',json={'confirmation':'ANNULER'},headers=self.client_headers).status_code,409)
        def resume(pve,plan,access,progress,checkpoint):
            self.assertEqual(plan,pending);self.assertEqual(access['password'],'FreshGuestPassword123')
        with patch('virtualwan.adapters.reinstall.fresh_install',side_effect=resume):self.assertEqual(self.job(self.client,self.client_headers,self.payload(password='ignored'))['state'],'succeeded')
        self.assertFalse(deployment(self.store,self.vm)['reinstall_pending'])
    def test_preflight_failure_can_be_cancelled_without_disk_changes(self):
        self.control.demo=False;self.control.allow_provision=True
        backend=MagicMock();backend.__enter__.return_value=backend;self.app.config['CLIENT_PVE_FACTORY']=lambda *a:backend
        with patch('virtualwan.adapters.reinstall.fresh_install',side_effect=domain.Conflict('Template absent')):self.assertEqual(self.job(self.admin,self.headers)['state'],'failed')
        r=self.admin.post(self.base+'/reinstall/cancel',json={'confirmation':'ANNULER'},headers=self.headers);self.assertEqual(r.status_code,200)
        self.assertFalse(deployment(self.store,self.vm)['reinstall_pending']);self.assertFalse(self.store.rows("SELECT name FROM secrets WHERE name LIKE 'REINSTALL_%'"))
    def test_existing_rescue_resize_and_active_operation_refuse_reinstall(self):
        for flag in ('rescue_state','rescue_pending','resize_pending'):
            with self.store.tx() as db:
                data=deployment(self.store,self.vm);data[flag]={'pending':True};db.execute('UPDATE deployments SET data=? WHERE id=?',(json.dumps(data),self.vm))
            self.assertEqual(self.admin.post(self.base+'/reinstall',json=self.payload(),headers=self.headers).status_code,409)
            with self.store.tx() as db:
                data=deployment(self.store,self.vm);data[flag]=None;db.execute('UPDATE deployments SET data=? WHERE id=?',(json.dumps(data),self.vm))

    def test_client_can_follow_admin_reinstall_for_own_vm_only(self):
        queue=[];self.app.config['CLIENT_TASK_RUNNER']=queue.append
        job=self.job(self.admin,self.headers)
        r=self.client.get('/api/portal/operations/'+job['id']);self.assertEqual(r.status_code,200);self.assertEqual(r.json['state'],'running')
        with self.store.tx() as db:db.execute('DELETE FROM client_vms WHERE deployment_id=?',(self.vm,))
        self.assertEqual(self.client.get('/api/portal/operations/'+job['id']).status_code,404)
        queue.pop(0)()

class Captured(io.StringIO):
    def close(self):self.content=self.getvalue();super().close()

class FakeSFTP:
    def __init__(self,network):self.network=network;self.writes={}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def file(self,path,mode='r'):
        if mode=='r':return io.BytesIO(json.dumps(self.network).encode())
        f=Captured();self.writes[path]=f;return f
    def chmod(self,*args):pass
    def posix_rename(self,*args):pass

class FreshDisk(unittest.TestCase):
    def setup_pve(self):
        cfg={'digest':'x','scsi0':'raid1-ssd:vm-120-disk-0,size=32G','ide2':'raid1-ssd:vm-120-cloudinit,media=cdrom','net0':'virtio=02:00:00:00:00:01,bridge=vmbr12,firewall=1,link_down=1','boot':'order=scsi0','cores':2,'memory':2048,'keyboard':'fr','onboot':1,'cicustom':'user=iso:snippets/virtualwan-120-user.yaml,network=iso:snippets/virtualwan-120-network.yaml'}
        profile={'config':{'pve':{'disk_storage':'raid1-ssd','snippet_storage':'iso','snippet_directory':'/mnt/pve/iso/snippets'},'guest':{'interface':'ens18'}}}
        p=ClientPVE(MagicMock(),profile,{'id':'vm','vmid':120,'mac':'02:00:00:00:00:01','ip':'192.0.2.10','fqdn':'vps-1920210.example.net'});p.node='pve2';p.base='/nodes/pve2/qemu/120';p.config=cfg.copy();p.ssh=MagicMock();sftp=FakeSFTP({'version':1,'config':[{'mac_address':p.client['mac'],'subnets':[{'address':'192.0.2.10/32'}]}]});p.ssh.open_sftp.return_value=sftp
        state={'power':'running','imports':0,'removed':[]};calls=[]
        def api(method,path,**params):
            calls.append((method,path,dict(params)))
            if path=='/snapshot':return [{'name':'current'}]
            if path=='/cluster/resources':return [{'vmid':9001,'type':'qemu','node':'pve2'}]
            if path.endswith('/9001/config'):return {'template':1,'scsi0':'raid1-ssd:base-9001-disk-0,size=8G'}
            if '/storage/' in path:return {'active':1,'avail':1024**4}
            if path=='/config':
                if method=='get':return cfg.copy()
                params.pop('digest',None)
                for name in params.pop('delete','').split(','):
                    if name in cfg:
                        spec=cfg.pop(name)
                        if name.startswith('unused'):state['removed'].append(spec)
                        elif name.startswith('scsi'):cfg['unused'+str(sum(k.startswith('unused') for k in cfg))]=spec
                for name,value in params.items():
                    if name=='scsi30' and 'import-from=' in value:
                        state['imports']+=1;value='raid1-ssd:vm-120-disk-1,size=8G,serial='+value.split('serial=')[1]
                    for unused in list(cfg):
                        if unused.startswith('unused') and cfg[unused].split(',')[0]==str(value).split(',')[0]:cfg.pop(unused)
                    cfg[name]=value
                return None
            if path=='/status/shutdown':state['power']='stopped';return 'UPID:task'
            if path=='/status/start':state['power']='running';return 'UPID:task'
        p.api=MagicMock(side_effect=api);p.check_identity=MagicMock(side_effect=lambda:setattr(p,'config',cfg.copy()));p.status=lambda:{'status':state['power']};p.wait_task=MagicMock();p.wait_power=MagicMock()
        def grow(args):
            if '/resize' in args[2]:cfg['scsi0']=cfg['scsi0'].split(',')[0]+',size=32G'
            return 'generating cloud-init ISO' if args[2].endswith('/cloudinit') else ''
        p.command=MagicMock(side_effect=grow)
        return p,cfg,state,calls
    def test_fresh_disk_replaces_old_preserves_network_and_resources(self):
        p,cfg,state,calls=self.setup_pve();saved=[];original={k:cfg[k] for k in ('net0','cores','memory','onboot')}
        fresh_install(p,{'id':'a'*32,'template':9001,'phase':'checking'},{'username':'debian','ssh_auth':'key','public_key':'ssh-ed25519 AAA','password':''},lambda *a:None,lambda v:saved.append(v))
        self.assertEqual(state['imports'],1);self.assertEqual(state['removed'],['raid1-ssd:vm-120-disk-0,size=32G']);self.assertEqual(cfg['scsi0'],'raid1-ssd:vm-120-disk-1,size=32G');self.assertEqual(state['power'],'running');self.assertEqual(saved[-1]['phase'],'starting')
        for k,v in original.items():self.assertEqual(cfg[k],v)
        self.assertTrue(any(s['phase']=='replacing' for s in saved));self.assertFalse(any(k.startswith('unused') for k in cfg));self.assertEqual(cfg['vga'],'std')
        # A retry after start failure never imports, overwrites snippets or erases again.
        p.command.reset_mock();p.ssh.open_sftp.reset_mock();fresh_install(p,saved[-1],{'username':'debian','ssh_auth':'key','public_key':'ssh-ed25519 AAA','password':''},lambda *a:None,lambda v:None)
        self.assertEqual(state['imports'],1);p.command.assert_not_called();p.ssh.open_sftp.assert_not_called();self.assertEqual(len(state['removed']),1)
    def test_extra_disks_snapshots_foreign_snippets_and_template_refused_before_shutdown(self):
        for change in ('extra','unused','foreign','template','snapshot','efi'):
            p,cfg,state,calls=self.setup_pve()
            if change=='extra':cfg['scsi1']='raid1-ssd:vm-120-disk-9,size=1G'
            if change=='unused':cfg['unused0']='raid1-ssd:vm-120-disk-9,size=1G'
            if change=='foreign':cfg['cicustom']='user=iso:snippets/foreign.yaml'
            if change=='efi':cfg['bios']='ovmf'
            original=p.api.side_effect
            if change in ('template','snapshot'):
                def api(method,path,_change=change,**params):
                    if _change=='template' and path.endswith('/9001/config'):return {'scsi0':'raid1-ssd:base-9001-disk-0'}
                    if _change=='snapshot' and path=='/snapshot':return [{'name':'keep'}]
                    return original(method,path,**params)
                p.api.side_effect=api
            with self.assertRaises(domain.Conflict):fresh_install(p,{'id':'a'*32,'template':9001,'phase':'checking'},{},lambda *a:None,lambda v:None)
            self.assertEqual(state['imports'],0);self.assertEqual(state['power'],'running');self.assertEqual(state['removed'],[])

    def test_resume_each_interrupted_disk_mutation_never_erases_new_system(self):
        for boundary in ('import','detach_old','detach_stage','attach_new','delete_old'):
            with self.subTest(boundary=boundary):
                p,cfg,state,calls=self.setup_pve();saved=[];original=p.api.side_effect;failed=[False]
                def api(method,path,**params):
                    result=original(method,path,**params)
                    hit=method=='set' and path=='/config' and ((boundary=='import' and 'import-from=' in params.get('scsi30','')) or (boundary=='detach_old' and params.get('delete')=='scsi0') or (boundary=='detach_stage' and params.get('delete')=='scsi30') or (boundary=='attach_new' and params.get('scsi0','').startswith('raid1-ssd:vm-120-disk-1')) or (boundary=='delete_old' and str(params.get('delete','')).startswith('unused')))
                    if hit and not failed[0]:failed[0]=True;raise domain.Conflict('Simulated lost acknowledgement')
                    return result
                p.api.side_effect=api;access={'username':'debian','ssh_auth':'key','public_key':'ssh-ed25519 AAA','password':''}
                with self.assertRaises(domain.Conflict):fresh_install(p,{'id':'a'*32,'template':9001,'phase':'checking'},access,lambda *a:None,lambda v:saved.append(v))
                self.assertTrue(saved)
                fresh_install(p,saved[-1],access,lambda *a:None,lambda v:saved.append(v))
                self.assertEqual(state['imports'],1);self.assertEqual(len(state['removed']),1);self.assertEqual(cfg['scsi0'],'raid1-ssd:vm-120-disk-1,size=32G');self.assertEqual(state['power'],'running')
    def test_start_failure_resumes_without_disk_or_credential_rewrite(self):
        p,cfg,state,calls=self.setup_pve();saved=[];original=p.wait_task
        def fail_start(upid,*args):
            if state['power']=='running':raise domain.Conflict('Start acknowledgement lost')
        p.wait_task.side_effect=fail_start;access={'username':'debian','ssh_auth':'key','public_key':'ssh-ed25519 AAA','password':''}
        with self.assertRaises(domain.Conflict):fresh_install(p,{'id':'a'*32,'template':9001,'phase':'checking'},access,lambda *a:None,lambda v:saved.append(v))
        self.assertEqual(saved[-1]['phase'],'starting');p.wait_task=MagicMock();p.ssh.open_sftp.reset_mock();p.command.reset_mock()
        fresh_install(p,saved[-1],access,lambda *a:None,lambda v:None);self.assertEqual(state['imports'],1);p.command.assert_not_called();p.ssh.open_sftp.assert_not_called()
    def test_changed_new_disk_rejected_on_start_retry(self):
        p,cfg,state,calls=self.setup_pve();saved=[];access={'username':'debian','ssh_auth':'key','public_key':'ssh-ed25519 AAA','password':''}
        fresh_install(p,{'id':'a'*32,'template':9001,'phase':'checking'},access,lambda *a:None,lambda v:saved.append(v))
        cfg['scsi0']='raid1-ssd:vm-120-disk-0,size=32G'
        with self.assertRaises(domain.Conflict):fresh_install(p,saved[-1],access,lambda *a:None,lambda v:None)
        self.assertEqual(state['imports'],1)

    def test_new_password_is_hashed_and_dual_stack_network_is_preserved(self):
        p,cfg,state,calls=self.setup_pve();p.client['public_ipv6']='2001:db8:123::120'
        sftp=p.ssh.open_sftp.return_value;sftp.network['config'][0]['subnets'].append({'type':'static6','address':p.client['public_ipv6']+'/128','gateway':'fe80::1'})
        stdin,stdout,stderr=MagicMock(),MagicMock(),MagicMock();stdout.read.return_value=b'$6$someSalt$SomeHashedValue123';stdout.channel.recv_exit_status.return_value=0;p.ssh.exec_command.return_value=(stdin,stdout,stderr)
        saved=[];fresh_install(p,{'id':'a'*32,'template':9001,'phase':'checking'},{'username':'newuser','ssh_auth':'both','public_key':'ssh-ed25519 AAA','password':'NewGuestPassword123'},lambda *a:None,lambda v:saved.append(v))
        user=json.loads(next(f.content for path,f in sftp.writes.items() if '-user.yaml.' in path).split('\n',1)[1]);self.assertTrue(user['ssh_pwauth']);self.assertTrue(user['ssh_deletekeys']);self.assertEqual(user['users'][0]['name'],'newuser');self.assertEqual(user['users'][0]['hashed_passwd'],'$6$someSalt$SomeHashedValue123');self.assertNotIn('NewGuestPassword123',json.dumps(user));self.assertEqual(saved[-1]['network'],sftp.network);stdin.write.assert_called_once_with('NewGuestPassword123\n')

    def test_changed_node_or_cloudinit_refused_during_start_retry(self):
        for change in ('node','media'):
            with self.subTest(change=change):
                p,cfg,state,calls=self.setup_pve();saved=[];access={'username':'debian','ssh_auth':'key','public_key':'ssh-ed25519 AAA','password':''}
                fresh_install(p,{'id':'a'*32,'template':9001,'phase':'checking'},access,lambda *a:None,lambda v:saved.append(v))
                if change=='node':p.node='pve3'
                else:cfg['cicustom']='user=iso:snippets/foreign.yaml'
                p.command.reset_mock()
                with self.assertRaises(domain.Conflict):fresh_install(p,saved[-1],access,lambda *a:None,lambda v:None)
                p.command.assert_not_called();self.assertEqual(state['imports'],1)
