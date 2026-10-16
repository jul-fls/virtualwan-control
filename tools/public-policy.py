#!/usr/bin/env python3
"""Public-only bridge policy: IPv4 and optional per-client routed IPv6."""
import ipaddress
import json
import re

"""Opt-in per-client IPv6 bridge policy. IPv4 rules remain byte-for-byte unchanged."""
import ipaddress

def validate6(c):
    clients=[v for v in c['clients'] if v.get('public_ipv6')]
    if not clients:return
    cfg=c['ipv6'];net=ipaddress.IPv6Network(cfg['network']);protected=[ipaddress.IPv6Network(x) for x in cfg['protected_networks']]
    if net.prefixlen!=64 or not net.network_address.is_global:raise ValueError('Expected a public /64')
    gl=ipaddress.IPv6Address(cfg['gateway_link_local']);ga=ipaddress.IPv6Address(cfg['gateway_address'])
    if not gl.is_link_local or ga not in net:raise ValueError('Invalid IPv6 gateway')
    if not any(net.subnet_of(n) for n in protected):raise ValueError('The complete client /64 must be protected, with explicit client exceptions')
    seen={str(ga),str(gl)}
    for v in clients:
        p=ipaddress.IPv6Address(v['public_ipv6']);ll=ipaddress.IPv6Address(v['link_local_ipv6'])
        if p not in net or not p.is_global or not ll.is_link_local:raise ValueError('Invalid client IPv6')
        for address in (str(p),str(ll)):
            if address in seen:raise ValueError('Duplicate IPv6 identity')
            seen.add(address)

def rules6(c,v,ports):
    if not v.get('public_ipv6'):return []
    cfg=c['ipv6'];gm=c['gateway_mac'].lower();m=v['mac'].lower();u=c['uplink']
    p=v['public_ipv6'];ll=v['link_local_ipv6'];gl=cfg['gateway_link_local'];ga=cfg['gateway_address']
    out=f'iifname {ports} oifname "{u}" ether saddr {m}'
    inc=f'iifname "{u}" oifname {ports} ether saddr {gm}'
    own='{ '+p+', '+ll+' }';gateway='{ '+ga+', '+gl+' }'
    def targets(values):return '{ '+', '.join('0x'+ipaddress.IPv6Address(x).packed.hex() for x in values)+' }'
    rows=[]
    def add(expr,verdict='accept'):rows.append('  '+expr+' counter '+verdict)
    # ND without extension headers, hop limit 255; target identity and LLA options pinned.
    ns='ether type ip6 ip6 nexthdr ipv6-icmp ip6 hoplimit 255 icmpv6 type nd-neighbor-solicit'
    na='ether type ip6 ip6 nexthdr ipv6-icmp ip6 hoplimit 255 icmpv6 type nd-neighbor-advert'
    macraw='0x'+m.replace(':','');gmraw='0x'+gm.replace(':','')
    add(f'{out} ether daddr {{ {gm}, 33:33:ff:00:00:01, '+('33:33:ff:'+':'.join(f'{x:02x}' for x in ipaddress.IPv6Address(gl).packed[-3:]))+f' }} {ns} ip6 saddr {own} ip6 daddr {{ {ga}, {gl}, '+str(ipaddress.IPv6Address(int(ipaddress.IPv6Address('ff02::1:ff00:0'))|(int(ipaddress.IPv6Address(ga))&0xffffff)))+', '+str(ipaddress.IPv6Address(int(ipaddress.IPv6Address('ff02::1:ff00:0'))|(int(ipaddress.IPv6Address(gl))&0xffffff)))+f' }} @th,64,128 {targets([ga,gl])} ip6 length 32 @th,192,16 0x0101 @th,208,48 {macraw}')
    add(f'{out} ether daddr {gm} {na} ip6 saddr {own} ip6 daddr {gateway} @th,64,128 {targets([p,ll])} ip6 length 32 @th,192,16 0x0201 @th,208,48 {macraw}')
    # DAD of this client's two identities only; never reaches another VM port.
    add(f'{out} ether daddr {{ {','.join('33:33:ff:'+':'.join(f'{x:02x}' for x in ipaddress.IPv6Address(a).packed[-3:]) for a in [p,ll])} }} {ns} ip6 saddr :: ip6 length 24 @th,64,128 {targets([p,ll])}')
    add(f'{inc} {ns} ip6 saddr {gateway} @th,64,128 {targets([p,ll])} ip6 length 32 @th,192,16 0x0101 @th,208,48 {gmraw}')
    add(f'{inc} ether daddr {m} {na} ip6 saddr {gateway} ip6 daddr {own} @th,64,128 {targets([ga,gl])} ip6 length 32 @th,192,16 0x0201 @th,208,48 {gmraw}')
    # Only ND above is permitted. Reject redirects, RA, RS and all remaining ND.
    add(f'{out} ether type ip6 meta l4proto ipv6-icmp icmpv6 type {{ nd-router-solicit, nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert, nd-redirect }}','drop')
    add(f'{inc} ether type ip6 meta l4proto ipv6-icmp icmpv6 type {{ nd-router-solicit, nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert, nd-redirect }}','drop')
    protected='{ '+', '.join(cfg['protected_networks'])+' }'
    public='{ '+', '.join(x['public_ipv6'] for x in c['clients'] if x.get('public_ipv6'))+' }'
    # Replies to admin connections from the home public prefix remain possible.
    # The inet guard below checks reply state without enabling native bridge CT.
    add(f'{out} ether daddr {gm} ether type ip6 ip6 saddr {p} ip6 daddr 2000::/3')
    add(f'{out} ether daddr {gm} ether type ip6 ip6 saddr {p} ip6 daddr {public}')
    add(f'{out} ether type ip6 ip6 daddr {protected}','drop')
    add(f'{out} ether daddr {gm} ether type ip6 ip6 saddr {p} ip6 daddr 2000::/3')
    add(f'{inc} ether daddr {m} ether type ip6 ip6 saddr {gateway} ip6 daddr {own} meta l4proto ipv6-icmp icmpv6 type {{ destination-unreachable, packet-too-big, time-exceeded, parameter-problem }}')
    add(f'{inc} ether daddr {m} ether type ip6 ip6 saddr 2000::/3 ip6 daddr {p}')
    return rows


TABLE = 'virtualwan_isolation'


def zones(v):
    """Stable, disjoint public-bridge/fwbr zones; collisions fail validation."""
    base=v.get('conntrack_zone',32768+2*(v['vmid']%16384))
    if type(base) is not int or not 2 <= base <= 65534 or base%2:
        raise ValueError('conntrack_zone must be an even integer from 2 to 65534')
    return base,base+1


def inet_policy(c):
    """Use only IP conntrack, with a separate context for every bridge leg."""
    table=c.get('table_name',TABLE);b=c['bridge'];gm=c['gateway_mac'].lower()
    lines=[f'add table inet {table}',f'delete table inet {table}',f'table inet {table} {{',
           ' chain zones { type filter hook prerouting priority -310; policy accept;']
    for v in c['clients']:
        main,fw=zones(v);m=v['mac'].lower()
        lines.append(f'  iifname "fwbr{v["vmid"]}i0" counter ct zone set {fw}')
        for family,key in [('ip','public_ip'),('ip6','public_ipv6')]:
            if not v.get(key):continue
            p=v[key]
            lines.append(f'  iifname "{b}" ether saddr {m} ether daddr {gm} {family} saddr {p} counter ct zone set {main}')
            lines.append(f'  iifname "{b}" ether saddr {gm} ether daddr {m} {family} daddr {p} counter ct zone set {main}')
    lines+=[' }',' chain administration { type filter hook forward priority -10; policy accept;']
    admins=c.get('administration_public_networks',[])
    public6=[v['public_ipv6'] for v in c['clients'] if v.get('public_ipv6')]
    for v in c['clients']:
        interfaces='{ '+', '.join(json.dumps(x) for x in (b,f'fwbr{v["vmid"]}i0'))+' }'
        out=f'iifname {interfaces} ether saddr {v["mac"].lower()} ether daddr {gm}'
        if admins:
            expr=f'{out} ip saddr {v["public_ip"]} ip daddr {{ {", ".join(admins)} }}'
            lines.append(f'  {expr} ct direction reply ct state {{ established, related }} counter accept')
            lines.append(f'  {expr} counter drop')
        if v.get('public_ipv6'):
            expr=f'{out} ip6 saddr {v["public_ipv6"]}'
            lines.append(f'  {expr} ip6 daddr {{ {", ".join(public6)} }} counter accept')
            expr+=f' ip6 daddr {{ {", ".join(c["ipv6"]["protected_networks"])} }} ip6 daddr 2000::/3'
            lines.append(f'  {expr} ct direction reply ct state {{ established, related }} counter accept')
            lines.append(f'  {expr} counter drop')
    lines+=[' }','}']
    return '\n'.join(lines)+'\n'


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
    identities={c['gateway_mac'].lower(),c['gateway_ip']};used_zones=set()
    for v in c['clients']:
        if not isinstance(v['vmid'],int) or not 100 <= v['vmid'] <= 99999999:
            raise ValueError('Invalid VMID')
        pair=zones(v)
        if used_zones.intersection(pair):raise ValueError('Conntrack zone collision; assign distinct even conntrack_zone values')
        used_zones.update(pair)
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
    validate6(c)
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
    # No DHCP or tagged traffic; IPv6 is opt-in per registered client.
    rule('ether type { 0x8100, 0x88a8 }','drop')
    for v in c['clients']:
        ports='{ '+', '.join(json.dumps(x) for x in (f'tap{v["vmid"]}i0',f'fwpr{v["vmid"]}p0'))+' }'
        m,p,n=v['mac'].lower(),v['public_ip'],v['next_hop']
        outgoing=f'iifname {ports} oifname "{u}" ether saddr {m} ether daddr {gm}'
        incoming=f'iifname "{u}" oifname {ports} ether saddr {gm} ether daddr {m}'
        if admins:
            rule(f'{outgoing} ether type ip ip saddr {p} ip daddr @administration_v4')
        rule(f'{outgoing} ether type ip ip daddr @protected_v4','drop')
        rule(f'{outgoing} ether type ip ip saddr {p}')
        rule(f'{incoming} ether type ip ip daddr {p} ip protocol icmp icmp type {{ destination-unreachable, time-exceeded, parameter-problem }} ip saddr {g}')
        rule(f'{incoming} ether type ip ip protocol icmp icmp type redirect','drop')
        if admins:
            rule(f'{incoming} ether type ip ip daddr {p} ip saddr @administration_v4')
        rule(f'{incoming} ether type ip ip saddr @protected_v4','drop')
        rule(f'{incoming} ether type ip ip daddr {p}')
        lines.extend(rules6(c,v,ports))
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
    return '\n'.join(lines)+'\n'+inet_policy(c)


if __name__=='__main__':
    import sys
    print(generate(json.load(open(sys.argv[1]))),end='')
