import copy,importlib.util,io,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import xml.etree.ElementTree as ET
from virtualwan.ipv6 import ipv6_settings,ipv6_address,ipv6_link_local,ipv6_network_data
from virtualwan.adapters.provision import Cloudflare,verify_guest
from virtualwan.adapters.pfsense import PfSense
from virtualwan.adapters import lifecycle,provision
from virtualwan.control import Control
from virtualwan.store import Store
from virtualwan.environment import apply_environment

ROOT=Path(__file__).parents[1]
V6={'enabled':True,'network':'2001:4860:1000::/64','gateway_address':'2001:4860:1000::1','gateway_link_local':'fe80::1','protected_networks':['2001:4860:1000::/60']}
ADDRESS='2001:4860:1000::120';KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test'

def guest_value():
 return {'addresses':[{'ifname':'ens18','addr_info':[{'local':'8.8.8.8'}]}],'routes':[{'dst':'default','gateway':'10.254.251.1'}],'dns':'nameserver 1.1.1.1\nnameserver 9.9.9.9','hostname':'vps-8888.hosting.example.net','egress':'8.8.8.8','ipv6':{'addresses':[{'ifname':'ens18','addr_info':[{'local':ADDRESS,'scope':'global'},{'local':'fe80::1','scope':'link'}]}],'routes':[{'dst':'default','gateway':'fe80::1'}],'egress':ADDRESS}}

class IPv6Helpers(unittest.TestCase):
 def test_disabled_profile_compatible(self):self.assertIsNone(ipv6_settings(None));self.assertIsNone(ipv6_settings({'enabled':False}))
 def test_allocation_stable_and_human_readable(self):self.assertEqual(ipv6_address(V6,120),ADDRESS);self.assertNotEqual(ipv6_address(V6,121),ADDRESS)
 def test_invalid_prefix_and_gateway_rejected(self):
  for key,value in [('network','2001:4860:1000::/56'),('gateway_link_local','2001:4860:1000::2'),('gateway_address','2001:4860:1001::1'),('enabled','false')]:
   with self.subTest(key=key),self.assertRaises(ValueError):ipv6_settings({**V6,key:value})
 def test_link_local_eui64(self):self.assertEqual(ipv6_link_local('bc:24:11:e9:e2:52'),'fe80::be24:11ff:fee9:e252')
 def test_only_public_addresses_in_network_snippet(self):
  g={'interface':'ens18','gateway':'10.254.251.1','mtu':1420,'dns':['1.1.1.1']}
  subnets=ipv6_network_data(g,'8.8.8.8','02:00:00:00:00:01',{**V6,'address':ADDRESS})['config'][0]['subnets']
  self.assertEqual([s['address'] for s in subnets],['8.8.8.8/32',ADDRESS+'/128']);self.assertEqual(subnets[1]['gateway'],'fe80::1')
 def test_verify_rejects_extra_global_ipv6(self):
  c={'public_ip':'8.8.8.8','guest':{'gateway':'10.254.251.1','dns':['1.1.1.1','9.9.9.9']},'ipv6':{**V6,'address':ADDRESS}}
  v=guest_value();verify_guest(v,c,v['hostname'],Mock());v['ipv6']['addresses'][0]['addr_info'].append({'local':'2001:4860:1000::999','scope':'global'})
  with self.assertRaises(ValueError):verify_guest(v,c,v['hostname'],Mock())
 def test_standalone_remote_payload_compiles(self):compile((ROOT/'virtualwan/ipv6.py').read_bytes()+b'\n'+(ROOT/'virtualwan/adapters/remote.py').read_bytes(),'payload.py','exec')

class IPv6DNS(unittest.TestCase):
 def cf(self,records):
  c=Cloudflare.__new__(Cloudflare);c.call=Mock(return_value=records);return c
 def test_ipv4_only_refuses_existing_aaaa(self):
  c=self.cf([{'id':'a','name':'vm.example.net','type':'A','content':'8.8.8.8'},{'id':'b','name':'vm.example.net','type':'AAAA','content':ADDRESS}])
  with self.assertRaises(ValueError):c.inspect('vm.example.net','8.8.8.8')
  self.assertEqual(len(c.inspect('vm.example.net','8.8.8.8',ADDRESS)),1)
 def test_aaaa_exact_filter_is_checked_locally(self):
  c=self.cf([{'id':'a','name':'vm.example.net','type':'AAAA','content':ADDRESS},{'id':'b','name':'other.example.net','type':'AAAA','content':'2001:4860:1000::999'}]);self.assertEqual(len(c.records_for_ip(ADDRESS)),1);self.assertEqual(c.call.call_args.kwargs['params']['type'],'AAAA')
 def test_foreign_aaaa_prevents_deletion(self):
  c=self.cf([{'id':'b','name':'foreign.example.net','type':'AAAA','content':ADDRESS,'comment':'Virtualwan managed'}])
  with self.assertRaises(ValueError):c.retire_ipv6('vm.example.net',ADDRESS)
  self.assertEqual(c.call.call_args.args[0],'GET')
 def test_ipv6_cleanup_is_idempotent(self):
  c=self.cf([]);c.retire_ipv6('vm.example.net',ADDRESS);self.assertTrue(all(x.args[0]=='GET' for x in c.call.call_args_list))

class IPv6Control(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Store(self.tmp.name);self.control=Control(self.store,demo=True)
  self.p=json.loads((ROOT/'examples/profile.json').read_text());self.p.update(ready=True);self.p['config']['ipv6']=copy.deepcopy(V6);self.profile=self.control.add_profile(self.p)
  self.model=self.control.add_model({'name':'Small','cores':1,'memory_mb':1024,'disk_gb':16})
 def deploy(self):
  self.control.add_ip(self.profile,'8.8.8.8');j=self.control.request_deploy({'profile_id':self.profile,'model_id':self.model,'ssh_auth':'key','public_key':KEY});self.control.process_one();self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(j,))['state'],'succeeded');return self.control.deployments()[0]
 def test_creation_exposes_ipv6_inventory(self):
  d=self.deploy();self.assertEqual(d['public_ipv6'],ipv6_address(V6,d['vmid']));self.assertEqual(d['link_local_ipv6'],ipv6_link_local(d['mac']))
 def test_release_detaches_both_addresses_and_preserves_history(self):
  d=self.deploy();j=self.control.request_retire(d['id'],{'mode':'release','confirmation':'LIBERER'});self.control.process_one();kept=self.control.deployments()[0]
  self.assertIsNone(kept['public_ipv6']);self.assertIsNone(kept['ip']);self.assertEqual(kept['last_attachment']['public_ipv6'],d['public_ipv6']);self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(j,))['state'],'succeeded')
 def test_delete_only_removes_ipv6_and_holds_ipv4(self):
  d=self.deploy();self.control.request_retire(d['id'],{'mode':'delete','confirmation':'SUPPRIMER'});self.control.process_one();self.assertFalse(self.control.deployments());self.assertEqual(self.store.one('SELECT state FROM ip_pool')['state'],'reserved')
 def test_live_prefix_cannot_change(self):
  self.deploy();p=copy.deepcopy(self.p);p['config']['ipv6']['gateway_link_local']='fe80::2'
  with self.assertRaises(ValueError):self.control.add_profile(p,self.profile)
 def test_clone_does_not_reuse_ipv6_prefix(self):
  key=self.control.clone_profile(self.profile,'Second','10.254.252.0/24','10.254.252.1');self.assertNotIn('ipv6',self.store.document('profiles',key)['config'])
 def test_env_overrides_configure_ipv6(self):
  with patch.dict(os.environ,{'VWAN_PROFILE_ID':self.profile,'VWAN_IPV6_ENABLED':'true','VWAN_IPV6_NETWORK':V6['network'],'VWAN_IPV6_GATEWAY_ADDRESS':V6['gateway_address'],'VWAN_IPV6_GATEWAY_LINK_LOCAL':'fe80::2','VWAN_IPV6_PROTECTED_NETWORKS_JSON':json.dumps(V6['protected_networks'])}):apply_environment(self.control)
  self.assertEqual(self.store.document('profiles',self.profile)['config']['ipv6']['gateway_link_local'],'fe80::2')

class IPv6Lifecycle(unittest.TestCase):
 def execute(self,mode,foreign=False):
  with tempfile.TemporaryDirectory() as t:
   cfg=json.loads((ROOT/'examples/profile.json').read_text())['config'];cfg.update(public_ip='8.8.8.8',ipv6=V6,lifecycle_mode=mode,retire_client={'vmid':120,'mac':'02:00:00:00:00:01','public_ip':'8.8.8.8','next_hop':'10.254.251.11','fqdn':'vps-8888.hosting.example.net','public_ipv6':ADDRESS,'ipv6_config':V6})
   path=Path(t)/'lifecycle.json';path.write_text(json.dumps(cfg));pve=Mock();pve.call.return_value={'exists':True};vps=Mock();cf=Mock();cf.records_for_ip.return_value=[];cf.inspect.return_value=[];pf=Mock()
   if foreign:cf.retire_ipv6.side_effect=ValueError('foreign AAAA')
   with patch.object(sys,'argv',['lifecycle',str(path)]),patch.object(lifecycle,'Journal'),patch.object(lifecycle,'Remote',side_effect=[pve,vps]),patch.object(lifecycle,'Cloudflare',return_value=cf),patch.object(lifecycle,'Ovh'),patch.object(lifecycle,'PfSense',return_value=pf),patch.object(lifecycle,'wait_dns'):
    if foreign:
     with self.assertRaises(ValueError):lifecycle.main()
    else:lifecycle.main()
   return pve,pf,cf
 def test_all_retirement_modes_clean_ipv6(self):
  for mode in ['release','delete','delete_release']:
   with self.subTest(mode=mode):
    pve,pf,cf=self.execute(mode);self.assertEqual(cf.retire_ipv6.call_count,2);self.assertEqual(pf.retire_ipv6.call_count,2);self.assertTrue(any(x.args[0]=='pve_retire_unregister' for x in pve.call.call_args_list))
 def test_foreign_dns_stops_before_vm_stop(self):
  pve,pf,cf=self.execute('delete',True);self.assertEqual([x.args[0] for x in pve.call.call_args_list],['pve_retire_check']);pf.retire_ipv6.assert_not_called()



class IPv6PfSense(unittest.TestCase):
 def setUp(self):
  self.root=ET.fromstring('<pfsense><interfaces><wan/><optpublic><ipaddrv6>2001:4860:1000::1</ipaddrv6><subnetv6>64</subnetv6></optpublic></interfaces><aliases/><filter/></pfsense>')
  self.pf=PfSense.__new__(PfSense);self.pf.cfg={'public_interface':'optpublic'};self.pf.log=Mock();self.pf.snapshot=Mock(side_effect=lambda label:copy.deepcopy(self.root));self.pf.apply=Mock();self.pf.request=Mock(side_effect=self.request);self.pf.form=Mock(side_effect=self.form);self.pf.native_post=Mock(side_effect=self.delete)
 def request(self,method,path,**kw):
  aliases=self.root.find('aliases')
  if method=='GET':
   from urllib.parse import parse_qs,urlsplit
   query=parse_qs(urlsplit(path).query);a=aliases[int(query['id'][0])] if 'id' in query else None
   data={'__csrf_magic':'test','name':a.findtext('name') if a is not None else '', 'type':a.findtext('type') if a is not None else 'host','address0':''}
   if a is not None:
    for i,x in enumerate(a.findtext('address','').split()):
     if a.findtext('type')=='network':net=__import__('ipaddress').IPv6Network(x);data.update({f'address{i}':str(net.network_address),f'address_subnet{i}':str(net.prefixlen)})
     else:data[f'address{i}']=x;data[f'address_subnet{i}']='128'
   text='<form>'+''.join('<input name="'+k+'" value="'+v+'">' for k,v in data.items())+'</form>';return Mock(text=text)
  data=kw['data'];a=next((a for a in aliases if a.findtext('name')==data['name']),None)
  if a is None:a=ET.SubElement(aliases,'alias')
  a.clear()
  for key in ['name','type','descr']:ET.SubElement(a,key).text=data[key]
  values=[]
  for key,x in data.items():
   if __import__('re').fullmatch(r'address\d+',key) and x:values.append(x+'/'+data['address_subnet'+key[7:]] if data['type']=='network' else x)
  ET.SubElement(a,'address').text=' '.join(values);return Mock(text='<p>Saved</p>')
 def form(self,path,data,required):
  r=ET.Element('rule')
  for a,b in [('type','type'),('interface','interface'),('ipprotocol','ipprotocol'),('descr','descr')]:ET.SubElement(r,a).text=data[b]
  source=ET.SubElement(r,'source');ET.SubElement(source,'address' if data['srctype']=='single' else 'any').text=data.get('src') if data['srctype']=='single' else None
  dest=ET.SubElement(r,'destination');ET.SubElement(dest,'network' if data['dsttype']=='(self)' else 'address').text=data['dst']+('/'+data['dstmask'] if data['dsttype']=='network' else '')
  self.root.find('filter').insert(0,r)
 def delete(self,page,data):
  if page.startswith('diag_dump_states'):return
  parent=self.root.find('filter' if page.startswith('firewall_rules') else 'aliases');parent.remove(list(parent)[int(data['id'])])
 def configure(self,address=ADDRESS):self.pf.configure_ipv6(copy.deepcopy(self.root),V6,address)
 def test_configure_twice_does_not_duplicate_rules(self):
  self.configure();self.configure();self.assertEqual(len(self.root.findall('filter/rule')),5);self.assertEqual(len(self.root.findall('aliases/alias')),2)
 def test_release_one_preserves_other_client(self):
  self.configure();other='2001:4860:1000::121';self.configure(other);self.pf.retire_ipv6(V6,ADDRESS)
  _,a=self.pf._v6_alias(self.root,'VWAN_CLIENTS_V6');self.assertEqual(self.pf._v6_values(a),[other]);self.assertEqual(len(self.root.findall('filter/rule')),5)
 def test_last_release_removes_rules_and_client_alias_retry_safe(self):
  self.configure();self.pf.retire_ipv6(V6,ADDRESS);self.pf.retire_ipv6(V6,ADDRESS)
  self.assertEqual(len(self.root.findall('filter/rule')),0);self.assertIsNone(self.pf._v6_alias(self.root,'VWAN_CLIENTS_V6')[1]);self.assertIsNotNone(self.pf._v6_alias(self.root,'VWAN_PROTECTED_V6')[1])
 def test_changed_gateway_blocks_cleanup_before_write(self):
  self.configure();ET.SubElement(self.root.find('filter/rule'),'gateway').text='FOREIGN';self.pf.native_post.reset_mock()
  with self.assertRaises(ValueError):self.pf.retire_ipv6(V6,ADDRESS,check_only=True)
  self.pf.native_post.assert_not_called()
 def test_foreign_alias_reference_blocks_cleanup_before_write(self):
  self.configure();alias=ET.SubElement(self.root.find('aliases'),'alias');ET.SubElement(alias,'name').text='OTHER';ET.SubElement(alias,'address').text='VWAN_CLIENTS_V6'
  with self.assertRaises(ValueError):self.pf.retire_ipv6(V6,ADDRESS,check_only=True)
  self.pf.native_post.assert_not_called()
 def test_lan_prefix_mismatch_rejected(self):
  self.root.find('interfaces/optpublic/subnetv6').text='56'
  with self.assertRaises(ValueError):self.configure()
  self.pf.request.assert_not_called()

class IPv6Orchestration(unittest.TestCase):
 def test_aaaa_is_only_published_after_guest_verification(self):
  with tempfile.TemporaryDirectory() as t:
   c=json.loads((ROOT/'examples/profile.json').read_text())['config'];c.update(public_ip='8.8.8.8',ipv6=copy.deepcopy(V6),reboot_test=False);c['pve']['vmid']=120
   key=Path(t)/'key.pub';key.write_text(KEY);c['guest']['ssh_public_key_file']=str(key)
   pf=Mock();pf.cfg={'next_hop':'10.254.251.11'};cf=Mock();vps=Mock();vps.call.return_value={};pve=Mock();order=[]
   def call(action,**kw):
    if action=='pve_inspect':return {'resources':[],'isolation':{'active':True}}
    if action=='pve_ipv6_allocate':return {'public_ipv6':ADDRESS,'source':'vmid'}
    if action=='pve_create':return {'mac':'02:00:00:00:00:01'}
    if action=='pve_verify':order.append('verified');return guest_value()
    return {}
   pve.call.side_effect=call;cf.ensure_ipv6.side_effect=lambda *a:order.append('published')
   connection=Mock();connection.__enter__=Mock(return_value=Mock(recv=Mock(return_value=b'SSH-test')));connection.__exit__=Mock(return_value=False)
   with patch.object(provision,'PfSense',return_value=pf),patch.object(provision,'Cloudflare',return_value=cf),patch.object(provision,'Ovh'),patch.object(provision,'Remote',side_effect=[vps,pve]),patch.object(provision,'Fernet'),patch.object(provision,'secret',return_value='test'),patch.object(provision,'wait_dns'),patch.object(provision.socket,'create_connection',return_value=connection):
    provision.orchestrate(c,'apply',Mock())
   self.assertEqual(order,['verified','published']);self.assertEqual(c['ipv6']['address'],ADDRESS)


 def test_failed_ipv6_verification_never_publishes_aaaa(self):
  with tempfile.TemporaryDirectory() as t:
   c=json.loads((ROOT/'examples/profile.json').read_text())['config'];c.update(public_ip='8.8.8.8',ipv6=copy.deepcopy(V6),reboot_test=False);c['pve']['vmid']=120
   key=Path(t)/'key.pub';key.write_text(KEY);c['guest']['ssh_public_key_file']=str(key)
   pf=Mock();pf.cfg={'next_hop':'10.254.251.11'};cf=Mock();vps=Mock();vps.call.return_value={};pve=Mock();order=[]
   def call(action,**kw):
    if action=='pve_inspect':return {'resources':[],'isolation':{'active':True}}
    if action=='pve_ipv6_allocate':return {'public_ipv6':ADDRESS,'source':'vmid'}
    if action=='pve_create':return {'mac':'02:00:00:00:00:01'}
    if action=='pve_verify':order.append('verified');return {**guest_value(),'ipv6':{}}
    return {}
   pve.call.side_effect=call;cf.ensure_ipv6.side_effect=lambda *a:order.append('published')
   connection=Mock();connection.__enter__=Mock(return_value=Mock(recv=Mock(return_value=b'SSH-test')));connection.__exit__=Mock(return_value=False)
   with patch.object(provision,'PfSense',return_value=pf),patch.object(provision,'Cloudflare',return_value=cf),patch.object(provision,'Ovh'),patch.object(provision,'Remote',side_effect=[vps,pve]),patch.object(provision,'Fernet'),patch.object(provision,'secret',return_value='test'),patch.object(provision,'wait_dns'),patch.object(provision.socket,'create_connection',return_value=connection):
    with self.assertRaises(ValueError),patch.object(provision.time,'monotonic',side_effect=[0,999]):provision.orchestrate(c,'apply',Mock())
   self.assertEqual(order,['verified']);cf.ensure_ipv6.assert_not_called()



class IPv6Registry(unittest.TestCase):
 def allocate(self,clients,config=None):
  from virtualwan.adapters import remote
  import builtins
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);registry=root/'registry.json';registry.write_text(json.dumps({'clients':clients,'nodes':{}}));realopen=builtins.open
   def mapped(path,*a,**kw):return realopen(root/'lock' if path=='/run/virtualwan-isolation.lock' else path,*a,**kw)
   with patch.object(remote,'Path',return_value=registry),patch('builtins.open',side_effect=mapped),patch.object(remote,'emit') as emit:
    remote.pve_ipv6_allocate({'pve':{'vmid':120},'ipv6':config or V6,'ip':'8.8.8.8'});return emit.call_args.args[0]
 def test_existing_manual_address_preserved(self):
  result=self.allocate([{'vmid':120,'public_ip':'8.8.8.8','public_ipv6':'2001:4860:1000::abc'}]);self.assertEqual(result['public_ipv6'],'2001:4860:1000::abc');self.assertEqual(result['source'],'registry')
 def test_collision_is_rejected(self):
  with self.assertRaises(ValueError):self.allocate([{'vmid':999,'public_ip':'9.9.9.9','public_ipv6':ADDRESS}])
 def test_changed_saved_address_is_rejected(self):
  with self.assertRaises(ValueError):self.allocate([{'vmid':120,'public_ip':'8.8.8.8','public_ipv6':'2001:4860:1000::abc'}],{**V6,'address':ADDRESS})

if __name__=='__main__':unittest.main()
