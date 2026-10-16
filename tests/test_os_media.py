import base64,io,json,os,unittest,contextlib
from pathlib import Path
from unittest.mock import MagicMock,patch
import test_admin_management as fixtures
from virtualwan.media import MAX_BYTES,CHUNK_BYTES,media_record
from virtualwan.management import deployment
from virtualwan.guest_os import os_network_data,os_user_data,freebsd_probe_code
from virtualwan.adapters.media import identifier,owned,set_cd
from virtualwan import domain

def iso_bytes():
    data=bytearray(65536);data[32768:32775]=b'\x01CD001\x01';return bytes(data)

class MediaPortal(unittest.TestCase):
    setUp=fixtures.AdminManagement.setUp
    tearDown=fixtures.AdminManagement.tearDown
    def begin(self,client=None,headers=None,**extra):
        return (client or self.client).post(('/api/admin' if client is self.admin else '/api/portal')+'/vps/'+self.vm+'/media',json={'filename':'installer.iso','size':len(iso_bytes()),**extra},headers=headers or self.client_headers)
    def upload(self):
        r=self.begin();self.assertEqual(r.status_code,201,r.json);media=r.json['media'];base='/api/portal/vps/'+self.vm+'/media'
        r=self.client.post(base+'/chunk',json={'id':media['id'],'offset':0,'data':base64.b64encode(iso_bytes()).decode()},headers=self.client_headers);self.assertEqual(r.status_code,200,r.json)
        r=self.client.post(base+'/complete',json={'id':media['id']},headers=self.client_headers);self.assertEqual(r.status_code,202,r.json);self.assertEqual(r.json['media']['state'],'ready',r.json);return base
    def test_one_slot_quota_paths_and_csrf_enforced_before_io(self):
        for body in ({'size':MAX_BYTES+1},{'size':True},{'filename':'../private.iso'},{'filename':'installer.qcow2'}):self.assertEqual(self.begin(**body).status_code,400)
        self.assertEqual(self.client.post('/api/portal/vps/'+self.vm+'/media',json={'size':65536,'filename':'a.iso'}).status_code,403)
        self.assertEqual(self.begin().status_code,201);self.assertEqual(self.begin().status_code,409)
        self.assertEqual(len(list((self.store.root/'client-media').glob('*.part'))),1)
    def test_chunk_offsets_and_size_fail_closed_and_resume(self):
        m=self.begin().json['media'];url='/api/portal/vps/'+self.vm+'/media/chunk'
        for body,status in [({'id':m['id'],'offset':1,'data':'YQ=='},409),({'id':m['id'],'offset':0,'data':'!'},400),({'id':m['id'],'offset':0,'data':base64.b64encode(b'x'*(CHUNK_BYTES+1)).decode()},400)]:
            self.assertEqual(self.client.post(url,json=body,headers=self.client_headers).status_code,status)
        self.assertEqual(media_record(self.store,self.vm)['offset'],0)
        r=self.client.post(url,json={'id':m['id'],'offset':0,'data':'YQ=='},headers=self.client_headers);self.assertEqual(r.json['offset'],1)
        r=self.admin.get(self.base+'/media');self.assertEqual(r.json['media']['offset'],1);self.assertNotIn('volume',r.json['media'])
    def test_client_iso_never_appears_on_other_accounts(self):
        self.begin();other=self.app.test_client();account=self.accounts.save({'name':'Other','email':'other@example.net','password':'OtherPassword123'})
        r=other.post('/api/portal/login',json={'email':'other@example.net','password':'OtherPassword123'});headers={'X-CSRF-Token':r.json['csrf']}
        for suffix,method,payload in [('', 'get',None),('', 'delete',{}),('/chunk','post',{'id':media_record(self.store,self.vm)['id'],'offset':0,'data':'YQ=='})]:
            response=getattr(other,method)('/api/portal/vps/'+self.vm+'/media'+suffix,**({'json':payload,'headers':headers} if payload is not None else {}));self.assertEqual(response.status_code,404)
        self.assertIsNotNone(media_record(self.store,self.vm))
    def test_ready_iso_mount_eject_next_boot_and_delete_shared_by_admin_and_client(self):
        base=self.upload()
        for action,mounted in [('insert',True),('eject',False),('next_boot',True)]:
            r=self.admin.post(self.base+'/media/attach',json={'action':action},headers=self.headers);self.assertEqual(r.status_code,200,r.json);self.assertEqual(r.json['media']['mounted'],mounted)
        self.assertFalse(list((self.store.root/'client-media').glob('*.part')))
        r=self.client.delete(base,json={},headers=self.client_headers);self.assertEqual(r.status_code,200);self.assertIsNone(media_record(self.store,self.vm))
        self.assertEqual(self.begin().status_code,201)
    def test_invalid_iso_stays_in_single_slot_until_removed(self):
        m=self.begin().json['media'];base='/api/portal/vps/'+self.vm+'/media'
        self.client.post(base+'/chunk',json={'id':m['id'],'offset':0,'data':base64.b64encode(b'x'*65536).decode()},headers=self.client_headers)
        r=self.client.post(base+'/complete',json={'id':m['id']},headers=self.client_headers);self.assertEqual(r.json['media']['state'],'failed');self.assertEqual(self.begin().status_code,409)
    def test_lock_and_reinstall_transition_prevent_iso_mutation(self):
        self.admin.put(self.base+'/lock',json={'locked':True,'confirmation':'CONFIRMER'},headers=self.headers)
        self.assertEqual(self.begin().status_code,409);self.assertEqual(self.begin(self.admin,self.headers).status_code,201)
        with self.store.tx() as db:
            d=deployment(self.store,self.vm);data={k:v for k,v in d.items() if k not in ('id','ip','profile_id','model_id')};data['reinstall_pending']={'phase':'importing'};db.execute('UPDATE deployments SET data=? WHERE id=?',(json.dumps(data),self.vm))
        self.assertEqual(self.admin.delete(self.base+'/media',json={},headers=self.headers).status_code,409)
    def test_custom_reinstall_requires_ready_iso_and_keeps_hostname(self):
        d=deployment(self.store,self.vm);b={'confirmation':'REINSTALLER','hostname':d['fqdn'],'erase_system_disk':True,'os_image_id':'custom'}
        self.assertEqual(self.client.post('/api/portal/vps/'+self.vm+'/reinstall',json=b,headers=self.client_headers).status_code,409)
        self.upload();r=self.client.post('/api/portal/vps/'+self.vm+'/reinstall',json=b,headers=self.client_headers);self.assertEqual(r.status_code,202,r.json)
        after=deployment(self.store,self.vm);self.assertEqual(after['fqdn'],d['fqdn']);self.assertEqual(after['os_family'],'custom');self.assertTrue(media_record(self.store,self.vm)['next_boot'])
    def test_ip_release_preserves_iso_and_later_detached_vm_deletion_cleans_it(self):
        self.upload()
        run=self.control.request_retire(self.vm,{'mode':'release','confirmation':'LIBERER'});self.control.process_one()
        self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(run,))['state'],'succeeded')
        self.assertEqual(deployment(self.store,self.vm)['status'],'detached');self.assertIsNotNone(media_record(self.store,self.vm))
        run=self.control.request_retire(self.vm,{'mode':'delete','confirmation':'SUPPRIMER'});self.control.process_one()
        self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(run,))['state'],'succeeded')
        self.assertIsNone(media_record(self.store,self.vm))
    def test_retirement_cannot_orphan_an_incomplete_upload(self):
        self.begin()
        with self.assertRaises(domain.Conflict):self.control.request_retire(self.vm,{'mode':'delete_release','confirmation':'SUPPRIMER'})
        self.assertEqual(deployment(self.store,self.vm)['status'],'simulated')

class OSSelection(unittest.TestCase):
    setUp=fixtures.AdminManagement.setUp
    tearDown=fixtures.AdminManagement.tearDown
    def register(self,**extra):
        d=deployment(self.store,self.vm);p=self.store.document('profiles',d['profile_id']);self.profile=p
        body={'name':'Ubuntu test','family':'ubuntu','profile_id':p['id'],'templates':{p['config']['pve']['node']:9101},'interface':'ens18','enabled':True,'prepared':True,**extra}
        return self.admin.post('/api/os-images',json=body,headers=self.headers)
    def test_admin_publication_and_client_catalogue_scoped_by_router(self):
        self.assertEqual(self.register(prepared=False).status_code,400)
        image=self.register().json
        self.assertEqual(self.client.get('/api/os-images').status_code,401)
        catalogue=self.client.get('/api/portal/vps/'+self.vm+'/os-images');self.assertEqual(catalogue.status_code,200)
        entry=next(i for i in catalogue.json['images'] if i['id']==image['id']);self.assertNotIn('templates',entry)
        self.assertNotIn('profile_id',entry)
    def test_reinstall_os_choice_is_snapshotted_and_preserves_identity(self):
        image=self.register().json;d=deployment(self.store,self.vm)
        r=self.client.post('/api/portal/vps/'+self.vm+'/reinstall',json={'hostname':d['fqdn'],'confirmation':'REINSTALLER','erase_system_disk':True,'os_image_id':image['id'],'ssh_auth':'password','password':'NewSystemPassword123'},headers=self.client_headers)
        self.assertEqual(r.status_code,202,r.json);after=deployment(self.store,self.vm)
        self.assertEqual(after['os_family'],'ubuntu');self.assertEqual(after['fqdn'],d['fqdn']);self.assertEqual(after['ip'],d['ip'])
    def test_missing_node_and_unknown_image_refused_before_disk_replacement(self):
        image=self.register(templates={'another-node':9102}).json;d=deployment(self.store,self.vm)
        for key in [image['id'],'foreign-image']:
            r=self.admin.post(self.base+'/reinstall',json={'hostname':d['fqdn'],'confirmation':'REINSTALLER','erase_system_disk':True,'os_image_id':key,'ssh_auth':'password','password':'NewSystemPassword123'},headers=self.headers);self.assertEqual(r.status_code,409,r.json)
        self.assertFalse(deployment(self.store,self.vm).get('reinstall_pending'))
    def test_creation_binds_os_template_and_custom_waits_for_iso_without_keys(self):
        image=self.register().json;model=self.store.rows('SELECT id FROM models')[0]['id']
        for key,family in [(image['id'],'ubuntu'),('custom','custom')]:
            body={'profile_id':self.profile['id'],'model_id':model,'os_image_id':key}
            if family!='custom':body.update(ssh_auth='password',password='NewSystemPassword123')
            run=self.control.request_deploy(body);job=self.store.one('SELECT payload FROM jobs WHERE id=?',(run,));config=json.loads(job['payload'])['config']
            self.assertEqual(config['guest']['os_family'],family)
            self.assertEqual(config['start_vm'],family!='custom')
            if family=='ubuntu':self.assertEqual(config['pve']['template'],9101)

class GuestOS(unittest.TestCase):
    def test_all_os_keep_public_only_addresses_and_permanent_gateway_route(self):
        guest={'username':'debian','gateway':'10.254.251.1','dns':['1.1.1.1'],'interface':'ens18','mtu':1420};access={'username':'user','ssh_auth':'key','public_key':'ssh-ed25519 AAA'}
        for family in ('debian','ubuntu','fedora','almalinux','rocky','freebsd'):
            g={**guest,'os_family':family,'interface':'vtnet0' if family=='freebsd' else 'ens18'}
            network=os_network_data(g,'8.8.8.8','02:00:00:00:00:01');self.assertEqual(network['config'][0]['subnets'][0]['address'],'8.8.8.8/32')
            data=os_user_data(g,'vps.example.net','8.8.8.8','02:00:00:00:00:01',access)
            self.assertEqual(data['fqdn'],'vps.example.net');self.assertNotIn('hashed_passwd',data['users'][0])
            if family=='freebsd':self.assertIn('route_virtualwan_gateway',data['write_files'][0]['content']);self.assertEqual(data['users'][0]['shell'],'/bin/sh')
            elif family in ('fedora','almalinux','rocky'):self.assertIn('wheel',data['users'][0]['groups']);self.assertNotIn('console-setup',data['packages'])
    def test_freebsd_probe_does_not_require_linux_ip_command(self):
        for dual in (False,True):compile(freebsd_probe_code(dual),'probe','exec');self.assertIn('ifconfig',freebsd_probe_code(dual))
    def test_freebsd_probe_normalizes_ifconfig_and_scoped_link_local_gateway(self):
        def command(*args,**kwargs):
            argv=args[0]
            if argv[0]=='ifconfig':return 'lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\nvtnet0: flags=8843\n\tinet 8.8.8.8 netmask 0xffffffff\n\tinet6 fe80::1234%vtnet0 prefixlen 64\n\tinet6 2606:4700:100::120 prefixlen 128\n'
            if argv[0]=='route':return 'gateway: '+('10.254.251.1' if '-inet' in argv else 'fe80::1%vtnet0')+'\n'
            return {'hostname':'vps.example.net\n','curl':'2606:4700:100::120' if '-6' in argv else '8.8.8.8','ssh-keygen':'256 SHA256:synthetic'}[argv[0]]
        stream=io.StringIO()
        with patch('subprocess.check_output',side_effect=command),patch('builtins.open',return_value=io.StringIO('nameserver 1.1.1.1\n')),contextlib.redirect_stdout(stream):exec(freebsd_probe_code(True),{})
        result=json.loads(stream.getvalue());self.assertEqual(result['routes'][0]['gateway'],'10.254.251.1');self.assertEqual(result['ipv6']['routes'][0]['gateway'],'fe80::1');self.assertEqual(result['addresses'][0]['ifname'],'lo')
    def test_cd_volume_is_scoped_to_vmid_and_generated_identifier(self):
        p=MagicMock();p.client={'vmid':120};p.profile={'config':{'pve':{'snippet_storage':'iso'}}}
        volume=identifier(p,'a'*32);self.assertTrue(owned(p,volume));p.client['vmid']=121;self.assertFalse(owned(p,volume))
    def test_live_cd_never_overwrites_scsi1_data_disk(self):
        p=MagicMock();p.api.return_value={'scsi1':'ssd:vm-120-disk-1'}
        with self.assertRaises(domain.Conflict):set_cd(p)
        self.assertFalse(any(call.args[0]=='set' for call in p.api.call_args_list))
