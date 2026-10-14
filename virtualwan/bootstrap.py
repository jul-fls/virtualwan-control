"""Generate reviewable onboarding artifacts. Never applies a guessed physical topology."""
import io
import json
from pathlib import Path
import zipfile

def bundle(profile,ips):
    cfg=profile['config'];router=cfg.get('router',{})
    root=Path(__file__).parent.parent
    network={
        'primary_ip':cfg['vps']['primary_ip'],'wan_interface':router.get('wan_interface'),
        'wireguard_interface':'wg-vwan','listen_port':router.get('listen_port',51820),
        'mtu':router.get('mtu',1420),'tunnel_address':router.get('tunnel_address'),
        'peer_address':router.get('peer_address'),'peer_public_key':router.get('peer_public_key'),
        'additional_ips':[i['ip'] for i in ips if i.get('state','assigned')=='assigned'],
        'inactive_ips':[i['ip'] for i in ips if i.get('state','assigned')!='assigned']}
    segment=cfg['pve'].get('isolation_segment') or 'legacy'
    registry={'nodes':{cfg['pve']['node']:{'bridge':cfg['pve']['bridge'],'uplink':'REPLACE_ACTUAL_UPLINK_PORT','address':cfg['pve']['host']}},
              'bridge':cfg['pve']['bridge'],'uplink':'REPLACE_ACTUAL_UPLINK_PORT',
              'gateway_ip':cfg['guest']['gateway'],'gateway_network':cfg['pfsense']['next_hop_network'],'gateway_mac':'REPLACE_PFSENSE_PUBLIC_INTERFACE_MAC',
              'clients':[], 'administration_public_networks':[],
              'blocked_networks':['0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8','169.254.0.0/16','172.16.0.0/12','192.168.0.0/16','192.0.0.0/24','192.0.2.0/24','198.18.0.0/15','198.51.100.0/24','203.0.113.0/24','224.0.0.0/4','240.0.0.0/4']}
    if cfg['vps'].get('primary_ip'):
        registry['blocked_networks'].append(cfg['vps']['primary_ip']+'/32')
        registry['administration_public_networks'].append(cfg['vps']['primary_ip']+'/32')
    notes=f'''# Onboarding: {profile['name']}

This archive is a plan, not a blind network deployment. Replace every REPLACE
value and verify interface names, WAN route, VLAN membership and router MAC.
The running infrastructure is not changed by generating this archive.

1. On the new Debian OVH router, inspect `ip -4 addr; ip -4 route get 1.1.1.1`.
   Confirm primary IP {cfg['vps']['primary_ip']} and discover its WAN interface.
   Install wireguard-tools and nftables. Copy virtualwan-router.py to
   /usr/local/sbin/virtualwan and reviewed router-config.json to
   /etc/virtualwan/virtualwan.json. It creates its own service on apply.
   `/usr/local/sbin/virtualwan validate` then `apply`; verify management SSH
   in a second connection and unchanged primary Internet egress.

2. On pfSense, create a dedicated WireGuard tunnel, use its public key in
   router-config.json, and configure the peer endpoint as this router's primary
   IP. Get the router key from `virtualwan status`. Peer Allowed IPs include
   0.0.0.0/0; do not install a new default route. Keepalive 25, MTU 1420.
   Use a distinct /30 tunnel network, not one used by another profile.
   Assign the tunnel, gateway to the router peer, upstream gateway/reply-to.

3. Create the dedicated client VLAN/interface with gateway {cfg['guest']['gateway']}
   and subnet {cfg['pfsense']['next_hop_network']}. Create/enroll the internal
   interface keys in the profile, its Kea scope with static ARP reservations,
   host alias {cfg['pfsense']['public_alias']} and gateway {cfg['pfsense']['wireguard_gateway']}.
   Configure Hybrid/Manual outbound NAT, client public source No NAT on WG,
   pass incoming WG to this public alias with gateway none, public-to-public
   routing with gateway none, outgoing alias Internet policy routed to WG.
   Deny protected/private destinations above this Internet rule. Existing
   LAN default routes and management interfaces must stay unchanged.

4. Create the dedicated bridge {cfg['pve']['bridge']} on each destination PVE,
   no host IPv4, explicit VLAN uplink. Update segment-fragment.json with the
   actual uplink and pfSense MAC, personal protected public endpoints and nodes.
   Add this fragment under registry.segments['{segment}'] (or keep the legacy
   top-level format for an existing legacy profile). Never overwrite existing
   clients/segments. Install the included policy and isolation service on every
   node, preserve /etc/pve/virtualwan-isolation.json, compile/apply before clients.
   Existing flat registry is supported; global nft flush is forbidden.
   Deploy the hook to the configured shared snippets storage, attach it to
   templates, and keep startup ordering before pve-guests.

5. In the UI fill in actual interfaces, new SSH credentials and host-key pins.
   Mark the profile ready only after setup. Sync purchased IPs and deploy one
   test VM. Preflight verifies the route, template, Kea and isolation. Test
   Internet input/output and direct/private isolation before client delivery.

This first-time firewall/VLAN onboarding needs an operator's review. Purchase
approval alone is not authorization to guess/change a physical network layout.
'''
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('README.md',notes)
        archive.writestr('router-config.json',json.dumps(network,indent=2))
        archive.writestr('segment-fragment.json',json.dumps(registry,indent=2))
        for name in ('virtualwan-router.py','public-policy.py','isolation-service.py','virtualwan-isolation-hook.pl'):
            archive.write(root/'tools'/name,name)
    return out.getvalue()
