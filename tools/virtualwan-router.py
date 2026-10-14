#!/usr/bin/env python3
"""Routed IPv4 Additional IPs over WireGuard; root, Debian 13.

Owns only wg-vwan, nft inet virtualwan, protocol 186 host routes and proxy
neighbours listed in its state. Never changes WAN addresses/default routes.
"""
import argparse
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path('/etc/virtualwan')
STATE = ROOT / 'state.json'
CONF = ROOT / 'virtualwan.json'
WGCONF = Path('/etc/wireguard/wg-vwan.conf')
SERVICE = Path('/etc/systemd/system/virtualwan.service')

def run(*args, check=True, input=None):
    return subprocess.run(args, input=input, text=True, capture_output=True,
                          check=check).stdout.strip()

def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def load(path):
    c = json.loads(Path(path).read_text())
    required = {'primary_ip', 'wan_interface', 'wireguard_interface',
                'listen_port', 'mtu', 'tunnel_address', 'peer_address',
                'peer_public_key', 'additional_ips'}
    if set(c) - {'inactive_ips'} != required:
        raise ValueError('Unexpected/missing configuration fields')
    if c['wireguard_interface'] != 'wg-vwan':
        raise ValueError('This installation reserves interface wg-vwan')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}', c['wan_interface']):
        raise ValueError('Invalid WAN interface')
    primary = ipaddress.IPv4Address(c['primary_ip'])
    tunnel = ipaddress.IPv4Interface(c['tunnel_address'])
    peer = ipaddress.IPv4Address(c['peer_address'])
    if peer not in tunnel.network or peer == tunnel.ip or peer in (
            tunnel.network.network_address, tunnel.network.broadcast_address):
        raise ValueError('Invalid tunnel peer')
    ips = [ipaddress.IPv4Address(x) for x in c['additional_ips']+c.get('inactive_ips',[])]
    if not 0 <= len(ips) <= 16 or len(set(ips)) != len(ips):
        raise ValueError('Expected 0-16 distinct Additional IPs')
    if not primary.is_global or primary.is_multicast or any(not x.is_global or x.is_multicast or x == primary for x in ips):
        raise ValueError('Public IPs required; management IP cannot be forwarded')
    if any(x in tunnel.network for x in ips):
        raise ValueError('Tunnel and Additional IPs overlap')
    if type(c['listen_port']) is not int or not 1 <= c['listen_port'] <= 65535:
        raise ValueError('Invalid UDP port')
    if type(c['mtu']) is not int or not 1280 <= c['mtu'] <= 1440:
        raise ValueError('Invalid MTU')
    key = c['peer_public_key']
    if key is not None and len(base64.b64decode(key, validate=True)) != 32:
        raise ValueError('Invalid WireGuard public key')
    return c

def preflight(c, state):
    addr = json.loads(run('ip', '-j', '-4', 'addr', 'show'))
    local_ips = {a['local'] for i in addr for a in i['addr_info']}
    if c['primary_ip'] not in local_ips:
        raise ValueError('Primary IP not assigned: refusing to modify system')
    if any(x in local_ips for x in c['additional_ips']+c.get('inactive_ips',[])):
        raise ValueError('Additional IP already assigned locally; audit required')
    default = json.loads(run('ip', '-j', '-4', 'route', 'get', '1.1.1.1'))[0]
    if default.get('dev') != c['wan_interface'] or default.get('prefsrc') != c['primary_ip']:
        raise ValueError('Internet route/source does not match management configuration')
    if state and any(state['config'][k] != c[k] for k in
                     ('primary_ip', 'wan_interface', 'wireguard_interface', 'tunnel_address')):
        raise ValueError('Changing management/interface/tunnel requires rollback first')
    routes = json.loads(run('ip', '-N', '-j', '-4', 'route', 'show', 'table', 'main'))
    for r in routes:
        if r.get('dst', '').removesuffix('/32') in c['additional_ips']+c.get('inactive_ips',[]):
            managed = (state is not None and str(r.get('protocol')) == '186' and
                       ((r.get('dev') == 'wg-vwan' and r.get('metric') == 20) or
                        (str(r.get('type')) in ('blackhole', '6') and r.get('metric') == 32767)))
            if not managed:
                raise ValueError('Existing unmanaged route for Additional IP')
    if not state and run('ip', 'link', 'show', 'wg-vwan', check=False):
        raise ValueError('wg-vwan already exists without managed state')
    if not state and run('nft', 'list', 'table', 'inet', 'virtualwan', check=False):
        raise ValueError('nft table virtualwan already exists without managed state')

def firewall(c):
    ips = ', '.join(c['additional_ips']+c.get('inactive_ips',[]))
    inactive=', '.join(c.get('inactive_ips',[]))
    wan = c['wan_interface']
    wg = c['wireguard_interface']
    return f'''table inet virtualwan {{
 set public_ips {{ type ipv4_addr; elements = {{ {ips} }}; }}
 set inactive_ips {{ type ipv4_addr; elements = {{ {inactive} }}; }}
 chain input {{
  type filter hook input priority -10; policy accept;
  iifname "{wg}" ip saddr {c['peer_address']} ip daddr {ipaddress.ip_interface(c['tunnel_address']).ip} ip protocol icmp accept
  iifname "{wg}" counter drop
 }}
 chain output {{
  type filter hook output priority -10; policy accept;
  ip saddr @public_ips counter drop
 }}
 chain forward {{
  type filter hook forward priority -10; policy accept;
  ip daddr @inactive_ips counter drop
  ip saddr @inactive_ips counter drop
  iifname "{wan}" oifname "{wg}" ip daddr @public_ips counter accept
  iifname "{wg}" oifname "{wan}" ip saddr @public_ips counter accept
  iifname "{wg}" counter drop
  oifname "{wg}" counter drop
  ip daddr @public_ips counter drop
  ip saddr @public_ips counter drop
 }}
}}
'''

def apply(c):
    state = json.loads(STATE.read_text()) if STATE.exists() else None
    preflight(c, state)
    exists = bool(run('nft', 'list', 'table', 'inet', 'virtualwan', check=False))
    rules = ('delete table inet virtualwan\n' if exists else '') + firewall(c)
    run('nft', '-c', '-f', '-', input=rules)
    if state is None:
        fields = ['net.ipv4.ip_forward', 'net.ipv4.conf.all.rp_filter',
                  'net.ipv4.conf.default.rp_filter',
                  f"net.ipv4.conf.{c['wan_interface']}.rp_filter"]
        state = {'original_sysctls': {k: run('sysctl', '-n', k) for k in fields},
                 'config': c}
        ROOT.mkdir(parents=True, exist_ok=True)
        os.chmod(ROOT, 0o700)
        write(STATE, json.dumps(state, indent=2) + '\n')
    keypath = ROOT / 'server.key'
    if not keypath.exists():
        write(keypath, run('wg', 'genkey') + '\n')
    private = keypath.read_text().strip()
    text = f"[Interface]\nAddress = {c['tunnel_address']}\nListenPort = {c['listen_port']}\nPrivateKey = {private}\nMTU = {c['mtu']}\nTable = off\n"
    if c['peer_public_key']:
        allowed = [c['peer_address'] + '/32'] + [x + '/32' for x in c['additional_ips']]
        text += f"\n[Peer]\nPublicKey = {c['peer_public_key']}\nAllowedIPs = {', '.join(allowed)}\n"
    write(WGCONF, text)
    write(CONF, json.dumps(c, indent=2) + '\n')
    run('nft', '-f', '-', input=rules)
    run('sysctl', '-q', '-w', 'net.ipv4.ip_forward=1')
    for scope in ['all', 'default', c['wan_interface']]:
        run('sysctl', '-q', '-w', f'net.ipv4.conf.{scope}.rp_filter=2')
    for ip in c['additional_ips']+c.get('inactive_ips',[]):
        run('ip', '-4', 'route', 'replace', 'blackhole', ip + '/32',
            'metric', '32767', 'proto', '186')
    if not run('ip', 'link', 'show', 'wg-vwan', check=False):
        run('wg-quick', 'up', 'wg-vwan')
    else:
        stripped = run('wg-quick', 'strip', str(WGCONF))
        fd, tmp = tempfile.mkstemp()
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(stripped + '\n')
            run('wg', 'syncconf', 'wg-vwan', tmp)
            run('ip', 'link', 'set', 'dev', 'wg-vwan', 'mtu', str(c['mtu']))
        finally:
            os.unlink(tmp)
    run('sysctl', '-q', '-w', 'net.ipv4.conf.wg-vwan.rp_filter=2')
    for ip in c['additional_ips']:
        run('ip', '-4', 'route', 'replace', ip + '/32', 'dev', 'wg-vwan',
            'metric', '20', 'proto', '186')
        run('ip', 'neigh', 'replace', 'proxy', ip, 'dev', c['wan_interface'])
    for ip in c.get('inactive_ips',[]):
        run('ip','-4','route','del',ip+'/32','metric','20','proto','186',check=False)
        run('ip','neigh','del','proxy',ip,'dev',c['wan_interface'],check=False)
    old=set(state['config']['additional_ips']+state['config'].get('inactive_ips',[]))
    new=set(c['additional_ips']+c.get('inactive_ips',[]))
    for ip in old - new:
        remove_ip(ip, c['wan_interface'])
    state['config'] = c
    write(STATE, json.dumps(state, indent=2) + '\n')
    unit = '''[Unit]
Description=Virtualwan routed Additional IPs
Wants=network-online.target
After=network-online.target nftables.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/virtualwan apply
ExecReload=/usr/local/sbin/virtualwan apply
ExecStop=/usr/local/sbin/virtualwan down
[Install]
WantedBy=multi-user.target
'''
    write(SERVICE, unit, 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'virtualwan.service')
    status(c)

def remove_ip(ip, wan):
    for metric in ['20', '32767']:
        run('ip', '-4', 'route', 'del', ip + '/32', 'metric', metric,
            'proto', '186', check=False)
    run('ip', 'neigh', 'del', 'proxy', ip, 'dev', wan, check=False)

def down():
    if run('ip', 'link', 'show', 'wg-vwan', check=False):
        run('wg-quick', 'down', 'wg-vwan')
    print('Tunnel down. Blackhole routes/firewall retained; management unaffected.')

def rollback():
    if not STATE.exists():
        print('Nothing to roll back')
        return
    state = json.loads(STATE.read_text())
    run('systemctl', 'disable', 'virtualwan.service', check=False)
    down()
    for ip in state['config']['additional_ips']+state['config'].get('inactive_ips',[]):
        remove_ip(ip, state['config']['wan_interface'])
    run('nft', 'delete', 'table', 'inet', 'virtualwan', check=False)
    for k, v in state['original_sysctls'].items():
        run('sysctl', '-q', '-w', k + '=' + v)
    SERVICE.unlink(missing_ok=True)
    run('systemctl', 'daemon-reload')
    # Keep keys/config for recovery; retain snapshot with a different name.
    STATE.replace(ROOT / 'rollback-state.json')
    print('Managed networking removed; original sysctls restored.')

def status(c):
    public = run('wg', 'pubkey', input=(ROOT / 'server.key').read_text())
    print('VPS public key:', public)
    print('Peer configured:', bool(c['peer_public_key']))
    print(run('ip', '-4', 'route', 'get', '1.1.1.1'))
    print(run('ip', '-4', 'route', 'show', 'proto', '186'))
    print(run('wg', 'show', 'wg-vwan', check=False))

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['validate', 'apply', 'down', 'rollback', 'status'])
    p.add_argument('--config', default=str(CONF))
    args = p.parse_args()
    if args.action == 'validate':
        c = load(args.config)
        print('Valid configuration. Peer configured:', bool(c['peer_public_key']))
        return
    if os.geteuid() != 0:
        p.error('root required')
    with open('/run/virtualwan.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'rollback':
            rollback()
        elif args.action == 'down':
            down()
        else:
            c = load(args.config)
            if args.action == 'apply':
                apply(c)
            else:
                status(c)

if __name__ == '__main__':
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as e:
        if isinstance(e, subprocess.CalledProcessError):
            # Never print command arguments: wg operations may contain secrets.
            print('Command failed:', e.stderr, file=sys.stderr)
        else:
            print(str(e), file=sys.stderr)
        sys.exit(1)
