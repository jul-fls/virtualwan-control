from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0,str(Path(__file__).parents[1]))
from virtualwan import domain
from virtualwan.control import Control
from virtualwan.providers import DemoOvh
from virtualwan.store import Store
from virtualwan.web import create_app

EXAMPLE=Path(__file__).parents[1]/'examples/profile.json'
KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test'
TESTROOT=Path(os.environ.get('VWAN_TEST_TMPDIR',Path(__file__).parents[1]/'data'))
TESTROOT.mkdir(parents=True,exist_ok=True)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=TESTROOT);self.store=Store(self.tmp.name)
        self.control=Control(self.store,demo=True)
        p=json.loads(EXAMPLE.read_text());p['ready']=True;p['config']['ovh']['expected_service']='vps-example.example.net'
        self.profile=self.control.add_profile(p)
        self.model=self.control.add_model({'name':'Standard','range':'Essentiel','cores':2,'memory_mb':2048,'disk_gb':32,'template':9001})
    def tearDown(self):self.tmp.cleanup()
    def deploy(self,**extra):return self.control.request_deploy({'profile_id':self.profile,'model_id':self.model,'ssh_auth':'key','public_key':KEY,**extra})
    def quote(self,**extra):
        q=self.control.request_quote({'profile_id':self.profile,'kind':'additional_ip','quantity':1,'planCode':'DEMO-ip','duration':'P1M','pricingMode':'default',**extra})
        self.control.process_one();return self.store.document('purchases',q['purchase_id'])
    def approve(self,p,**extra):return self.control.approve(p['id'],{'revision':p['revision'],'confirmation':'COMMANDER '+p['id'][:8],'accept_contracts':True,**extra},'admin')

    def test_double_reservation_prevented(self):
        self.control.add_ip(self.profile,'8.8.8.8')
        def allocate(_):
            try:return self.deploy()
            except domain.Conflict:return None
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(allocate,range(2)))
        self.assertEqual(sum(x is not None for x in result),1)

    def test_demo_deploy_never_calls_provider(self):
        self.control.add_ip(self.profile,'8.8.8.8');key=self.deploy()
        with patch('requests.request',side_effect=AssertionError('network')):self.assertTrue(self.control.process_one())
        self.assertEqual(self.control.deployments()[0]['status'],'simulated')
        self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(key,))['state'],'succeeded')

    def test_failed_deploy_quarantines_ip(self):
        self.control.add_ip(self.profile,'8.8.8.8');self.deploy()
        with patch.object(self.control,'run_deploy',side_effect=RuntimeError('test failure')):self.control.process_one()
        self.assertEqual(self.store.one('SELECT state FROM ip_pool')['state'],'quarantined')

    def test_capacity_includes_pending_quotes(self):
        p=self.store.document('profiles',self.profile);p['capacity']=1;self.control.add_profile(p,self.profile)
        self.quote()
        with self.assertRaises(domain.Conflict):self.quote()

    def test_primary_ip_is_never_stock(self):
        p=self.store.document('profiles',self.profile);p['config']['vps']['primary_ip']='8.8.8.8';self.control.add_profile(p,self.profile)
        with self.assertRaises(ValueError):self.control.add_ip(self.profile,'8.8.8.8')

    def test_disabled_real_deployment(self):
        self.control.demo=False;self.control.add_ip(self.profile,'8.8.8.8')
        with self.assertRaises(domain.Conflict):self.deploy()

    def test_purchase_requires_phrase(self):
        p=self.quote()
        with self.assertRaises(ValueError):self.approve(p,confirmation='yes')
        self.assertEqual(self.store.document('purchases',p['id'])['state'],'awaiting_approval')

    def test_contract_acceptance_required(self):
        with self.assertRaises(ValueError):self.approve(self.quote(),accept_contracts=False)

    def test_expired_quote_cannot_buy(self):
        p=self.quote();p['expires_at']=time.time()-1
        with self.store.tx() as db:db.execute('UPDATE purchases SET data=? WHERE id=?',(json.dumps(p),p['id']))
        with self.assertRaises(domain.Conflict):self.approve(p)

    def test_quote_revision_bound(self):
        with self.assertRaises(domain.Conflict):self.approve(self.quote(),revision=999)

    def test_supplier_price_change_requires_new_approval(self):
        p=self.quote();fresh=deepcopy(p['supplier_quote']);fresh['prices']['withTax']['value']=999
        with self.assertRaises(domain.Conflict):domain.approval_valid(p,{'revision':p['revision'],'confirmation':'COMMANDER '+p['id'][:8],'accept_contracts':True},fresh)

    def test_nested_supplier_tax_and_discount_are_bound(self):
        p=self.quote();fresh=deepcopy(p['supplier_quote']);fresh['prices']['tax']['value']=5
        with self.assertRaises(domain.Conflict):domain.approval_valid(p,{'revision':p['revision'],'confirmation':'COMMANDER '+p['id'][:8],'accept_contracts':True},fresh)

    def test_unknown_supplier_price_schema_cannot_be_approved(self):
        p=self.quote();fresh={'contracts':[],'details':[]}
        with self.assertRaises(ValueError):domain.approval_valid(p,{'revision':p['revision'],'confirmation':'COMMANDER '+p['id'][:8],'accept_contracts':True},fresh)

    def test_real_price_schema_is_read_correctly(self):
        self.assertEqual(domain.amount({'prices':{'withTax':{'currencyCode':'CAD','value':3.12}}})['value'],3.12)

    def test_negative_price_cannot_be_approved(self):
        with self.assertRaises(ValueError):domain.amount({'prices':{'withTax':{'currencyCode':'EUR','value':-2}}})

    def test_profile_change_invalidates_quote(self):
        p=self.quote();profile=self.store.document('profiles',self.profile);profile['name']='Changed';self.control.add_profile(profile,self.profile)
        with self.assertRaises(domain.Conflict):self.approve(p)

    def test_checkout_sent_once(self):
        p=self.quote();calls=[]
        class Provider(DemoOvh):
            def checkout(self,*a,**k):calls.append(a);return super().checkout(*a,**k)
        self.control.provider_factory=Provider;self.approve(p)
        with self.assertRaises(domain.Conflict):self.approve(p)
        self.assertEqual(len(calls),1)

    def test_checkout_timeout_is_uncertain_not_retried(self):
        p=self.quote();calls=[]
        class Provider(DemoOvh):
            def checkout(self,*a,**k):calls.append(a);raise TimeoutError('ambiguous')
        self.control.provider_factory=Provider
        with self.assertRaises(domain.Conflict):self.approve(p)
        self.assertEqual(self.store.document('purchases',p['id'])['state'],'uncertain')
        with self.assertRaises(domain.Conflict):self.approve(p)
        self.assertEqual(len(calls),1)

    def test_delivery_waits_until_supplier_delivered(self):
        p=self.quote();p['order']={'orderId':123}
        with self.store.tx() as db:db.execute("UPDATE purchases SET state='awaiting_payment',data=? WHERE id=?",(json.dumps(p),p['id']))
        class Provider(DemoOvh):
            def delivery(self,order):return {'status':'notPaid','details':[]}
        self.control.provider_factory=Provider
        result=self.control.follow_delivery(p['id'],'a'*32)
        self.assertEqual(result['state'],'awaiting_payment');self.assertEqual(self.control.deployments(),[])

    def test_snapshot_of_combined_order_model_is_preserved(self):
        p=self.quote(followup={'model_id':self.model,'public_key':KEY,'ssh_auth':'key'})
        self.control.add_model({'name':'Changed','range':'Premium','cores':8,'memory_mb':8192,'disk_gb':100,'template':9001},self.model)
        self.assertEqual(p['model_snapshot']['cores'],2)

    def test_router_purchase_cannot_replace_existing_router(self):
        with self.assertRaises(domain.Conflict):self.quote(kind='router')

    def test_clone_clears_binding_and_forces_new_segment(self):
        key=self.control.clone_profile(self.profile,'Other','10.254.252.0/24','10.254.252.1')
        p=self.store.document('profiles',key)
        self.assertFalse(p['ready']);self.assertIsNone(p['config']['ovh']['expected_service']);self.assertEqual(p['config']['vps']['host'],'')
        self.assertIsNotNone(p['config']['pve']['isolation_segment']);self.assertNotEqual(p['config']['pfsense']['public_alias'],self.store.document('profiles',self.profile)['config']['pfsense']['public_alias'])

    def test_overlapping_segments_rejected(self):
        with self.assertRaises(domain.Conflict):self.control.clone_profile(self.profile,'Other','10.254.251.128/25','10.254.251.129')

    def test_secret_is_encrypted_and_log_redacted(self):
        value='a-super-private-secret-test';self.store.secret_set('TEST_SECRET',value);self.store.event('a'*32,'test',message=value,password=value)
        self.assertEqual(self.store.secret_get('TEST_SECRET'),value)
        self.assertNotIn(value,(Path(self.tmp.name)/'runs'/('a'*32)/'events.jsonl').read_text())
        self.assertNotIn(value.encode(),(Path(self.tmp.name)/'control.sqlite3').read_bytes())

    def test_restart_does_not_retry_running_purchase(self):
        p=self.quote()
        with self.store.tx() as db:db.execute("UPDATE purchases SET state='checking_out' WHERE id=?",(p['id'],))
        other=Store(self.tmp.name);self.assertEqual(other.document('purchases',p['id'])['state'],'uncertain')

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=TESTROOT);self.app=create_app({'TESTING':True,'ADMIN_PASSWORD':'test-password-with-entropy','DATA_DIR':self.tmp.name,'DEMO':True,'START_WORKER':False});self.client=self.app.test_client()
    def tearDown(self):self.tmp.cleanup()
    def login(self):
        response=self.client.post('/api/login',json={'username':'admin','password':'test-password-with-entropy'})
        self.assertEqual(response.status_code,200);return response.json['csrf']
    def test_api_requires_session(self):self.assertEqual(self.client.get('/api/state').status_code,401)
    def test_csrf_required_for_mutation(self):
        self.login();self.assertEqual(self.client.post('/api/purchases',json={}).status_code,403)
    def test_cross_origin_login_refused(self):
        self.assertEqual(self.client.post('/api/login',json={},headers={'Origin':'https://evil.example.net'}).status_code,403)
    def test_dns_rebinding_host_refused(self):self.assertEqual(self.client.get('/health',headers={'Host':'evil.example.net'}).status_code,400)
    def test_secret_values_not_returned(self):
        csrf=self.login();response=self.client.put('/api/secrets/TEST_API_TOKEN',json={'value':'never-return-this'},headers={'X-CSRF-Token':csrf});self.assertEqual(response.status_code,200,response.json)
        state=self.client.get('/api/state');self.assertNotIn('never-return-this',state.get_data(as_text=True));self.assertIn('TEST_API_TOKEN',state.json['secret_names'])
    def test_bootstrap_is_download_not_network_change(self):
        self.login();key=self.client.get('/api/state').json['profiles'][0]['id'];response=self.client.get('/api/profiles/'+key+'/bootstrap')
        self.assertEqual(response.status_code,200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as z:self.assertIn('REPLACE_ACTUAL_UPLINK_PORT',z.read('segment-fragment.json').decode())
    def test_demo_volume_cannot_become_live(self):
        with self.assertRaises(ValueError):create_app({'ADMIN_PASSWORD':'test-password-with-entropy','DATA_DIR':self.tmp.name,'DEMO':False,'START_WORKER':False})
    def test_security_headers(self):
        response=self.client.get('/');self.assertEqual(response.headers['X-Frame-Options'],'DENY');self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
    def test_get_cannot_approve_purchase(self):
        self.login();self.assertEqual(self.client.get('/api/purchases/'+'a'*32+'/approve').status_code,405)

    def test_complete_web_deploy_and_purchase_flow(self):
        csrf=self.login();headers={'X-CSRF-Token':csrf};state=self.client.get('/api/state').json
        p=state['profiles'][0];m=state['models'][0];control=self.app.extensions['control']
        response=self.client.post('/api/deployments',json={'profile_id':p['id'],'model_id':m['id'],'ip':'192.0.2.21','public_key':KEY,'ssh_auth':'key'},headers=headers)
        self.assertEqual(response.status_code,202,response.json);control.process_one()
        self.assertEqual(self.client.get('/api/events/'+response.json['job_id']).status_code,200)
        response=self.client.post('/api/purchases',json={'profile_id':p['id'],'quantity':1,'kind':'additional_ip','planCode':'DEMO-ip','duration':'P1M','pricingMode':'default'},headers=headers)
        self.assertEqual(response.status_code,202,response.json);key=response.json['purchase_id'];control.process_one()
        purchase=self.app.extensions['store'].document('purchases',key)
        response=self.client.post('/api/purchases/'+key+'/approve',json={'revision':purchase['revision'],'confirmation':'COMMANDER '+key[:8],'accept_contracts':True},headers=headers)
        self.assertEqual(response.status_code,200,response.json);self.assertEqual(response.json['state'],'simulated')

    def test_profile_model_edit_and_clone_endpoints(self):
        csrf=self.login();h={'X-CSRF-Token':csrf};state=self.client.get('/api/state').json;p=state['profiles'][0];m=state['models'][0]
        self.assertEqual(self.client.put('/api/profiles/'+p['id'],json=p,headers=h).status_code,200)
        self.assertEqual(self.client.put('/api/models/'+m['id'],json=m,headers=h).status_code,200)
        response=self.client.post('/api/profiles/'+p['id']+'/clone',json={'name':'Other','network':'10.254.252.0/24','gateway':'10.254.252.1'},headers=h)
        self.assertEqual(response.status_code,201,response.json)

    def test_inventory_queue_endpoints(self):
        csrf=self.login();h={'X-CSRF-Token':csrf};key=self.client.get('/api/state').json['profiles'][0]['id']
        self.assertEqual(self.client.post('/api/profiles/'+key+'/sync',json={},headers=h).status_code,202)
        self.assertEqual(self.client.post('/api/profiles/'+key+'/inspect',json={},headers=h).status_code,202)

    def test_cancel_quote_is_not_revived_by_worker(self):
        csrf=self.login();h={'X-CSRF-Token':csrf};key=self.client.get('/api/state').json['profiles'][0]['id']
        response=self.client.post('/api/purchases',json={'profile_id':key,'quantity':1,'kind':'additional_ip','planCode':'DEMO-ip','duration':'P1M','pricingMode':'default'},headers=h)
        key=response.json['purchase_id'];self.assertEqual(self.client.post('/api/purchases/'+key+'/cancel',json={},headers=h).status_code,200)
        self.app.extensions['control'].process_one();self.assertEqual(self.app.extensions['store'].document('purchases',key)['state'],'cancelled')

if __name__=='__main__':unittest.main()
