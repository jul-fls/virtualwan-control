import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from virtualwan.web import create_app
from virtualwan.accounts import totp
from virtualwan import domain
from virtualwan.adapters.client_pve import ClientPVE,validate_rules
from virtualwan.environment import apply_environment


class ClientPortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('VWAN_TEST_TMPDIR'))
        self.env=patch.dict(os.environ,{'VWAN_PROFILES_JSON':'[]','VWAN_MODELS_JSON':'[]','VWAN_PUBLIC_URL':'http://localhost:8080'},clear=True);self.env.start()
        self.app=create_app({'TESTING':True,'START_WORKER':False,'DEMO':True,'ADMIN_PASSWORD':'AdminPasswordForTests123','DATA_DIR':self.tmp.name})
        self.store=self.app.extensions['store'];self.accounts=self.app.extensions['accounts'];self.control=self.app.extensions['control']
        self.a=self.accounts.save({'name':'Client A','email':'a@example.net','password':'CustomerPasswordA123'})
        self.b=self.accounts.save({'name':'Client B','email':'b@example.net','password':'CustomerPasswordB123'})
        self.vm=self.control.deployments()[0]['id']
        with self.store.tx() as db:db.execute('INSERT INTO client_vms(deployment_id,account_id) VALUES(?,?)',(self.vm,self.a))
        self.client=self.app.test_client();self.headers={}
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def login(self,address='a@example.net',password='CustomerPasswordA123',**extra):
        r=self.client.post('/api/portal/login',json={'email':address,'password':password,**extra})
        if r.status_code==200:self.headers={'X-CSRF-Token':r.json['csrf']}
        return r
    def test_client_account_never_reaches_admin_api(self):
        self.assertEqual(self.login().status_code,200)
        for method,path in [('get','/api/state'),('get','/api/accounts'),('post','/api/profiles'),('post','/api/purchases')]:
            self.assertEqual(getattr(self.client,method)(path,json={},headers=self.headers).status_code,401)
    def test_ownership_and_csrf_on_all_vm_routes(self):
        self.login('b@example.net','CustomerPasswordB123')
        for method,suffix,payload in [('get','/status',{}),('get','/rescue/credentials',{}),('get','/firewall',{}),('put','',{'display_name':'forbidden'}),('post','/power',{'action':'start'}),('post','/console',{}),('put','/reverse',{'hostname':'example.net'}),('put','/firewall',{'rules':[],'confirmation':'APPLIQUER'}),('post','/rescue',{'confirmation':'CONFIRMER'}),('post','/graphical',{})]:
            with self.subTest(suffix=suffix):self.assertEqual(getattr(self.client,method)('/api/portal/vps/'+self.vm+suffix,json=payload,headers=self.headers).status_code,404)
        self.login();self.assertEqual(self.client.post('/api/portal/vps/'+self.vm+'/power',json={'action':'start'}).status_code,403)
    def test_client_state_is_sanitized(self):
        self.login();r=self.client.get('/api/portal/state');self.assertEqual(len(r.json['vps']),1)
        for name in ('next_hop','profile_id','config','pve','mac','node','password_hash','recovery','totp','secret_names'):self.assertNotIn('"'+name+'"',r.get_data(as_text=True))
        from unittest.mock import MagicMock
        failed=MagicMock();failed.__enter__.side_effect=ValueError('private-management-host.example.invalid')
        self.app.config['CLIENT_PVE_FACTORY']=lambda *args:failed;self.control.demo=False
        error=self.client.get('/api/portal/vps/'+self.vm+'/status')
        self.assertEqual(error.status_code,409);self.assertNotIn('private-management-host',error.get_data(as_text=True))
        self.assertTrue(self.store.rows("SELECT id FROM events WHERE action='client.infrastructure.failed'"))

    def test_password_change_revokes_all_sessions(self):
        self.login();other=self.accounts.login('a@example.net','CustomerPasswordA123',None)
        r=self.client.put('/api/portal/password',json={'current_password':'CustomerPasswordA123','password':'AChangedPassword123'},headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertIsNone(self.accounts.authenticate(other['token']))
        self.assertEqual(self.client.get('/api/portal/state').status_code,401)
        self.assertEqual(self.login(password='AChangedPassword123').status_code,200)
    def test_suspend_revokes_access(self):
        self.login();self.accounts.save({'name':'A','email':'a@example.net','enabled':False},self.a)
        self.assertEqual(self.client.get('/api/portal/state').status_code,401)
        self.assertEqual(self.login().status_code,401)
    def test_totp_rfc_vector_and_replay_recovery(self):
        self.assertEqual(totp('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',1),'287082')
        self.login();r=self.client.post('/api/portal/mfa/begin',json={'current_password':'CustomerPasswordA123'},headers=self.headers)
        self.assertEqual(r.status_code,200,r.json);secret=r.json['secret'];challenge=r.json['challenge'];step=int(time.time()//30)
        r=self.client.post('/api/portal/mfa/complete',json={'challenge':challenge,'code':totp(secret,step)},headers=self.headers)
        self.assertEqual(r.status_code,200,r.json);codes=r.json['recovery_codes'];self.assertEqual(len(codes),10)
        self.assertEqual(self.login().status_code,401)
        self.assertEqual(self.login(code='éééééé').status_code,401)
        self.assertEqual(self.login(code=totp(secret,step)).status_code,401)
        self.assertEqual(self.login(code=codes[0]).status_code,200)
        self.assertEqual(self.login(code=codes[0]).status_code,401)
        self.assertNotIn(secret,self.store.path.read_bytes().decode(errors='ignore'))
    def test_reauth_required_for_security_changes(self):
        self.login()
        self.assertEqual(self.client.put('/api/portal/account',json={'name':'A','email':'new@example.net'},headers=self.headers).status_code,409)
        self.assertEqual(self.client.post('/api/portal/passkey/register/begin',json={},headers=self.headers).status_code,409)
    def test_challenges_one_use_expiring_and_account_bound(self):
        challenge=self.accounts.challenge(self.a,'console',{'test':True})
        with self.assertRaises(domain.Conflict):self.accounts.consume(challenge,'console',self.b)
        self.accounts.consume(challenge,'console',self.a)
        with self.assertRaises(domain.Conflict):self.accounts.consume(challenge,'console',self.a)
        challenge=self.accounts.challenge(self.a,'console',{},-1)
        with self.assertRaises(domain.Conflict):self.accounts.consume(challenge,'console',self.a)
    def test_demo_power_and_destructive_confirmation(self):
        self.login();path='/api/portal/vps/'+self.vm+'/power'
        self.assertEqual(self.client.post(path,json={'action':'reset'},headers=self.headers).status_code,400)
        for action in ('start','shutdown','reboot','stop','reset'):
            self.assertEqual(self.client.post(path,json={'action':action,'confirmation':'CONFIRMER'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.post(path,json={'action':'delete'},headers=self.headers).status_code,400)
    def test_metadata_is_free_text_without_changing_proxmox_identity(self):
        self.login();r=self.client.put('/api/portal/vps/'+self.vm,json={'display_name':'Mon serveur','description':'<script>alert(1)</script>'},headers=self.headers)
        self.assertEqual(r.status_code,200)
        r=self.client.get('/api/portal/state');self.assertEqual(r.json['vps'][0]['display_name'],'Mon serveur')
        self.assertNotEqual(self.control.deployments()[0]['fqdn'],'Mon serveur')
    def test_public_console_challenge_not_in_url_or_logs(self):
        self.login();r=self.client.post('/api/portal/vps/'+self.vm+'/console',json={},headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertTrue(r.json['password']);self.assertTrue(r.json['ticket'])
        events=json.dumps(self.store.rows('SELECT data FROM events'))
        self.assertNotIn(r.json['password'],events);self.assertNotIn(r.json['ticket'],events)
    def test_reverse_validation_no_foreign_vm_or_shell_syntax(self):
        self.login()
        for value in ('bad','x;ls.example.net','x/evil.net','127.0.0.1','a..example.net'):
            r=self.client.put('/api/portal/vps/'+self.vm+'/reverse',json={'hostname':value},headers=self.headers)
            # Dotted IPv4 is also rejected: PTR must be a hostname.
            self.assertEqual(r.status_code,400)
        self.assertEqual(self.client.put('/api/portal/vps/'+self.vm+'/reverse',json={'hostname':'customer.example.net'},headers=self.headers).status_code,200)
    def test_throttle_blocks_password_bruteforce(self):
        for _ in range(8):self.assertEqual(self.login(password='wrong').status_code,401)
        self.assertEqual(self.login().status_code,429)
    def test_admin_assignment_revokes_previous_ownership(self):
        admin=self.app.test_client();csrf=admin.post('/api/login',json={'username':'admin','password':'AdminPasswordForTests123'}).json['csrf']
        self.login();r=admin.put('/api/deployments/'+self.vm+'/account',json={'account_id':self.b},headers={'X-CSRF-Token':csrf})
        self.assertEqual(r.status_code,200);self.assertEqual(self.client.get('/api/portal/vps/'+self.vm+'/status').status_code,404)
    def test_power_task_is_submitted_then_failure_is_recorded_without_infra_details(self):
        from unittest.mock import MagicMock
        self.login();self.control.demo=False;self.control.allow_provision=True
        backend=MagicMock();backend.__enter__.return_value=backend;backend.task.return_value={'task':'UPID:pve2:00000001:00000001:00000001:qmstart:120:root@pam:','node':'pve2'}
        backend.task_status.return_value={'status':'stopped','exitstatus':'Error: start refused'}
        self.app.config['CLIENT_PVE_FACTORY']=lambda *a:backend
        r=self.client.post('/api/portal/vps/'+self.vm+'/power',json={'action':'start'},headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertIs(r.json['task'],True);self.assertNotIn('pve2',r.get_data(as_text=True))
        self.assertEqual(self.store.one('SELECT state FROM client_operations')['state'],'submitted')
        r=self.client.get('/api/portal/operations/'+r.json['operation_id'])
        self.assertEqual(r.json['state'],'failed');self.assertNotIn('pve2',r.get_data(as_text=True))
        self.assertNotIn('UPID',self.client.get('/api/portal/state').get_data(as_text=True))
    def test_legacy_database_migration_has_valid_client_vm_foreign_key(self):
        import sqlite3
        from virtualwan.store import Store
        from virtualwan.accounts import Accounts
        with tempfile.TemporaryDirectory(dir=os.environ.get('VWAN_TEST_TMPDIR')) as directory:
            with sqlite3.connect(str(Path(directory)/'control.sqlite3')) as db:
                db.execute('CREATE TABLE deployments(id TEXT PRIMARY KEY,ip TEXT NOT NULL UNIQUE,profile_id TEXT NOT NULL,model_id TEXT,data TEXT NOT NULL)')
                db.execute('INSERT INTO deployments VALUES(?,?,?,?,?)',('old','8.8.8.8','profile',None,'{}'))
            db.close()
            store=Store(directory);account=Accounts(store,'http://localhost').save({'name':'Client','email':'migration@example.net','password':'MigrationPassword123'})
            with store.tx() as db:
                self.assertEqual({r['table'] for r in db.execute('PRAGMA foreign_key_list(client_vms)')},{'accounts','deployments'})
                db.execute('INSERT INTO client_vms(deployment_id,account_id) VALUES(?,?)',('old',account))
                db.execute("UPDATE deployments SET ip=NULL WHERE id='old'")
                db.execute("DELETE FROM deployments WHERE id='old'")
                self.assertIsNone(db.execute('SELECT * FROM client_vms').fetchone())
    def test_graphical_waits_for_shutdown_before_config_and_starts_after(self):
        from unittest.mock import MagicMock,call
        pve=ClientPVE(self.store,{},{});pve.config={'serial0':'socket','vga':'serial0','digest':'digest'}
        order=MagicMock()
        with patch.object(pve,'status',side_effect=[{'status':'running'},{'status':'stopped'}]),patch.object(pve,'check_identity'),patch.object(pve,'api',side_effect=['UPID:shutdown',None,{'vga':'std'},'UPID:start']) as api,patch.object(pve,'wait_task') as task,patch.object(pve,'wait_power') as power:
            order.attach_mock(api,'api');order.attach_mock(task,'task');order.attach_mock(power,'power')
            result=pve.graphical()
        self.assertTrue(result['restarted'])
        names=[c[0] for c in order.mock_calls]
        self.assertEqual(names,['api','task','power','api','api','api','task','power'])
        api.assert_any_call('create','/status/shutdown',timeout=120,forceStop=0)
        api.assert_any_call('set','/config',vga='std',digest='digest',delete='serial0')
    def test_graphical_shutdown_timeout_never_changes_config_or_force_stops(self):
        from virtualwan.adapters.client_pve import GraphicalPreparationError
        pve=ClientPVE(self.store,{},{});pve.config={'digest':'digest'}
        with patch.object(pve,'status',return_value={'status':'running'}),patch.object(pve,'api',return_value='UPID:shutdown') as api,patch.object(pve,'wait_task',side_effect=GraphicalPreparationError('Arrêt non confirmé')):
            with self.assertRaisesRegex(GraphicalPreparationError,'Arrêt non confirmé'):pve.graphical()
        api.assert_called_once_with('create','/status/shutdown',timeout=120,forceStop=0)
    def test_graphical_configuration_failure_restores_running_guest(self):
        from virtualwan.adapters.client_pve import GraphicalPreparationError
        pve=ClientPVE(self.store,{},{});pve.config={'digest':'digest'}
        with patch.object(pve,'status',side_effect=[{'status':'running'},{'status':'stopped'},{'status':'stopped'}]),patch.object(pve,'check_identity'),patch.object(pve,'api',side_effect=['UPID:shutdown',ValueError('private detail'),'UPID:start']) as api,patch.object(pve,'wait_task'),patch.object(pve,'wait_power'):
            with self.assertRaisesRegex(GraphicalPreparationError,'remis en marche'):pve.graphical()
        self.assertEqual(api.call_args.args,('create','/status/start'))
    def test_graphical_background_job_deduplicates_and_blocks_parallel_power(self):
        self.login();pending=[];self.app.config['CLIENT_TASK_RUNNER']=pending.append
        path='/api/portal/vps/'+self.vm+'/graphical'
        first=self.client.post(path,json={},headers=self.headers);self.assertEqual(first.status_code,202)
        again=self.client.post(path,json={},headers=self.headers);self.assertEqual(first.json,again.json);self.assertEqual(len(pending),1)
        self.assertEqual(self.client.post('/api/portal/vps/'+self.vm+'/power',json={'action':'start'},headers=self.headers).status_code,409)
        pending[0]()
        status=self.client.get('/api/portal/operations/'+first.json['operation_id'])
        self.assertEqual(status.json['state'],'succeeded');self.assertEqual(status.json['result']['phase'],'ready')
    def test_graphical_background_failure_has_actionable_safe_error(self):
        from unittest.mock import MagicMock
        from virtualwan.adapters.client_pve import GraphicalPreparationError
        self.login();self.control.demo=False;self.control.allow_provision=True
        backend=MagicMock();backend.__enter__.return_value=backend
        backend.graphical.side_effect=GraphicalPreparationError('Le VPS ne s’est pas arrêté proprement. Réessayez après son arrêt.')
        self.app.config.update(CLIENT_PVE_FACTORY=lambda *a:backend,CLIENT_TASK_RUNNER=lambda f:f())
        response=self.client.post('/api/portal/vps/'+self.vm+'/graphical',json={},headers=self.headers)
        self.assertEqual(response.status_code,202)
        result=self.client.get('/api/portal/operations/'+response.json['operation_id']).json
        self.assertEqual(result['state'],'failed');self.assertIn('arrêté proprement',result['result']['error'])
        self.assertNotIn('infrastructure',result['result']['error'])
    def test_rescue_password_encrypted_available_only_to_owner_while_active(self):
        self.login();self.app.config['CLIENT_TASK_RUNNER']=lambda f:f();path='/api/portal/vps/'+self.vm+'/rescue'
        first=self.client.post(path,json={'confirmation':'CONFIRMER'},headers=self.headers)
        self.assertEqual(first.status_code,202);credentials=self.client.get(path+'/credentials');password=credentials.json['password'];self.assertEqual(len(password),12);self.assertRegex(password,r'^[A-Za-z0-9]{12}$');self.assertTrue(any(c.isupper() for c in password) and any(c.islower() for c in password) and any(c.isdigit() for c in password));self.assertFalse(set(password)&set('0O1Il'))
        self.assertEqual(credentials.headers['Cache-Control'],'no-store');self.assertEqual(credentials.json['username'],'rescue')
        self.assertEqual(self.client.post(path,json={'confirmation':'CONFIRMER'},headers=self.headers).status_code,409)
        self.assertNotIn(password,self.client.get('/api/portal/state').get_data(as_text=True))
        self.assertNotIn(password,json.dumps(self.store.rows('SELECT * FROM client_operations')))
        self.assertNotIn(password,json.dumps(self.store.rows('SELECT * FROM deployments')))
        self.assertNotIn(password,json.dumps(self.store.rows('SELECT * FROM events')))
        credentials=self.client.get(path+'/credentials');self.assertEqual(credentials.json['password'],password)
        self.assertEqual(credentials.headers['Cache-Control'],'no-store')
        row=self.store.one('SELECT ciphertext FROM rescue_credentials WHERE deployment_id=?',(self.vm,))
        self.assertNotIn(password.encode(),row['ciphertext']);self.assertEqual(self.store.cipher.decrypt(row['ciphertext']).decode(),password)
        self.login('b@example.net','CustomerPasswordB123')
        self.assertEqual(self.client.get(path+'/credentials').status_code,404)
        self.login()
        # Re-opening the database preserves the encrypted active password.
        from virtualwan.store import Store
        reopened=Store(self.tmp.name)
        row=reopened.one('SELECT ciphertext FROM rescue_credentials WHERE deployment_id=?',(self.vm,))
        self.assertEqual(reopened.cipher.decrypt(row['ciphertext']).decode(),password)
        off=self.client.post(path,json={'confirmation':'CONFIRMER','disable':True},headers=self.headers)
        self.assertEqual(self.client.get(path+'/credentials').status_code,409)
        self.assertEqual(self.store.rows('SELECT * FROM rescue_credentials'),[])
        self.assertNotIn('password',off.json)
        self.assertTrue(self.client.get('/api/portal/operations/'+off.json['operation_id']).json['result']['disable'])
        second=self.client.post(path,json={'confirmation':'CONFIRMER'},headers=self.headers)
        second_password=self.client.get(path+'/credentials').json['password'];self.assertNotEqual(second_password,password)
        # A failed return to disk must not lose the still-active credentials.
        from unittest.mock import MagicMock
        backend=MagicMock();backend.__enter__.return_value=backend;backend.rescue.side_effect=domain.Conflict('VM encore en marche.')
        self.app.config['CLIENT_PVE_FACTORY']=lambda *a:backend;self.control.demo=False;self.control.allow_provision=True
        failed=self.client.post(path,json={'confirmation':'CONFIRMER','disable':True},headers=self.headers);self.assertEqual(failed.status_code,202);self.assertEqual(self.client.get('/api/portal/operations/'+failed.json['operation_id']).json['state'],'failed')
        self.assertEqual(self.client.get(path+'/credentials').json['password'],second_password)
        with self.store.tx() as db:db.execute('DELETE FROM deployments WHERE id=?',(self.vm,))
        self.assertEqual(self.store.rows('SELECT * FROM rescue_credentials'),[])
    def test_rescue_background_deduplicates_blocks_power_and_survives_start_failure(self):
        from unittest.mock import MagicMock
        self.login();pending=[];self.app.config['CLIENT_TASK_RUNNER']=pending.append
        path='/api/portal/vps/'+self.vm+'/rescue'
        first=self.client.post(path,json={'confirmation':'CONFIRMER'},headers=self.headers)
        again=self.client.post(path,json={'confirmation':'CONFIRMER'},headers=self.headers)
        self.assertEqual(first.status_code,202);self.assertEqual(first.json,again.json);self.assertEqual(len(pending),1)
        self.assertEqual(self.client.post('/api/portal/vps/'+self.vm+'/power',json={'action':'start'},headers=self.headers).status_code,409)
        self.assertEqual(self.client.get('/api/portal/state').json['vps'][0]['rescue_operation']['id'],first.json['operation_id'])
        self.control.demo=False;self.control.allow_provision=True
        backend=MagicMock();backend.__enter__.return_value=backend;self.app.config['CLIENT_PVE_FACTORY']=lambda *a:backend
        plan={'boot':'order=scsi0','ide0':'iso:iso/rescue.iso,media=cdrom','credential_volid':'auth'}
        def fail(*a,**kw):
            kw['checkpoint'](plan,False);kw['checkpoint'](plan,True);kw['progress']('starting','Démarrage du VPS…')
            raise domain.Conflict('Démarrage non confirmé, réessayez.')
        backend.rescue.side_effect=fail;pending.pop()()
        status=self.client.get('/api/portal/operations/'+first.json['operation_id']).json
        self.assertEqual(status['state'],'failed');self.assertNotIn('infrastructure',status['result']['error'])
        state=self.client.get('/api/portal/state').json['vps'][0];self.assertTrue(state['rescue_state']);self.assertEqual(state['rescue_transition'],{'disable':False})
        password=self.client.get(path+'/credentials').json['password'];self.assertEqual(len(password),12)
        def success(*a,**kw):
            self.assertEqual(a[2],plan);self.assertEqual(kw['password'],password);kw['checkpoint'](plan,True);return plan
        backend.rescue.side_effect=success
        retry=self.client.post(path,json={'confirmation':'CONFIRMER'},headers=self.headers);pending.pop()()
        self.assertEqual(self.client.get('/api/portal/operations/'+retry.json['operation_id']).json['state'],'succeeded')
        state=self.client.get('/api/portal/state').json['vps'][0];self.assertIsNone(state['rescue_transition']);self.assertTrue(state['rescue_state'])
        self.assertEqual(self.client.get(path+'/credentials').json['password'],password)

    def test_scalar_bootstrap_and_csv_without_json(self):
        os.environ.update(VWAN_ROUTER_BOOTSTRAP='true',VWAN_PROFILE_ID='scalar',VWAN_ROUTER_NAME='Scalar router',VWAN_ROUTER_PRIMARY_IP='8.8.8.8',VWAN_PFSENSE_NEXT_HOP_NETWORK='10.254.248.0/24',VWAN_GUEST_GATEWAY='10.254.248.1',VWAN_PFSENSE_PUBLIC_INTERFACE='opt_scalar',VWAN_GUEST_DNS='1.1.1.1,9.9.9.9',VWAN_MODEL_BOOTSTRAP='true')
        apply_environment(self.control);self.assertEqual(self.store.document('profiles','scalar')['config']['guest']['dns'],['1.1.1.1','9.9.9.9'])
        p=self.store.document('profiles','scalar');p['name']='Changed in UI';self.control.add_profile(p,'scalar');apply_environment(self.control)
        self.assertEqual(self.store.document('profiles','scalar')['name'],'Changed in UI')
    def test_json_import_preserves_ui_edits(self):
        p=self.store.document('profiles',self.control.deployments()[0]['profile_id']);os.environ['VWAN_PROFILES_JSON']=json.dumps([p])
        apply_environment(self.control);p['name']='UI owned';self.control.add_profile(p,p['id']);apply_environment(self.control)
        self.assertEqual(self.store.document('profiles',p['id'])['name'],'UI owned')
    def test_firewall_validation_limits_protocol_source_ports(self):
        self.assertEqual(validate_rules([{'action':'DROP','proto':'tcp','dport':'22','source':'8.8.8.8/32'}])[0]['type'],'in')
        for rule in ({'action':'ACCEPT','proto':'tcp','dport':'1; reboot'},{'action':'DROP','proto':'all'},{'action':'DROP','proto':'tcp','source':'bad'},{'action':'DROP','proto':'tcp','dport':'70000'}):
            with self.assertRaises(ValueError):validate_rules([rule])
        with self.assertRaises(ValueError):validate_rules([{}]*51)
    def test_live_mode_disabled_refuses_mutations(self):
        self.login();self.control.demo=False;self.control.allow_provision=False
        self.assertEqual(self.client.post('/api/portal/vps/'+self.vm+'/power',json={'action':'start'},headers=self.headers).status_code,409)
    def test_proxmox_api_uses_scoped_paths_and_accepts_real_task_ids(self):
        pve=ClientPVE(self.store,{}, {'id':self.vm});pve.base='/nodes/pve2/qemu/120';pve.reg={'nodes':{'pve2':{},'pve3':{}}}
        with patch.object(pve,'command',return_value='{}') as command:
            pve.api('get','/config');self.assertEqual(command.call_args.args[0][2],'/nodes/pve2/qemu/120/config')
            pve.api('create','/status/start');self.assertEqual(command.call_args.args[0][2],'/nodes/pve2/qemu/120/status/start')
            pve.api('get','/cluster/resources',type='vm');self.assertEqual(command.call_args.args[0][2],'/cluster/resources')
            upid='UPID:pve3:00000001:00000001:00000001:qmstart:120:root@pam:'
            pve.task_status(upid);self.assertEqual(command.call_args.args[0][2],'/nodes/pve3/tasks/'+upid+'/status')
        with self.assertRaises(ValueError):pve.task_status('UPID:bad;reboot')
    def test_backend_firewall_never_enables_cluster_or_node(self):
        pve=ClientPVE(self.store,{},{});pve.config={'net0':'virtio=02:00:00:00:00:01,bridge=vmbr12','digest':'digest'}
        with patch.object(pve,'firewall',return_value={'cluster_enabled':False,'nic_enabled':False}),patch.object(pve,'api') as api:
            with self.assertRaises(domain.Conflict):pve.set_firewall({'rules':[],'enabled':True,'policy_in':'DROP'})
            api.assert_not_called()
    def test_firewall_preserves_admin_priority_and_refreshes_digests(self):
        pve=ClientPVE(self.store,{}, {'id':self.vm});pve.config={'net0':'virtio=x,firewall=1','digest':'vm-digest'}
        items=[{'comment':'ADMIN','type':'in','action':'DROP'}, {'comment':'VWAN-CLIENT old1','type':'in','action':'ACCEPT'},{'comment':'VWAN-CLIENT old2','type':'in','action':'DROP'}];revision=0
        def api(method,path,**params):
            nonlocal revision
            if path=='/firewall/rules' and method=='get':return [{**r,'pos':i,'digest':str(revision)} for i,r in enumerate(items)]
            if path.startswith('/firewall/rules/'):
                self.assertEqual(params['digest'],str(revision));position=int(path.rsplit('/',1)[-1])
                if method=='delete':items.pop(position)
                elif 'moveto' in params:items.insert(params['moveto'],items.pop(position))
                else:items[position].update({k:v for k,v in params.items() if k!='digest'})
                revision+=1
            elif method=='create':items.insert(0,params);revision+=1
        with patch.object(pve,'api',side_effect=api),patch.object(pve,'firewall',return_value={'cluster_enabled':True,'nic_enabled':True}):
            pve.set_firewall({'enabled':True,'policy_in':'ACCEPT','rules':[{'action':'DROP','proto':'tcp','dport':'22','label':'SSH'},{'action':'ACCEPT','proto':'tcp','dport':'80','label':'HTTP'}]})
        self.assertEqual(items[0]['comment'],'ADMIN')
        self.assertTrue(items[1]['comment'].endswith(': SSH'));self.assertTrue(items[2]['comment'].endswith(': HTTP'))
        self.assertTrue(all(r.get('enable')==1 for r in items[1:]))
    def test_rescue_requires_credentials_and_allowlisted_iso_before_stopping(self):
        pve=ClientPVE(self.store,{},{});pve.config={}
        with patch.object(pve,'status',return_value={'status':'running'}),patch.object(pve,'api') as api:
            with self.assertRaises(domain.Conflict):pve.rescue('iso:iso/rescue.iso')
            api.assert_not_called()
        with patch.object(pve,'status',return_value={'status':'stopped'}),patch.object(pve,'api') as api:
            with self.assertRaises(domain.Conflict):pve.rescue('https://evil.example/anything.iso')
            api.assert_not_called()


if __name__=='__main__':unittest.main()
