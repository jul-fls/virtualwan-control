import importlib.util,json,tempfile,unittest,sys
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).parents[1]/'tools'
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,ROOT/file);obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj);return obj
policy=load('policy','public-policy.py');service=load('service','isolation-service.py') if sys.platform!='win32' else None
def config():
 return {'bridge':'vmbr12','uplink':'vlan150','gateway_ip':'10.254.251.1','gateway_mac':'02:00:00:00:00:01','blocked_networks':['10.0.0.0/8'],
 'nodes':{'node-a':{'bridge':'vmbr12','uplink':'vlan150'}},'clients':[{'vmid':120,'mac':'02:00:00:00:00:02','public_ip':'8.8.8.8','next_hop':'10.254.251.11'}]}
class ConntrackTests(unittest.TestCase):
 def test_colliding_vmids_need_explicit_distinct_zones(self):
  c=config();c['clients'].append({**c['clients'][0],'vmid':16504,'mac':'02:00:00:00:00:03','public_ip':'9.9.9.9','next_hop':'10.254.251.12'})
  with self.assertRaisesRegex(ValueError,'zone collision'):policy.validate(c)
  c['clients'][1]['conntrack_zone']=2000;policy.validate(c)
 @unittest.skipIf(sys.platform=='win32','Linux service requires fcntl')
 def test_collision_across_segments_rejected(self):
  a=config();b=config();b.update(bridge='vmbr13',gateway_ip='10.254.252.1');b['nodes']={'node-a':{'bridge':'vmbr13','uplink':'vlan151'}};b['clients']=[{**b['clients'][0],'vmid':121,'mac':'02:00:00:00:00:03','public_ip':'9.9.9.9','next_hop':'10.254.252.11','conntrack_zone':policy.zones(a['clients'][0])[0]}]
  with patch.object(service,'module',return_value=policy):
   with self.assertRaisesRegex(ValueError,'zone collision'):service.segments({'segments':{'a':a,'b':b}})
 def test_no_native_bridge_conntrack(self):
  c=config();c['blocked_networks'].append('82.66.231.166/32');c['administration_public_networks']=['82.66.231.166/32']
  data=policy.generate(c);bridge,inet=data.split('add table inet',1)
  self.assertNotIn('ct state',bridge);self.assertNotIn('ct zone',bridge);self.assertIn('ct direction reply',inet)
 @unittest.skipIf(sys.platform=='win32','Linux service requires fcntl')
 def test_disabled_ip_hooks_cannot_install_unguarded_bridge(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'registry.json';path.write_text(json.dumps(config()))
   def run(*args):return 'node-a\n' if args[0]=='hostname' else '0\n' if args[0]=='sysctl' else ''
   with patch.object(service,'REGISTRY',path),patch.object(service,'module',return_value=policy),patch.object(service,'run',side_effect=run) as called:
    with self.assertRaisesRegex(ValueError,'refusing an unguarded policy'):service.apply()
    self.assertFalse(any(call.args[0]=='nft' for call in called.call_args_list))
if __name__=='__main__':unittest.main()
