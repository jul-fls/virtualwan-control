import importlib.util,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('rescue_network',Path(__file__).parents[1]/'rescue/network.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class RescueNetwork(unittest.TestCase):
    def network(self):return {'version':1,'config':[{'type':'physical','name':'ens18','mac_address':'52:54:00:12:34:56','mtu':1420,'subnets':[{'type':'static','address':'198.51.100.23/32','gateway':'10.254.251.1','dns_nameservers':['1.1.1.1']},{'type':'static6','address':'2001:db8::23/128','gateway':'fe80::1'}]}]}
    def test_dual_stack_mac_match_survives_live_interface_rename(self):
        plan=module.plan(self.network(),{'52:54:00:12:34:56':'eth0'})
        self.assertEqual(plan['interface'],'eth0');self.assertEqual(plan['ipv4'],'198.51.100.23/32');self.assertEqual(plan['gateway6'],'fe80::1');self.assertEqual(plan['dns'],['1.1.1.1'])
    def test_missing_mac_unknown_layout_and_dhcp_fail_closed(self):
        with self.assertRaises(ValueError):module.plan(self.network(),{})
        data=self.network();data['version']=2
        with self.assertRaises(ValueError):module.plan(data,{'52:54:00:12:34:56':'eth0'})
        data=self.network();data['config'][0]['subnets'][0]['type']='dhcp'
        with self.assertRaises(ValueError):module.plan(data,{'52:54:00:12:34:56':'eth0'})
    def test_userdata_only_yields_plain_authorized_keys(self):
        key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA synthetic'
        user={'runcmd':['touch /should-never-exist'],'users':[{'name':'root','sudo':['ALL=(ALL) ALL'],'ssh_authorized_keys':[key,'command="danger" '+key,key+'\nextra']}]}
        self.assertEqual(module.authorized_keys(user),[key])
    def test_rescue_hostname_keeps_domain_and_validates_dns_labels(self):
        self.assertEqual(module.rescue_hostname({'fqdn':'vps-141951093.hosting.flusin.fr'}),'vps-141951093-rescue.hosting.flusin.fr')
        self.assertEqual(module.rescue_hostname({'hostname':'Other.EXAMPLE.NET.'}),'other-rescue.example.net')
        self.assertEqual(module.rescue_hostname({'hostname':'vps-1'}),'vps-1-rescue')
        self.assertIsNone(module.rescue_hostname({}))
        for value in ('bad;reboot.example','x\ny.example','-bad.example','a..example','x'*57+'.example',123):
            with self.assertRaises(ValueError):module.rescue_hostname({'fqdn':value})
    def test_invalid_routes_or_dns_never_reach_commands(self):
        data=self.network();data['config'][0]['subnets'][1]['gateway']='2001:db8::1'
        with self.assertRaises(ValueError):module.plan(data,{'52:54:00:12:34:56':'eth0'})
        data=self.network();data['config'][0]['subnets'][0]['dns_nameservers']=['1.1.1.1; reboot']
        with self.assertRaises(ValueError):module.plan(data,{'52:54:00:12:34:56':'eth0'})

class RescueCredentials(unittest.TestCase):
    def test_hash_and_mac_binding_are_strict(self):
        spec=importlib.util.spec_from_file_location('rescue_auth',Path(__file__).parents[1]/'rescue/auth.py');auth=importlib.util.module_from_spec(spec);spec.loader.exec_module(auth)
        data={'version':1,'session':'a'*32,'mac':'52:54:00:12:34:56','password_hash':'$6$synthetic$'+('a'*86)}
        self.assertEqual(auth.validate(data,{'52:54:00:12:34:56'}),data['password_hash'])
        with self.assertRaises(ValueError):auth.validate(data,{'52:54:00:65:43:21'})
        for value in ['plaintext','!','$6$synthetic$short','x\nrescue:injection']:
            with self.assertRaises(ValueError):auth.validate({**data,'password_hash':value},{'52:54:00:12:34:56'})
