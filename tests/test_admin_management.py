import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from virtualwan.web import create_app
from virtualwan.management import deployment,notes
from virtualwan.adapters.client_pve import ClientPVE
from virtualwan import domain

class AdminManagement(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('VWAN_TEST_TMPDIR'));self.env=patch.dict(os.environ,{'VWAN_PUBLIC_URL':'http://localhost:8080'},clear=True);self.env.start()
        self.app=create_app({'TESTING':True,'START_WORKER':False,'DEMO':True,'ADMIN_PASSWORD':'AdminPasswordForTests123','DATA_DIR':self.tmp.name,'CLIENT_TASK_RUNNER':lambda f:f()})
        self.store=self.app.extensions['store'];self.control=self.app.extensions['control'];self.accounts=self.app.extensions['accounts']
        self.account=self.accounts.save({'name':'Client <test>','email':'admin-vm@example.net','password':'CustomerPassword123'})
        self.vm=self.control.deployments()[0]['id'];self.base='/api/admin/vps/'+self.vm
        with self.store.tx() as db:db.execute('INSERT INTO client_vms(deployment_id,account_id) VALUES(?,?)',(self.vm,self.account))
        self.admin=self.app.test_client();r=self.admin.post('/api/login',json={'username':'admin','password':'AdminPasswordForTests123'});self.headers={'X-CSRF-Token':r.json['csrf']}
        self.client=self.app.test_client();r=self.client.post('/api/portal/login',json={'email':'admin-vm@example.net','password':'CustomerPassword123'});self.client_headers={'X-CSRF-Token':r.json['csrf']}
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def test_admin_and_client_sessions_are_not_interchangeable(self):
        self.assertEqual(self.client.get('/api/admin/state').status_code,401)
        self.assertEqual(self.admin.get('/api/portal/state').status_code,401)
        self.assertEqual(self.admin.post(self.base+'/power',json={'action':'start'}).status_code,403)
        state=self.admin.get('/api/admin/state');self.assertEqual(state.status_code,200);self.assertTrue(state.json['admin'])
        with self.store.tx() as db:db.execute('DELETE FROM client_vms WHERE deployment_id=?',(self.vm,))
        self.assertEqual(self.admin.get(self.base+'/status').status_code,200)
        self.assertEqual(self.admin.post(self.base+'/power',json={'action':'reboot'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/api/portal/vps/'+self.vm+'/status').status_code,404)
    def test_lock_denies_all_client_vm_apis_but_keeps_admin_and_account(self):
        path='/api/portal/vps/'+self.vm
        ticket=self.client.post(path+'/console',json={},headers=self.client_headers).json['ticket']
        r=self.admin.put(self.base+'/lock',json={'locked':True,'reason':'Incident interne','confirmation':'CONFIRMER'},headers=self.headers);self.assertEqual(r.status_code,200,r.json)
        for method,suffix,b in [('get','/status',{}),('get','/firewall',{}),('put','',{'display_name':'denied'}),('post','/power',{'action':'start'}),('post','/console',{}),('get','/rescue/credentials',{}),('post','/graphical',{}),('post','/rescue',{'confirmation':'CONFIRMER'}),('put','/reverse',{'hostname':'test.example.net'}),('put','/firewall',{'rules':[],'confirmation':'APPLIQUER'})]:
            self.assertEqual(getattr(self.client,method)(path+suffix,json=b,headers=self.client_headers).status_code,409,suffix)
        self.assertEqual(self.store.rows('SELECT id FROM client_challenges WHERE id=?',(ticket,)),[])
        self.assertTrue(self.store.rows("SELECT id FROM client_operations WHERE deployment_id=? AND action='admin.lock' AND state='succeeded'",(self.vm,)))
        state=self.client.get('/api/portal/state');self.assertEqual(state.status_code,200);self.assertTrue(state.json['vps'][0]['management_locked']);self.assertNotIn('Incident interne',state.text)
        with self.store.tx() as db:
            row=db.execute('SELECT * FROM deployments WHERE id=?',(self.vm,)).fetchone();other=json.loads(row['data']);other['management_locked']=False
            db.execute('INSERT INTO deployments VALUES(?,?,?,?,?)',('other-vps','192.0.2.222',row['profile_id'],row['model_id'],json.dumps(other)))
            db.execute('INSERT INTO client_vms(deployment_id,account_id) VALUES(?,?)',('other-vps',self.account))
        self.assertEqual(self.client.get('/api/portal/vps/other-vps/status').status_code,200)
        self.assertEqual(self.admin.get(self.base+'/status').status_code,200)
        self.assertEqual(self.admin.post(self.base+'/console',json={},headers=self.headers).status_code,200)
        r=self.admin.put(self.base+'/lock',json={'locked':False,'confirmation':'CONFIRMER'},headers=self.headers);self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.get(path+'/status').status_code,200)
    def test_admin_resize_network_onboot_and_notes(self):
        model=self.control.add_model({'name':'Small','range':'Test','cores':1,'memory_mb':1024,'disk_gb':1,'template':9001})
        r=self.admin.post(self.base+'/resize',json={'model_id':model,'confirmation':'CONFIRMER'},headers=self.headers);self.assertEqual(r.status_code,202,r.json)
        job=self.admin.get('/api/admin/operations/'+r.json['operation_id']).json;self.assertEqual(job['state'],'succeeded',job)
        d=deployment(self.store,self.vm);self.assertEqual(d['model_id'],model);self.assertEqual(d['cores'],1);self.assertGreaterEqual(d['disk_gb'],1)
        self.assertIn('Client &lt;test&gt;',d['pve_notes']);self.assertIn('admin-vm@example.net',d['pve_notes'])
        for suffix,body in [('network',{'blocked':True,'confirmation':'CONFIRMER'}),('onboot',{'enabled':True}),('notes',{})]:
            r=self.admin.post(self.base+'/'+suffix,json=body,headers=self.headers);self.assertEqual(r.status_code,202,r.json)
            self.assertEqual(self.admin.get('/api/admin/operations/'+r.json['operation_id']).json['state'],'succeeded')
        d=deployment(self.store,self.vm);self.assertTrue(d['network_suspended']);self.assertTrue(d['onboot'])
    def test_notes_preserve_marker_manual_notes_and_update_idempotently(self):
        d=deployment(self.store,self.vm);marker='Virtualwan managed: '+d['fqdn'];original=marker+'\nPersonal note'
        first=notes(self.store,d,original);second=notes(self.store,{**d,'account_name':'Updated'},first)
        self.assertEqual(second.splitlines()[0],marker);self.assertIn('Personal note',second);self.assertEqual(second.count('virtualwan:begin'),1);self.assertIn('Updated',second);self.assertNotIn('Client &lt;test&gt;',second)
    def test_admin_resize_retry_keeps_confirmed_model_after_failed_start(self):
        self.control.demo=False;self.control.allow_provision=True
        model=self.control.add_model({'name':'Large','range':'Test','cores':4,'memory_mb':4096,'disk_gb':64,'template':9001})
        backend=MagicMock();backend.__enter__.return_value=backend;self.app.config['CLIENT_PVE_FACTORY']=lambda *a:backend
        def fail(target,pending,progress,checkpoint):
            checkpoint({'model':target,'was_running':True,'cores':4,'memory_mb':4096,'disk_gb':64},True)
            raise domain.Conflict('Démarrage non confirmé')
        backend.resize.side_effect=fail
        r=self.admin.post(self.base+'/resize',json={'model_id':model,'confirmation':'CONFIRMER'},headers=self.headers)
        self.assertEqual(self.admin.get('/api/admin/operations/'+r.json['operation_id']).json['state'],'failed')
        d=deployment(self.store,self.vm);self.assertEqual(d['model_id'],model);self.assertEqual(d['resize_pending']['model_id'],model)

class HardwareResize(unittest.TestCase):
    def setup_pve(self):
        store=MagicMock();p=ClientPVE(store,{}, {'id':'vm','vmid':120});p.node='pve2';p.base='/nodes/pve2/qemu/120'
        cfg={'boot':'order=scsi0','scsi0':'raid1-ssd:vm-120-disk-0,size=64G','memory':4096,'cores':4,'digest':'x','net0':'virtio=02:00:00:00:00:01,bridge=vmbr12,firewall=1,tag=150'};p.config=cfg.copy();calls=[]
        def api(method,path,**values):
            calls.append((method,path,values))
            if path=='/config':
                if method=='get':return cfg.copy()
                values.pop('digest',None);cfg.update(values)
            elif path.endswith('/status'):return {'avail':1024**4}
            elif path.startswith('/status/'):return 'UPID:task'
        p.api=MagicMock(side_effect=api);p.check_identity=MagicMock(side_effect=lambda:setattr(p,'config',cfg.copy()));p.wait_task=MagicMock();p.wait_power=MagicMock();p.status=MagicMock(side_effect=[{'status':'running'},{'status':'stopped'}]);return p,cfg,calls
    def test_downgrade_never_shrinks_disk_and_restarts_running_vm(self):
        p,cfg,calls=self.setup_pve();p.command=MagicMock();check=[]
        result=p.resize({'name':'Small','cores':1,'memory_mb':1024,'disk_gb':16},checkpoint=lambda data,done:check.append(done))
        self.assertEqual(result['disk_gb'],64);self.assertEqual(cfg['cores'],1);self.assertEqual(cfg['memory'],1024);p.command.assert_not_called();self.assertEqual(check,[False,True]);self.assertEqual(calls[-1][1],'/status/start')
    def test_upgrade_grows_virtual_disk_with_verified_size(self):
        p,cfg,calls=self.setup_pve()
        # Assert exact validated command independently of shell quoting.
        def grow(args):
            self.assertIn('--disk',args);self.assertIn('96G',args);cfg['scsi0']='raid1-ssd:vm-120-disk-0,size=96G';return 'disk resized'
        p.command=MagicMock(side_effect=grow)
        result=p.resize({'name':'Large','cores':8,'memory_mb':8192,'disk_gb':96});self.assertEqual(result['disk_gb'],96);p.command.assert_called_once()
    def test_network_suspension_preserves_bridge_mac_firewall_and_vlan(self):
        p,cfg,calls=self.setup_pve();original=cfg['net0'];p.set_network(True);self.assertEqual(cfg['net0'],original+',link_down=1');p.set_network(False);self.assertEqual(cfg['net0'],original)
