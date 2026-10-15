import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from virtualwan.control import Control
from virtualwan.environment import apply_environment
from virtualwan.store import Store
from virtualwan.web import create_app
from virtualwan import domain
from virtualwan.adapters import remote
from virtualwan.adapters.provision import require_ssh_host_key

ROOT=Path(__file__).parents[1]
TESTROOT=Path(os.environ.get('VWAN_TEST_TMPDIR',ROOT/'data'))
TESTROOT.mkdir(parents=True,exist_ok=True)


class EnvironmentRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=TESTROOT);self.store=Store(self.tmp.name);self.control=Control(self.store,demo=True)
        self.profile=json.loads((ROOT/'examples/profile.json').read_text());self.profile['id']='router-test'
        self.profile['config']['ovh']['expected_service']='vps-example.example.net'
        self.profile['config']['router']['peer_public_key']='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='
    def tearDown(self):self.tmp.cleanup()
    def app(self,password='Testing-admin-password',**extra):
        return create_app({'TESTING':True,'START_WORKER':False,'DATA_DIR':self.tmp.name,'ADMIN_PASSWORD':password,'DEMO':True,**extra})
    def test_environment_profiles_idempotent(self):
        with patch.dict(os.environ,{'VWAN_PROFILES_JSON':json.dumps([self.profile])}):
            apply_environment(self.control);apply_environment(self.control)
        self.assertEqual(self.store.document('profiles','router-test')['revision'],1)
    def test_environment_model_import_does_not_overwrite_database(self):
        model={'id':'small','name':'Small','cores':1,'memory_mb':512,'disk_gb':8}
        with patch.dict(os.environ,{'VWAN_MODELS_JSON':json.dumps([model])}):apply_environment(self.control)
        model['memory_mb']=1024
        with patch.dict(os.environ,{'VWAN_MODELS_JSON':json.dumps([model])}):apply_environment(self.control)
        self.assertEqual(self.store.document('models','small')['memory_mb'],512)
    def test_environment_secret_has_priority(self):
        self.store.secret_set('PROVIDER_SECRET','older-value')
        with patch.dict(os.environ,{'PROVIDER_SECRET':'new-environment-value'}):self.assertEqual(self.store.secret_get('PROVIDER_SECRET'),'new-environment-value')
    def test_auth_rotation_invalidates_existing_session(self):
        client=self.app().test_client();response=client.post('/api/login',json={'username':'admin','password':'Testing-admin-password'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(self.store.rows('SELECT * FROM sessions')),1)
        self.app(password='A-different-testing-password')
        self.assertEqual(len(self.store.rows('SELECT * FROM sessions')),0)
    def test_restart_same_password_retains_session(self):
        self.app().test_client().post('/api/login',json={'username':'admin','password':'Testing-admin-password'})
        self.app();self.assertEqual(len(self.store.rows('SELECT * FROM sessions')),1)
    def test_invalid_public_url_rejected(self):
        with patch.dict(os.environ,{'VWAN_PUBLIC_URL':'ftp://console.example.net'}):
            with self.assertRaises(ValueError):self.app()
    def test_router_requires_confirmation(self):
        key=self.control.add_profile(self.profile)
        with self.assertRaises(ValueError):self.control.request_router_setup(key,{'confirmation':'yes'})
    def test_router_demo_does_not_connect_and_does_not_mark_ready(self):
        key=self.control.add_profile(self.profile)
        job=self.control.request_router_setup(key,{'confirmation':'CONFIGURER '+key[:8]})
        with patch('paramiko.SSHClient',side_effect=AssertionError('network')):self.control.process_one()
        self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state'],'succeeded')
        self.assertFalse(self.store.document('profiles',key)['ready'])
    def test_router_setup_refuses_overlapping_tunnel(self):
        key=self.control.add_profile(self.profile)
        second=json.loads(json.dumps(self.profile));second['config']['vps']['primary_ip']='192.0.2.11'
        second['config']['ovh']['expected_service']='vps-other.example.net';second['config']['pfsense']['next_hop_network']='10.254.252.0/24'
        second['config']['guest']['gateway']='10.254.252.1';second['config']['pfsense']['public_interface']='opt-other'
        self.control.add_profile(second)
        with self.assertRaises(domain.Conflict):self.control.request_router_setup(key,{'confirmation':'CONFIGURER '+key[:8]})
    def test_router_setup_disabled_real_mode(self):
        key=self.control.add_profile(self.profile);self.control.demo=False
        with self.assertRaises(domain.Conflict):self.control.request_router_setup(key,{'confirmation':'CONFIGURER '+key[:8]})
    def test_duplicate_setup_not_queued_twice(self):
        key=self.control.add_profile(self.profile);body={'confirmation':'CONFIGURER '+key[:8]}
        self.control.request_router_setup(key,body)
        with self.assertRaises(domain.Conflict):self.control.request_router_setup(key,body)
    def test_router_setup_endpoint_requires_csrf(self):
        key=self.control.add_profile(self.profile);app=self.app()
        client=app.test_client();r=client.post('/api/login',json={'username':'admin','password':'Testing-admin-password'})
        self.assertEqual(client.post('/api/profiles/'+key+'/setup-router',json={'confirmation':'CONFIGURER '+key[:8]}).status_code,403)
        self.assertEqual(client.post('/api/profiles/'+key+'/setup-router',json={'confirmation':'CONFIGURER '+key[:8]},headers={'X-CSRF-Token':r.json['csrf']}).status_code,202)
    def test_remote_primary_mismatch_refuses_before_changes(self):
        with patch.object(remote,'run',return_value=json.dumps([{'prefsrc':'9.9.9.9','dev':'ens3'}])),patch.object(remote.subprocess,'run') as command:
            with self.assertRaises(ValueError):remote.router_bootstrap({'router':{'primary_ip':'8.8.8.8'}})
            command.assert_not_called()
    def test_remote_unexpected_os_refuses_before_changes(self):
        with patch.object(remote,'run',return_value=json.dumps([{'prefsrc':'8.8.8.8','dev':'ens3'}])),patch.object(remote.Path,'read_text',return_value='ID=debian\nVERSION_ID="12"'),patch.object(remote.subprocess,'run') as command:
            with self.assertRaises(ValueError):remote.router_bootstrap({'router':{'primary_ip':'8.8.8.8'}})
            command.assert_not_called()
    def test_remote_existing_installation_refuses_before_changes(self):
        with patch.object(remote,'run',return_value=json.dumps([{'prefsrc':'8.8.8.8','dev':'ens3'}])),patch.object(remote.Path,'read_text',return_value='ID=debian\nVERSION_ID="13"'),patch.object(remote.Path,'exists',return_value=True),patch.object(remote.subprocess,'run') as command:
            with self.assertRaises(ValueError):remote.router_bootstrap({'router':{'primary_ip':'8.8.8.8'}})
            command.assert_not_called()


class ConnectionOverrideTests(unittest.TestCase):
    setUp=EnvironmentRouterTests.setUp
    tearDown=EnvironmentRouterTests.tearDown
    # Keep this class separate to exercise both declarative and existing-store paths.
    def test_connections_override_json_values(self):
        with patch.dict(os.environ,{'VWAN_PROFILES_JSON':json.dumps([self.profile]),'VWAN_PVE_HOST':'192.168.2.22','VWAN_PVE_PORT':'2222','VWAN_PFSENSE_URL':'https://pf.example.net'}):
            apply_environment(self.control)
        c=self.store.document('profiles','router-test')['config']
        self.assertEqual(c['pve']['host'],'192.168.2.22');self.assertEqual(c['pve']['port'],2222)
        self.assertEqual(c['pfsense']['url'],'https://pf.example.net')
        self.assertEqual(self.profile['config']['pve']['host'],'192.168.1.10')
    def test_connections_override_existing_profile_without_json(self):
        key=self.control.add_profile(self.profile)
        with patch.dict(os.environ,{'VWAN_PROFILES_JSON':'[]','VWAN_PVE_HOST':'192.168.2.22'}):apply_environment(self.control)
        self.assertEqual(self.store.document('profiles',key)['config']['pve']['host'],'192.168.2.22')
    def test_multiple_profiles_require_selector(self):
        other=json.loads(json.dumps(self.profile));other['id']='other-router'
        with patch.dict(os.environ,{'VWAN_PROFILES_JSON':json.dumps([self.profile,other]),'VWAN_PVE_HOST':'192.168.2.22','VWAN_PROFILE_ID':''}):
            with self.assertRaisesRegex(ValueError,'Plusieurs routeurs'):apply_environment(self.control)
    def test_recorded_host_key_is_accepted(self):
        import paramiko
        p=Path(self.tmp.name)/'known';hosts=paramiko.HostKeys()
        hosts.add('192.168.1.10','ssh-rsa',paramiko.RSAKey.generate(1024));hosts.save(str(p))
        require_ssh_host_key({'host':'192.168.1.10','port':22,'known_hosts':str(p)})
    def test_http_firewall_override_rejected(self):
        with patch.dict(os.environ,{'VWAN_PROFILES_JSON':json.dumps([self.profile]),'VWAN_PFSENSE_URL':'http://pf.example.net'}):
            with self.assertRaises(ValueError):apply_environment(self.control)
    def test_port_override_rejected(self):
        with patch.dict(os.environ,{'VWAN_PROFILES_JSON':json.dumps([self.profile]),'VWAN_PVE_PORT':'70000'}):
            with self.assertRaises(ValueError):apply_environment(self.control)
    def test_unknown_profile_selector_rejected(self):
        with patch.dict(os.environ,{'VWAN_PROFILES_JSON':json.dumps([self.profile]),'VWAN_PVE_HOST':'192.168.2.22','VWAN_PROFILE_ID':'missing'}):
            with self.assertRaises(ValueError):apply_environment(self.control)
    def test_missing_host_keys_has_actionable_message(self):
        with self.assertRaisesRegex(ValueError,'Vérifier clé SSH PVE'):
            require_ssh_host_key({'host':'192.168.1.10','port':22,'known_hosts':str(Path(self.tmp.name)/'missing')})
    def test_empty_host_keys_remain_untrusted(self):
        p=Path(self.tmp.name)/'known';p.write_text('')
        with self.assertRaisesRegex(ValueError,'non enregistrée'):
            require_ssh_host_key({'host':'192.168.1.10','port':22,'known_hosts':str(p)})


if __name__=='__main__':unittest.main()
