import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from virtualwan import domain
from virtualwan.control import Control
from virtualwan.store import Store
from virtualwan.web import create_app


class ProfileDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('VWAN_TEST_TMPDIR'))
        self.store=Store(self.tmp.name);self.control=Control(self.store,demo=False)
        self.p=json.loads((Path(__file__).parents[1]/'examples/profile.json').read_text())
        self.p['ready']=False;self.key=self.control.add_profile(self.p)
        self.env=patch.dict(os.environ,{},clear=True);self.env.start()
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def remove(self):return self.control.delete_profile(self.key,'SUPPRIMER')
    def purchase(self,state):
        with self.store.tx() as db:db.execute('INSERT INTO purchases VALUES(?,?,?,?,?,?)',('quote',self.key,state,'{}',1,time.time()))
    def job(self,kind,state,simulated=False):
        key=self.store.job(kind,{'profile_id':self.key,'simulated':simulated})
        with self.store.tx() as db:db.execute('UPDATE jobs SET state=? WHERE id=?',(state,key))
        return key
    def test_unused_draft_removed_locally_and_audited(self):
        self.store.secret_set('SHARED_TEST_SECRET','kept-value')
        with patch('requests.request',side_effect=AssertionError('No network')):
            self.assertEqual(self.remove(),{'ok':True,'remote_changes':False})
        self.assertFalse(self.store.rows('SELECT id FROM profiles'))
        self.assertEqual(self.store.secret_get('SHARED_TEST_SECRET'),'kept-value')
        self.assertEqual(self.store.one("SELECT action FROM events WHERE action='profile.deleted'")['action'],'profile.deleted')
        with self.assertRaises(KeyError):self.remove()
    def test_confirmation_required(self):
        with self.assertRaises(ValueError):self.control.delete_profile(self.key,'')
        self.assertTrue(self.store.rows('SELECT id FROM profiles'))
    def test_ready_profile_refused(self):
        self.p['ready']=True;self.control.add_profile(self.p,self.key)
        with self.assertRaisesRegex(domain.Conflict,'prêt'):self.remove()
    def test_vm_or_ip_dependency_refused(self):
        with self.store.tx() as db:db.execute('INSERT INTO deployments VALUES(?,?,?,?,?)',('vm',None,self.key,None,'{}'))
        with self.assertRaisesRegex(domain.Conflict,'VM'):self.remove()
        with self.store.tx() as db:
            db.execute('DELETE FROM deployments')
            db.execute('INSERT INTO ip_pool VALUES(?,?,?,?,?)',('8.8.8.8',self.key,'available',None,time.time()))
        with self.assertRaisesRegex(domain.Conflict,'IP'):self.remove()
    def test_pending_or_delivered_purchase_refused(self):
        for state in ('preparing','awaiting_approval','checking_out','awaiting_payment','awaiting_delivery','uncertain','delivered','needs_setup'):
            with self.subTest(state=state):
                self.purchase(state)
                with self.assertRaisesRegex(domain.Conflict,'achat'):self.remove()
                with self.store.tx() as db:db.execute('DELETE FROM purchases')
    def test_closed_quote_and_readonly_history_preserved(self):
        self.purchase('cancelled');job=self.job('inspect','succeeded')
        self.remove()
        self.assertEqual(self.store.one('SELECT state FROM purchases')['state'],'cancelled')
        self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state'],'succeeded')
    def test_active_jobs_and_real_installation_refused(self):
        for kind,state in (('sync','queued'),('inspect','running'),('router_setup','interrupted'),('router_setup','failed'),('router_setup','succeeded'),('deploy','cancelled')):
            with self.subTest(kind=kind,state=state):
                self.job(kind,state)
                with self.assertRaises(domain.Conflict):self.remove()
                with self.store.tx() as db:db.execute('DELETE FROM jobs')
    def test_environment_profile_refused_until_removed(self):
        os.environ['VWAN_PROFILES_JSON']=json.dumps([dict(self.p,id=self.key)])
        with self.assertRaisesRegex(domain.Conflict,'VWAN_PROFILES_JSON'):self.remove()
        os.environ.pop('VWAN_PROFILES_JSON');self.remove()
    def test_targeted_environment_overrides_refused(self):
        os.environ.update(VWAN_PROFILE_ID=self.key,VWAN_PVE_HOST='192.0.2.10')
        with self.assertRaisesRegex(domain.Conflict,'variables'):self.remove()
        os.environ['VWAN_PROFILE_ID']='another-profile';self.remove()
    def test_late_job_cannot_reference_deleted_draft(self):
        self.remove()
        with self.assertRaises(KeyError):self.store.job('sync',{'profile_id':self.key})
        self.assertFalse(self.store.rows('SELECT id FROM jobs'))
    def test_delete_api_auth_csrf_and_success(self):
        app=create_app({'TESTING':True,'ADMIN_PASSWORD':'test-password-with-entropy','DATA_DIR':self.tmp.name,'DEMO':False,'START_WORKER':False})
        client=app.test_client();url='/api/profiles/'+self.key
        self.assertEqual(client.delete(url,json={'confirmation':'SUPPRIMER'}).status_code,401)
        csrf=client.post('/api/login',json={'username':'admin','password':'test-password-with-entropy'}).json['csrf']
        self.assertEqual(client.delete(url,json={'confirmation':'SUPPRIMER'}).status_code,403)
        headers={'X-CSRF-Token':csrf}
        self.assertEqual(client.delete(url,json={'confirmation':'no'},headers=headers).status_code,400)
        response=client.delete(url,json={'confirmation':'SUPPRIMER'},headers=headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(client.get('/api/state').json['profiles'],[])
        self.assertEqual(client.delete(url,json={'confirmation':'SUPPRIMER'},headers=headers).status_code,404)
