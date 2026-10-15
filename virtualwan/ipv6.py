"""Stdlib helpers also prepended to the standalone PVE adapter."""
from copy import deepcopy
import ipaddress
import re


def ipv6_settings(value):
    if value is None:return None
    if not isinstance(value,dict):raise ValueError('ipv6 must be an object')
    if not isinstance(value.get('enabled',False),bool):raise ValueError('ipv6.enabled must be boolean')
    if not value.get('enabled'):return None
    c=deepcopy(value);network=ipaddress.IPv6Network(c['network'])
    if network.prefixlen!=64 or not network.subnet_of(ipaddress.IPv6Network('2000::/3')) or not network.network_address.is_global:raise ValueError('IPv6 requires a public routed /64')
    gateway=ipaddress.IPv6Address(c['gateway_address']);link=ipaddress.IPv6Address(c['gateway_link_local'])
    if gateway not in network or '%' in str(link) or not link.is_link_local:raise ValueError('Invalid IPv6 gateways')
    c.update(network=str(network),gateway_address=str(gateway),gateway_link_local=str(link))
    protected=[ipaddress.IPv6Network(x) for x in c.get('protected_networks',[])]+[network,ipaddress.IPv6Network('fc00::/7'),ipaddress.IPv6Network('fe80::/10'),ipaddress.IPv6Network('ff00::/8'),ipaddress.IPv6Network('::/127')]
    c['protected_networks']=[str(x) for x in ipaddress.collapse_addresses(protected)]
    for key,default in [('public_alias','VWAN_CLIENTS_V6'),('protected_alias','VWAN_PROTECTED_V6')]:
        c.setdefault(key,default)
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}',c[key]):raise ValueError('Invalid IPv6 pfSense alias')
    if c['public_alias']==c['protected_alias']:raise ValueError('IPv6 aliases must be distinct')
    c.setdefault('wan_interface','wan')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}',c['wan_interface']):raise ValueError('Invalid IPv6 WAN interface')
    return c


def ipv6_address(config,vmid):
    c=ipv6_settings(config)
    if not c:return None
    if not 100<=int(vmid)<=999999999:raise ValueError('Invalid IPv6 VMID')
    a=ipaddress.IPv6Address(int(ipaddress.IPv6Network(c['network']).network_address)+int(str(vmid),16))
    if str(a)==c['gateway_address']:raise ValueError('IPv6 collides with gateway')
    return str(a)


def ipv6_link_local(mac):
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',mac):raise ValueError('Invalid MAC')
    raw=bytearray.fromhex(mac.replace(':',''));raw[0]^=2
    return str(ipaddress.IPv6Address(int(ipaddress.IPv6Address('fe80::'))+int.from_bytes(raw[:3]+b'\xff\xfe'+raw[3:],'big')))


def ipv6_validate_address(config,address):
    c=ipv6_settings(config)
    if not c:raise ValueError('IPv6 profile required for this allocation')
    a=ipaddress.IPv6Address(address)
    if a not in ipaddress.IPv6Network(c['network']) or str(a)==c['gateway_address'] or not a.is_global:raise ValueError('IPv6 client outside managed prefix')
    return str(a)


def ipv6_network_data(g,ipv4,mac,config=None):
    subnets=[{'type':'static','address':ipv4+'/32','pointopoint':g['gateway'],'gateway':g['gateway'],'dns_nameservers':g['dns']}]
    if config:
        c=ipv6_settings(config);a=ipv6_validate_address(c,c['address'])
        subnets.append({'type':'static6','address':a+'/128','gateway':c['gateway_link_local'],'accept-ra':0,'autoconf':0})
    return {'version':1,'config':[{'type':'physical','name':g['interface'],'mac_address':mac.lower(),'mtu':g['mtu'],'subnets':subnets}]}


def ipv6_cloud_files(interface):
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}',interface):raise ValueError('Invalid guest interface')
    return [{'path':'/etc/sysctl.d/70-virtualwan-ipv6.conf','owner':'root:root','permissions':'0644','content':''.join(f'net.ipv6.conf.{interface}.{k}=0\n' for k in ['accept_ra','autoconf','use_tempaddr'])}]
