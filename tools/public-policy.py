#!/usr/bin/env python3
"""Strict public-only bridge policy: all IPv4 client exchanges use the router MAC."""
import ipaddress
import json
import re

TABLE = 'virtualwan_isolation'


def validate(c):
    if not re.fullmatch(r'[a-z][a-z0-9_]{1,62}',c.get('table_name',TABLE)):
        raise ValueError('Invalid isolation table name')
    for key in ('bridge', 'uplink'):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', c[key]):
            raise ValueError('Invalid bridge or uplink')
    if c['bridge'] == c['uplink']:
        raise ValueError('Bridge cannot equal uplink')
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',c['gateway_mac'].lower()):
        raise ValueError('Invalid router MAC')
    if int(c['gateway_mac'][:2],16)&1 or c['gateway_mac']=='00:00:00:00:00:00':
        raise ValueError('Router MAC must be unicast')
    gateway=ipaddress.IPv4Address(c['gateway_ip'])
    transit=ipaddress.IPv4Network(c.get('gateway_network',str(gateway)+'/24'),strict=False)
    if gateway not in transit:raise ValueError('Gateway outside client transit network')
    protected=[ipaddress.IPv4Network(x) for x in c['blocked_networks']]
    for value in c.get('administration_public_networks', []):
        subnet=ipaddress.IPv4Network(value)
        if not subnet.network_address.is_global or not subnet.broadcast_address.is_global:
            raise ValueError('Administration exceptions must contain public Internet sources only')
        if not any(subnet.subnet_of(n) for n in protected):
            raise ValueError('Administration exception must be a protected public endpoint')
    identities={c['gateway_mac'].lower(),c['gateway_ip']}
    for v in c['clients']:
        if not isinstance(v['vmid'],int) or not 100 <= v['vmid'] <= 99999999:
            raise ValueError('Invalid VMID')
        if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',v['mac'].lower()) or int(v['mac'][:2],16)&1:
            raise ValueError('Invalid MAC')
        if not ipaddress.IPv4Address(v['public_ip']).is_global:
            raise ValueError('Expected public IPv4')
        if any(ipaddress.IPv4Address(v['public_ip']) in n for n in protected):
            raise ValueError('Client public IP overlaps protected infrastructure')
        hop=ipaddress.IPv4Address(v['next_hop'])
        if hop not in transit or hop in (transit.network_address,transit.broadcast_address):
            raise ValueError('Invalid next-hop identity')
        for identity in (str(v['vmid']),v['mac'].lower(),v['public_ip'],v['next_hop']):
            if identity in identities:raise ValueError('Duplicate identity')
            identities.add(identity)
    for subnet in c['blocked_networks']:
        ipaddress.IPv4Network(subnet)
    return c


def generate(c):
    validate(c)
    b,u,gm,g=c['bridge'],c['uplink'],c['gateway_mac'].lower(),c['gateway_ip']
    broadcast='ff:ff:ff:ff:ff:ff'
    table=c.get('table_name',TABLE)
    lines=[f'add table bridge {table}',f'delete table bridge {table}',f'table bridge {table} {{']
    nets=', '.join(c['blocked_networks'])
    lines += [' set protected_v4 { type ipv4_addr; flags interval; auto-merge;',f'  elements = {{ {nets} }}',' }']
    admins=c.get('administration_public_networks', [])
    if admins:
        lines += [' set administration_v4 { type ipv4_addr; flags interval; auto-merge;',f'  elements = {{ {", ".join(admins)} }}',' }']
    def chain(name,hook,priority=-200):
        lines.extend([f' chain {name} {{',f'  type filter hook {hook} priority {priority}; policy accept;'])
    def rule(text,verdict='accept'):
        lines.append(f'  {text} counter {verdict}')
    chain('source_guard','prerouting',-300)
    lines.append(f'  meta ibrname != "{b}" return')
    rule(f'iifname "{u}"')
    for v in c['clients']:
        for port in (f'tap{v["vmid"]}i0',f'fwpr{v["vmid"]}p0'):
            rule(f'iifname "{port}" ether saddr {v["mac"].lower()}')
    rule('','drop');lines.append(' }')
    # Also cover a legacy fwbr if the per-NIC firewall is enabled in the future.
    bridges='{ '+', '.join(json.dumps(x) for x in [b]+[f'fwbr{v["vmid"]}i0' for v in c['clients']])+' }'
    chain('host_input','input');rule(f'meta ibrname {bridges}','drop');lines.append(' }')
    chain('host_output','output');rule(f'meta obrname {bridges}','drop');lines.append(' }')
    chain('clients','forward',0)
    lines.append(f'  meta ibrname != "{b}" return')
    # No DHCP, no IPv6 and no tagged traffic in this public-only template.
    rule('ether type { 0x8100, 0x88a8 }','drop')
    for v in c['clients']:
        ports='{ '+', '.join(json.dumps(x) for x in (f'tap{v["vmid"]}i0',f'fwpr{v["vmid"]}p0'))+' }'
        m,p,n=v['mac'].lower(),v['public_ip'],v['next_hop']
        outgoing=f'iifname {ports} oifname "{u}" ether saddr {m} ether daddr {gm}'
        incoming=f'iifname "{u}" oifname {ports} ether saddr {gm} ether daddr {m}'
        if admins:
            rule(f'{outgoing} ether type ip ip saddr {p} ip daddr @administration_v4 ct direction reply ct state {{ established, related }}')
        rule(f'{outgoing} ether type ip ip daddr @protected_v4','drop')
        rule(f'{outgoing} ether type ip ip saddr {p}')
        rule(f'{incoming} ether type ip ip daddr {p} ip protocol icmp icmp type {{ destination-unreachable, time-exceeded, parameter-problem }} ip saddr {g}')
        rule(f'{incoming} ether type ip ip protocol icmp icmp type redirect','drop')
        if admins:
            rule(f'{incoming} ether type ip ip daddr {p} ip saddr @administration_v4')
        rule(f'{incoming} ether type ip ip saddr @protected_v4','drop')
        rule(f'{incoming} ether type ip ip daddr {p}')
        arp='ether type arp arp htype 1 arp ptype ip arp hlen 6 arp plen 4'
        aout=f'iifname {ports} oifname "{u}" ether saddr {m}'
        ain=f'iifname "{u}" oifname {ports} ether saddr {gm}'
        rule(f'{aout} ether daddr {{ {gm}, {broadcast} }} {arp} arp saddr ether {m} arp operation request arp saddr ip {{ 0.0.0.0, {p} }} arp daddr ip {{ {g}, {p} }}')
        rule(f'{aout} ether daddr {gm} {arp} arp saddr ether {m} arp operation reply arp saddr ip {p} arp daddr ip {g} arp daddr ether {gm}')
        rule(f'{aout} ether daddr {broadcast} {arp} arp saddr ether {m} arp operation reply arp saddr ip {p} arp daddr ip {p}')
        rule(f'{ain} ether daddr {{ {m}, {broadcast} }} {arp} arp saddr ether {gm} arp operation request arp saddr ip {g} arp daddr ip {{ {g}, {p}, {n} }}')
        rule(f'{ain} ether daddr {m} {arp} arp saddr ether {gm} arp operation reply arp saddr ip {g} arp daddr ip {p} arp daddr ether {m}')
        rule(f'{ain} ether daddr {broadcast} {arp} arp saddr ether {gm} arp operation reply arp saddr ip {g} arp daddr ip {g}')
    rule('','drop');lines+=[' }','}']
    return '\n'.join(lines)+'\n'


if __name__=='__main__':
    import sys
    print(generate(json.load(open(sys.argv[1]))),end='')
