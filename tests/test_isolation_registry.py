import copy
import importlib.util
from pathlib import Path
import unittest
import sys
from unittest.mock import patch

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parents[1]/'tools'/file)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

policy=load('policy','public-policy.py')
service=load('service','isolation-service.py') if sys.platform != 'win32' else None

def segment(bridge='vmbr12',gateway='10.254.251.1'):
    return {'bridge':bridge,'uplink':'vlan150','gateway_ip':gateway,'gateway_mac':'02:00:00:00:00:01',
            'blocked_networks':['10.0.0.0/8','192.168.0.0/16'],'administration_public_networks':[],
            'nodes':{'node-a':{'bridge':bridge,'uplink':'vlan150','address':'192.168.1.10'}},'clients':[]}

@unittest.skipIf(sys.platform == 'win32', 'Linux isolation service requires fcntl; exercised in Linux CI/container')
class RegistryTests(unittest.TestCase):
    def setUp(self):self.loader=patch.object(service,'module',return_value=policy);self.loader.start()
    def tearDown(self):self.loader.stop()
    def test_legacy_table_and_new_table_are_distinct(self):
        self.assertEqual(service.table_name('legacy'),'virtualwan_isolation')
        self.assertNotEqual(service.table_name('east'),service.table_name('west'))
    def test_same_bridge_cannot_host_two_segments(self):
        with self.assertRaises(ValueError):service.segments({'segments':{'east':segment(),'west':segment()}})
    def test_distinct_bridges_are_supported(self):
        result=service.segments({'segments':{'east':segment(),'west':segment('vmbr13','10.254.252.1')}})
        self.assertEqual(set(result),{'east','west'})
    def test_vm_identity_cannot_exist_on_two_segments(self):
        first=segment();second=segment('vmbr13','10.254.252.1')
        first['clients']=[{'vmid':120,'mac':'02:00:00:00:00:02','public_ip':'8.8.8.8','next_hop':'10.254.251.11'}]
        second['clients']=[{'vmid':120,'mac':'02:00:00:00:00:03','public_ip':'9.9.9.9','next_hop':'10.254.252.11'}]
        with self.assertRaises(ValueError):service.segments({'segments':{'east':first,'west':second}})
    def test_configured_transit_mask_is_honored(self):
        config=segment();config['gateway_network']='10.254.251.0/27';config['clients']=[{'vmid':120,'mac':'02:00:00:00:00:02','public_ip':'8.8.8.8','next_hop':'10.254.251.100'}]
        with self.assertRaises(ValueError):policy.validate(config)
    def test_config_cannot_inject_table_commands(self):
        config=segment();config['table_name']='evil; flush ruleset'
        with self.assertRaises(ValueError):policy.generate(config)

if __name__=='__main__':unittest.main()
