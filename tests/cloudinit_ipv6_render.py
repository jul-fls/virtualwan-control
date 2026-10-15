"""Offline cloud-init ENI render check; no interface is activated."""
import json,sys,tempfile,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from virtualwan.ipv6 import ipv6_network_data
c={'enabled':True,'network':'2001:4860:1000::/64','gateway_address':'2001:4860:1000::1','gateway_link_local':'fe80::1','address':'2001:4860:1000::120'}
g={'interface':'ens18','gateway':'10.254.251.1','mtu':1420,'dns':['1.1.1.1']}
with tempfile.TemporaryDirectory() as t:
 p=Path(t);n=p/'network.json';n.write_text(json.dumps(ipv6_network_data(g,'8.8.8.8','02:00:00:00:00:01',c)))
 subprocess.run(['cloud-init','devel','net-convert','--network-data',str(n),'--kind','yaml','--directory',str(p/'out'),'--distro','debian','--output-kind','eni'],check=True,capture_output=True)
 files=list((p/'out').rglob('50-cloud-init*'));assert len(files)==1
 text=files[0].read_text();print(text)
 for value in ['iface ens18 inet static','8.8.8.8/32','pointopoint 10.254.251.1','iface ens18 inet6 static','2001:4860:1000::120/128','gateway fe80::1']:assert value in text,(value,text)
 r=subprocess.run(['ifup','--no-act','--verbose','--force','--interfaces',str(files[0]),'ens18'],check=True,capture_output=True,text=True);assert 'fe80::1' in r.stdout+r.stderr
 print(r.stdout+r.stderr)
 assert 'autoconf' in r.stdout+r.stderr and 'accept_ra' in r.stdout+r.stderr
 print('PASS dual-stack ENI render and ifupdown dry-run')
