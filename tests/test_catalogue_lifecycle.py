import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch,Mock
import xml.etree.ElementTree as ET
from test_control import Tests as Fixture,KEY
from virtualwan import domain
from virtualwan.catalogue import enrich,is_ip_offer
from virtualwan.hostnames import stock_hostname
from virtualwan.providers import DemoOvh
from virtualwan.adapters import remote
from virtualwan.adapters.pfsense import PfSense


class LifecycleTests(unittest.TestCase):
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    deploy=Fixture.deploy
    def created(self):
        self.control.add_ip(self.profile,'8.8.8.8');self.deploy();self.control.process_one()
        return self.control.deployments()[0]
    def test_reverse_name(self):self.assertEqual(stock_hostname('127.0.1.2','hosting.example.net'),'vps-127012.hosting.example.net')
    def test_reverse_rejects_domain_injection(self):
        with self.assertRaises(ValueError):stock_hostname('8.8.8.8','host;evil')
    def test_retire_requires_confirmation(self):
        client=self.created()
        with self.assertRaises(ValueError):self.control.request_retire(client['id'],{'confirmation':'yes'})
        self.assertEqual(self.control.deployments()[0]['status'],'simulated')
    def test_retire_releases_only_after_success(self):
        client=self.created();job=self.control.request_retire(client['id'],{'confirmation':'SUPPRIMER'})
        self.assertEqual(self.store.one('SELECT state FROM ip_pool WHERE ip=?',('8.8.8.8',))['state'],'reserved')
        with patch('subprocess.Popen',side_effect=AssertionError('network')):self.control.process_one()
        self.assertFalse(self.control.deployments());self.assertEqual(self.store.one('SELECT state FROM ip_pool WHERE ip=?',('8.8.8.8',))['state'],'available')
        result=json.loads(self.store.one('SELECT result FROM jobs WHERE id=?',(job,))['result'])
        self.assertEqual(result['fqdn'],'vps-8888.hosting.example.net')
    def test_retire_failure_keeps_record_and_quarantines(self):
        client=self.created();self.control.request_retire(client['id'],{'confirmation':'SUPPRIMER'})
        with patch.object(self.control,'run_lifecycle',side_effect=ValueError('Partial cleanup')):self.control.process_one()
        self.assertEqual(len(self.control.deployments()),1)
        self.assertEqual(self.store.one('SELECT state FROM ip_pool WHERE ip=?',('8.8.8.8',))['state'],'quarantined')
    def test_duplicate_retirement_refused(self):
        c=self.created();self.control.request_retire(c['id'],{'confirmation':'SUPPRIMER'})
        with self.assertRaises(domain.Conflict):self.control.request_retire(c['id'],{'confirmation':'SUPPRIMER'})
    def test_assigned_ip_cannot_be_reinitialized(self):
        self.created()
        with self.assertRaises(domain.Conflict):self.control.request_stock('8.8.8.8')
    def test_stale_lifecycle_job_cannot_touch_reassigned_ip(self):
        self.control.add_ip(self.profile,'8.8.8.8');job=self.control.request_stock('8.8.8.8')
        with self.store.tx() as db:db.execute("UPDATE ip_pool SET job_id='different-owner' WHERE ip='8.8.8.8'")
        self.control.process_one()
        self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state'],'failed')
    def test_stock_dns_simulation_returns_default_name(self):
        self.control.add_ip(self.profile,'8.8.8.8');job=self.control.request_stock('8.8.8.8');self.control.process_one()
        result=json.loads(self.store.one('SELECT result FROM jobs WHERE id=?',(job,))['result'])
        self.assertEqual(result['fqdn'],'vps-8888.hosting.example.net')
    def test_purchased_ip_is_reserved_before_stock_is_exposed(self):
        self.control.add_ip(self.profile,'8.8.8.8');self.control.request_stock('8.8.8.8')
        model=self.store.document('models',self.model)
        purchase={'followup':{'model_id':self.model,'ssh_auth':'key','public_key':KEY},'model_snapshot':model,'delivered_ips':['8.8.8.8']}
        with self.store.tx() as db:db.execute("INSERT INTO purchases(id,profile_id,state,data,created_at) VALUES(?,?,'awaiting_delivery',?,0)",('paid',self.profile,json.dumps(purchase)))
        self.control.process_one()
        self.assertEqual(self.store.one('SELECT state FROM ip_pool WHERE ip=?',('8.8.8.8',))['state'],'reserved')
        self.assertEqual(self.store.document('purchases','paid')['state'],'delivered')
        self.assertEqual(len(self.control.deployments()),1)
    def test_unknown_ip_cannot_be_initialized(self):
        with self.assertRaises(KeyError):self.control.request_stock('9.9.9.9')
    def test_pf_foreign_route_refused_before_deletion(self):
        pf=PfSense.__new__(PfSense);pf.cfg={'wireguard_interface':'optwg','public_interface':'optpublic'}
        pf.snapshot=Mock(return_value=ET.fromstring('<pfsense><staticroutes><route><network>8.8.8.8/32</network><gateway>FOREIGN</gateway></route></staticroutes></pfsense>'))
        pf.native_post=Mock()
        with self.assertRaises(ValueError):pf.retire('8.8.8.8',{'next_hop':'10.254.251.11','mac':'02:00:00:00:00:01'})
        pf.native_post.assert_not_called()

    def test_release_keeps_vm_disks_and_removes_ip_assignment(self):
        c=self.created();job=self.control.request_retire(c['id'],{'mode':'release','confirmation':'LIBERER'})
        self.control.process_one();kept=self.control.deployments()[0]
        self.assertEqual(kept['vmid'],c['vmid']);self.assertIsNone(kept['ip'])
        self.assertEqual(kept['status'],'detached');self.assertTrue(kept['network_detached'])
        self.assertEqual(kept['last_attachment']['ip'],'8.8.8.8')
        self.assertEqual(self.store.one('SELECT state FROM ip_pool')['state'],'available')
        self.assertFalse(json.loads(self.store.one('SELECT result FROM jobs WHERE id=?',(job,))['result'])['retired'])
    def test_delete_only_holds_ip_until_explicit_release(self):
        c=self.created();job=self.control.request_retire(c['id'],{'mode':'delete','confirmation':'SUPPRIMER'})
        self.control.process_one();self.assertFalse(self.control.deployments())
        self.assertEqual(self.store.one('SELECT state FROM ip_pool')['state'],'reserved')
        self.assertIsNone(self.store.one('SELECT job_id FROM ip_pool')['job_id'])
        result=json.loads(self.store.one('SELECT result FROM jobs WHERE id=?',(job,))['result'])
        self.assertFalse(result['dns_verified'])
        self.control.request_stock('8.8.8.8');self.control.process_one()
        self.assertEqual(self.store.one('SELECT state FROM ip_pool')['state'],'available')
    def test_delete_detached_vm_does_not_touch_reassigned_ip(self):
        c=self.created();self.control.request_retire(c['id'],{'mode':'release','confirmation':'LIBERER'});self.control.process_one()
        self.deploy();self.control.process_one()
        new=next(d for d in self.control.deployments() if d['ip']=='8.8.8.8')
        self.control.request_retire(c['id'],{'mode':'delete','confirmation':'SUPPRIMER'});self.control.process_one()
        self.assertEqual([d['id'] for d in self.control.deployments()],[new['id']])
        self.assertEqual(self.store.one('SELECT state FROM ip_pool')['state'],'assigned')
    def test_release_confirmation_does_not_accept_delete_phrase(self):
        c=self.created()
        with self.assertRaises(ValueError):self.control.request_retire(c['id'],{'mode':'release','confirmation':'SUPPRIMER'})
    def test_unknown_action_rejected(self):
        c=self.created()
        with self.assertRaises(ValueError):self.control.request_retire(c['id'],{'mode':'anything','confirmation':'SUPPRIMER'})
    def test_detached_vm_cannot_release_same_ip_again(self):
        c=self.created();self.control.request_retire(c['id'],{'mode':'release','confirmation':'LIBERER'});self.control.process_one()
        with self.assertRaises(domain.Conflict):self.control.request_retire(c['id'],{'mode':'release','confirmation':'LIBERER'})
    def test_release_failure_keeps_vm_and_quarantines_ip(self):
        c=self.created();self.control.request_retire(c['id'],{'mode':'release','confirmation':'LIBERER'})
        with patch.object(self.control,'run_lifecycle',side_effect=RuntimeError('Partial detach')):self.control.process_one()
        self.assertEqual(self.control.deployments()[0]['ip'],'8.8.8.8')
        self.assertEqual(self.store.one('SELECT state FROM ip_pool')['state'],'quarantined')
    def test_custom_hostname_pattern(self):
        self.assertEqual(stock_hostname('8.8.8.8','hosting.example.net','node-{ipv4_dashed}.{domain}'),'node-8-8-8-8.hosting.example.net')

    def test_nullable_ip_migration_preserves_existing_records(self):
        import sqlite3
        from virtualwan.store import Store
        c=self.created()
        with sqlite3.connect(self.store.path) as db:
            db.execute('ALTER TABLE deployments RENAME TO current_deployments')
            db.execute('CREATE TABLE deployments(id TEXT PRIMARY KEY,ip TEXT NOT NULL UNIQUE,profile_id TEXT NOT NULL,model_id TEXT,data TEXT NOT NULL)')
            db.execute('INSERT INTO deployments SELECT * FROM current_deployments')
            db.execute('DROP TABLE current_deployments')
        db.close()
        migrated=Store(self.tmp.name)
        self.assertEqual(migrated.one('SELECT ip FROM deployments WHERE id=?',(c['id'],))['ip'],'8.8.8.8')
        with migrated.tx() as db:db.execute('UPDATE deployments SET ip=NULL WHERE id=?',(c['id'],))
        self.assertIsNone(migrated.one('SELECT ip FROM deployments')['ip'])


class AdapterLifecycleTests(unittest.TestCase):
    def exercise(self,mode,ip='8.8.8.8'):
        import tempfile
        from virtualwan.adapters import lifecycle
        with tempfile.TemporaryDirectory(dir=__import__('test_control').TESTROOT) as directory:
            config={'public_ip':ip,'domain':'hosting.example.net','lifecycle_mode':mode,
                    'retire_client':{'vmid':120,'mac':'02:00:00:00:00:01','public_ip':'8.8.8.8','next_hop':'10.254.251.11'},
                    'pve':{},'vps':{'primary_ip':'9.9.9.9','config_path':'/etc/virtualwan.json'},'pfsense':{},'cloudflare':{},'ovh':{}}
            path=Path(directory)/'lifecycle.json';path.write_text(json.dumps(config))
            pve=Mock();pve.call.return_value={'exists':True};vps=Mock();cf=Mock();cf.inspect.return_value=[];cf.call.return_value=[]
            with patch.object(sys,'argv',['lifecycle',str(path)]),patch.object(lifecycle,'Journal'),patch.object(lifecycle,'Remote',side_effect=[pve,vps]),patch.object(lifecycle,'PfSense') as pf,patch.object(lifecycle,'Cloudflare',return_value=cf) as cloud,patch.object(lifecycle,'Ovh') as ovh,patch.object(lifecycle,'wait_dns'):
                lifecycle.main()
            return pve,vps,pf,cloud,ovh
    def test_release_detaches_without_destroying_disks_and_resets_ptr(self):
        pve,_,_,_,ovh=self.exercise('release')
        actions=[c.args[0] for c in pve.call.call_args_list]
        self.assertIn('pve_retire_detach',actions);self.assertNotIn('pve_retire_destroy',actions)
        ovh.return_value.ensure.assert_called_once_with('8.8.8.8','vps-8888.hosting.example.net',force=True)
    def test_delete_only_never_constructs_dns_or_ovh_adapter(self):
        pve,_,_,cloud,ovh=self.exercise('delete')
        self.assertIn('pve_retire_destroy',[c.args[0] for c in pve.call.call_args_list])
        cloud.assert_not_called();ovh.assert_not_called()
    def test_delete_detached_vm_never_touches_router_or_pfsense(self):
        pve,vps,pf,cloud,ovh=self.exercise('delete',None)
        vps.call.assert_not_called();pf.assert_not_called();cloud.assert_not_called();ovh.assert_not_called()

    def test_firewall_conflict_does_not_stop_vm_or_connect_router(self):
        import tempfile
        from virtualwan.adapters import lifecycle
        with tempfile.TemporaryDirectory(dir=__import__('test_control').TESTROOT) as directory:
            config={'public_ip':'8.8.8.8','domain':'hosting.example.net','lifecycle_mode':'delete_release',
                    'retire_client':{'vmid':120,'mac':'02:00:00:00:00:01','next_hop':'10.254.251.11'},
                    'pve':{},'vps':{'primary_ip':'9.9.9.9'},'pfsense':{},'cloudflare':{},'ovh':{}}
            path=Path(directory)/'lifecycle.json';path.write_text(json.dumps(config))
            pve=Mock();pve.call.return_value={'exists':True}
            with patch.object(sys,'argv',['lifecycle',str(path)]),patch.object(lifecycle,'Journal'),patch.object(lifecycle,'Remote',return_value=pve) as remote_call,patch.object(lifecycle,'PfSense') as pf,patch.object(lifecycle,'Cloudflare'),patch.object(lifecycle,'Ovh'):
                pf.return_value.retire.side_effect=ValueError('Legacy gateway mismatch')
                with self.assertRaisesRegex(ValueError,'Legacy gateway'):lifecycle.main()
                self.assertEqual([c.args[0] for c in pve.call.call_args_list],['pve_retire_check'])
                self.assertEqual(remote_call.call_count,1)
                pf.return_value.retire.assert_called_once_with('8.8.8.8',config['retire_client'],check_only=True)


class PtrTests(unittest.TestCase):
    def ovh(self,old,fqdn):
        from virtualwan.adapters.provision import Ovh
        ovh=Ovh.__new__(Ovh);ovh.log=Mock();ovh.block='8.8.8.8%2F32';ovh.path='/ip/'+ovh.block+'/reverse/8.8.8.8'
        ovh.call=Mock(side_effect=[{'reverse':old},None,{}, {'reverse':fqdn}]);return ovh
    def test_customer_reverse_is_explicitly_reset(self):
        fqdn='vps-8888.hosting.example.net';ovh=self.ovh('mail.customer.example',fqdn)
        ovh.ensure('8.8.8.8',fqdn,force=True)
        self.assertEqual([c.args[0] for c in ovh.call.call_args_list],['GET','DELETE','POST','GET'])
        self.assertEqual(ovh.call.call_args_list[2].args[2],{'ipReverse':'8.8.8.8','reverse':fqdn})
    def test_even_matching_reverse_gets_supplier_reset(self):
        fqdn='vps-8888.hosting.example.net';ovh=self.ovh(fqdn,fqdn)
        ovh.ensure('8.8.8.8',fqdn,force=True);self.assertEqual(ovh.call.call_count,4)
    def test_normal_assignment_is_idempotent(self):
        fqdn='vps-8888.hosting.example.net';ovh=self.ovh(fqdn,fqdn)
        ovh.ensure('8.8.8.8',fqdn);self.assertEqual(ovh.call.call_count,1)

    def test_pf_legacy_conflict_reports_actual_gateway_without_deletion(self):
        pf=PfSense.__new__(PfSense);pf.cfg={'wireguard_interface':'optwg','public_interface':'optpublic'}
        pf.log=Mock();pf.snapshot=Mock(return_value=ET.fromstring('<pfsense><staticroutes><route><network>8.8.8.8/32</network><gateway>LEGACY_VM_GW</gateway></route></staticroutes></pfsense>'))
        pf.native_post=Mock()
        with self.assertRaisesRegex(ValueError,'LEGACY_VM_GW'):
            pf.retire('8.8.8.8',{'next_hop':'10.254.251.11','mac':'02:00:00:00:00:01'},check_only=True)
        pf.native_post.assert_not_called();pf.log.event.assert_called_once()
    def test_pf_preflight_performs_no_deletions_or_state_purge(self):
        pf=PfSense.__new__(PfSense);pf.cfg={'wireguard_interface':'optwg','public_interface':'optpublic'}
        pf.log=Mock();pf.snapshot=Mock(return_value=ET.fromstring('<pfsense><staticroutes><route><network>8.8.8.8/32</network><gateway>VWAN_8888</gateway></route></staticroutes></pfsense>'))
        pf.native_post=Mock()
        pf.retire('8.8.8.8',{'next_hop':'10.254.251.11','mac':'02:00:00:00:00:01'},check_only=True)
        pf.native_post.assert_not_called()



class RecoveryTests(unittest.TestCase):
    setUp=LifecycleTests.setUp
    tearDown=LifecycleTests.tearDown
    deploy=LifecycleTests.deploy
    created=LifecycleTests.created
    def failed_retirement(self):
        c=self.created();job=self.control.request_retire(c['id'],{'confirmation':'SUPPRIMER'})
        with patch.object(self.control,'run_lifecycle',side_effect=ValueError('gateway mismatch')):self.control.process_one()
        return c,job
    def test_failed_retirement_updates_vm_state(self):
        c,job=self.failed_retirement()
        self.assertEqual(self.control.deployments()[0]['status'],'retirement_failed')
        self.assertEqual(self.control.deployments()[0]['lifecycle_job']['id'],job)
    def test_old_stuck_vm_state_is_repaired_on_read(self):
        c,job=self.failed_retirement()
        with self.store.tx() as db:
            data=json.loads(db.execute('SELECT data FROM deployments WHERE id=?',(c['id'],)).fetchone()['data'])
            data['status']='retiring';db.execute('UPDATE deployments SET data=? WHERE id=?',(json.dumps(data),c['id']))
        self.assertEqual(self.control.deployments()[0]['status'],'retirement_failed')
    def test_retry_resumes_same_vm_and_lease(self):
        c,job=self.failed_retirement();self.control.recover_job(job,'retry','REPRENDRE '+job[:8])
        self.assertEqual(self.control.deployments()[0]['status'],'retiring')
        self.assertEqual(self.store.one('SELECT job_id,state FROM ip_pool')['job_id'],job)
        self.control.process_one();self.assertFalse(self.control.deployments())
        self.assertEqual(self.store.one('SELECT state FROM ip_pool')['state'],'available')
    def test_cancel_keeps_partial_resources_and_can_resume_same_job(self):
        c,job=self.failed_retirement();self.control.recover_job(job,'cancel','ANNULER '+job[:8])
        self.assertEqual(self.control.deployments()[0]['status'],'retirement_cancelled')
        self.assertEqual(self.control.deployments()[0]['id'],c['id'])
        self.assertEqual(self.store.one('SELECT state,job_id FROM ip_pool')['state'],'quarantined')
        self.assertFalse(self.control.process_one())
        self.control.recover_job(job,'retry','REPRENDRE '+job[:8]);self.control.process_one()
        self.assertFalse(self.control.deployments())
    def test_cancel_queued_job_never_calls_adapter(self):
        c=self.created();job=self.control.request_retire(c['id'],{'confirmation':'SUPPRIMER'})
        self.control.recover_job(job,'cancel','ANNULER '+job[:8])
        with patch.object(self.control,'run_lifecycle',side_effect=AssertionError('network call')):self.assertFalse(self.control.process_one())
    def test_running_job_cannot_be_cancelled_or_requeued(self):
        c=self.created();job=self.control.request_retire(c['id'],{'confirmation':'SUPPRIMER'})
        self.control.job_state(job,'running',{})
        for action,phrase in [('cancel','ANNULER '),('retry','REPRENDRE ')]:
            with self.assertRaises(domain.Conflict):self.control.recover_job(job,action,phrase+job[:8])
    def test_reassigned_ip_refuses_recovery(self):
        c,job=self.failed_retirement()
        with self.store.tx() as db:db.execute("UPDATE ip_pool SET job_id='other-owner'")
        for action,phrase in [('cancel','ANNULER '),('retry','REPRENDRE ')]:
            with self.assertRaises(domain.Conflict):self.control.recover_job(job,action,phrase+job[:8])
    def test_cancel_confirmation_is_required(self):
        c,job=self.failed_retirement()
        with self.assertRaises(ValueError):self.control.recover_job(job,'cancel','yes')


class CatalogueTests(unittest.TestCase):
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    def draft(self):
        p=self.store.document('profiles',self.profile);api=DemoOvh({},self.store,'test')
        body={'kind':'additional_ip','planCode':'DEMO-ip','duration':'P1M','pricingMode':'default','quantity':1}
        result=api.describe(p,body)
        body.update(profile_id=p['id'],draft_id=result['draft_id'],item_configurations={'1':{'country':'FR'}})
        return api,p,body,result
    def test_draft_ui_fields_have_countries(self):
        _,_,_,d=self.draft();self.assertEqual(d['fields'][0]['values'],['FR','DE','UK'])
    def test_draft_produces_bound_quote(self):
        api,p,b,_=self.draft();r=api.prepare(p,b);self.assertIn('quote_digest',r)
    def test_draft_cannot_be_consumed_twice(self):
        api,p,b,_=self.draft();api.prepare(p,b)
        with self.assertRaises(ValueError):api.prepare(p,b)
    def test_country_outside_supplier_list_refused(self):
        api,p,b,_=self.draft();b['item_configurations']['1']['country']='ZZ'
        with self.assertRaises(ValueError):api.prepare(p,b)
    def test_required_country_cannot_be_omitted(self):
        api,p,b,_=self.draft();b['item_configurations']={}
        with self.assertRaises(ValueError):api.prepare(p,b)
    def test_draft_profile_revision_is_bound(self):
        api,p,b,_=self.draft();p['revision']+=1
        with self.assertRaises(domain.Conflict):api.prepare(p,b)
    def test_draft_selection_cannot_be_changed(self):
        api,p,b,_=self.draft();b['quantity']=2
        with self.assertRaises(domain.Conflict):api.prepare(p,b)
    def test_unknown_configuration_rejected(self):
        api,p,b,_=self.draft();b['item_configurations']['1']['unexpected']='foo'
        with self.assertRaises(ValueError):api.prepare(p,b)
    def test_enrichment_uses_official_technical_blocks(self):
        offers=[{'planCode':'ssd','productName':'Generic','prices':[]}]
        catalog={'plans':[{'planCode':'ssd','invoiceName':'VPS SSD','product':'hardware'}],
                 'products':[{'name':'hardware','blobs':{'technical':{'cpu':{'cores':4},'memory':{'size':8}}}}]}
        self.assertEqual(enrich(offers,catalog)[0]['technical']['memory']['size'],8)
    def test_ip_filter_excludes_disk_options(self):
        self.assertTrue(is_ip_offer({'planCode':'ip','family':'Additional IP'}))
        self.assertFalse(is_ip_offer({'planCode':'option-additional-disk-50g','family':'additionalDisk'}))
    def test_new_form_quote_runs_through_worker(self):
        _,_,b,_=self.draft();r=self.control.request_quote(b);self.control.process_one()
        self.assertEqual(self.store.document('purchases',r['purchase_id'])['state'],'awaiting_approval')


@unittest.skipIf(sys.platform=='win32','Remote Linux actions require fcntl')
class RemoteRetireTests(unittest.TestCase):
    def fixture(self,action,mac='02:00:00:00:00:01',template=False):
        client={'vmid':120,'mac':'02:00:00:00:00:01','public_ip':'8.8.8.8','next_hop':'10.254.251.11'}
        registry={'nodes':{'pve-a':{'bridge':'vmbr12'}},'clients':[client]}
        q={'action':action,'pve':{'template':9001,'node':'pve-a'},'client':client}
        def command(*args,**kw):
            if '/cluster/resources' in args:return json.dumps([{'vmid':120,'node':'pve-a','status':'running'}])
            if '/nodes/pve-a/qemu/120/config' in args:return json.dumps({'net0':'virtio='+mac+',bridge=vmbr12','template':1 if template else 0})
            raise AssertionError('Mutation attempted: '+str(args))
        return q,registry,command
    def test_wrong_mac_refuses_even_stop(self):
        q,registry,cmd=self.fixture('pve_retire_stop',mac='02:00:00:00:00:02')
        with patch.object(sys,'stdin',io.StringIO(json.dumps(q))),patch.object(remote.Path,'read_text',return_value=json.dumps(registry)),patch.object(remote,'run',side_effect=cmd):
            with self.assertRaises(ValueError):remote.main()
    def test_template_refused_even_stop(self):
        q,registry,cmd=self.fixture('pve_retire_stop',template=True)
        with patch.object(sys,'stdin',io.StringIO(json.dumps(q))),patch.object(remote.Path,'read_text',return_value=json.dumps(registry)),patch.object(remote,'run',side_effect=cmd):
            with self.assertRaises(ValueError):remote.main()
    def test_preflight_only_reads_owned_vm(self):
        q,registry,cmd=self.fixture('pve_retire_check')
        with patch.object(sys,'stdin',io.StringIO(json.dumps(q))),patch.object(remote.Path,'read_text',return_value=json.dumps(registry)),patch.object(remote,'run',side_effect=cmd),patch.object(remote,'emit') as emit:
            remote.main();self.assertTrue(emit.call_args[0][0]['exists'])


@unittest.skipIf(sys.platform=='win32','Router uses Linux fcntl')
class IdleRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec=importlib.util.spec_from_file_location('idle_router',Path(__file__).parents[1]/'tools/virtualwan-router.py')
        cls.router=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.router)
    def config(self):return {'primary_ip':'8.8.8.8','additional_ips':['9.9.9.9'],'wan_interface':'ens3','wireguard_interface':'wg-vwan','listen_port':51820,'mtu':1420,'tunnel_address':'10.254.250.1/30','peer_address':'10.254.250.2','peer_public_key':None}
    def load(self,c):
        with patch.object(self.router.Path,'read_text',return_value=json.dumps(c)):return self.router.load('fixture')
    def test_legacy_config_is_accepted(self):self.assertEqual(self.load(self.config())['additional_ips'],['9.9.9.9'])
    def test_primary_cannot_be_stock(self):
        c=self.config();c['inactive_ips']=['8.8.8.8']
        with self.assertRaises(ValueError):self.load(c)
    def test_ip_cannot_be_active_and_inactive(self):
        c=self.config();c['inactive_ips']=['9.9.9.9']
        with self.assertRaises(ValueError):self.load(c)
    def test_stock_drop_precedes_forward_accept(self):
        c=self.config();c['inactive_ips']=['1.1.1.1'];rules=self.router.firewall(self.load(c))
        self.assertLess(rules.index('ip daddr @inactive_ips counter drop'),rules.index('ip daddr @public_ips counter accept'))


del Fixture

if __name__=='__main__':unittest.main()
