#!/usr/bin/env python3
"""Configure only the ephemeral live OS; never execute NoCloud user-data."""
import ipaddress,json,os,pwd,re,subprocess,time
from pathlib import Path

def run(*args):
    result=subprocess.run(args,text=True,capture_output=True)
    if result.returncode:raise RuntimeError(args[0]+': '+result.stderr.strip())
    return result.stdout

def plan(network,interfaces):
    if not isinstance(network,dict) or network.get('version')!=1:raise ValueError('Expected NoCloud network-config version 1')
    physical=[c for c in network.get('config',[]) if c.get('type')=='physical']
    if len(physical)!=1:raise ValueError('Exactly one managed public NIC is required')
    cfg=physical[0];mac=str(cfg.get('mac_address','')).lower()
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',mac) or mac not in interfaces:raise ValueError('Network MAC does not match a live NIC')
    interface=interfaces[mac]
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}',interface):raise ValueError('Invalid NIC name')
    mtu=int(cfg.get('mtu',1420))
    if not 1280<=mtu<=9000:raise ValueError('Invalid MTU')
    result={'interface':interface,'mac':mac,'mtu':mtu,'dns':[]}
    for subnet in cfg.get('subnets',[]):
        kind=subnet.get('type');address=ipaddress.ip_interface(subnet.get('address',''))
        if kind=='static' and address.version==4 and address.network.prefixlen==32:
            gateway=ipaddress.IPv4Address(subnet.get('gateway',subnet.get('pointopoint','')))
            if 'ipv4' in result:raise ValueError('Duplicate IPv4')
            result.update(ipv4=str(address),gateway4=str(gateway))
        elif kind=='static6' and address.version==6 and address.network.prefixlen==128:
            gateway=ipaddress.IPv6Address(subnet.get('gateway',''))
            if not gateway.is_link_local or 'ipv6' in result:raise ValueError('Invalid IPv6 gateway or duplicate IPv6')
            result.update(ipv6=str(address),gateway6=str(gateway))
        else:raise ValueError('Unsupported network subnet; no DHCP fallback')
        for dns in subnet.get('dns_nameservers',[]):
            value=str(ipaddress.ip_address(dns))
            if value not in result['dns']:result['dns'].append(value)
    if 'ipv4' not in result:raise ValueError('A public /32 is required')
    return result

def authorized_keys(user):
    keys=[]
    if not isinstance(user,dict):return keys
    rows=[user.get('ssh_authorized_keys',[])]
    rows += [u.get('ssh_authorized_keys',[]) for u in user.get('users',[]) if isinstance(u,dict)]
    for group in rows:
        if not isinstance(group,list):continue
        for key in group:
            if isinstance(key,str) and len(key)<=16384 and re.fullmatch(r'(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com) [A-Za-z0-9+/]+={0,3}(?: [^\r\n]*)?',key) and key not in keys:keys.append(key)
    return keys[:100]

def rescue_hostname(user):
    if not isinstance(user,dict):return None
    value=user.get('fqdn') or user.get('hostname')
    if not value:return None
    if not isinstance(value,str):raise ValueError('Invalid guest hostname')
    value=value.lower().rstrip('.')
    labels=value.split('.')
    if len(value)>253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in labels):raise ValueError('Invalid guest hostname')
    labels[0]+='-rescue'
    result='.'.join(labels)
    if len(labels[0])>63 or len(result)>253:raise ValueError('Rescue hostname exceeds DNS limits')
    return result


def document(path):
    import yaml
    with path.open('rb') as stream:data=stream.read(1024*1024+1)
    if len(data)>1024*1024:raise ValueError('NoCloud document exceeds size limit')
    return yaml.safe_load(data)

def main():
    run('ssh-keygen','-A')
    devices=[]
    for _ in range(20):
        devices=[Path('/dev/disk/by-label')/label for label in ('cidata','CIDATA') if (Path('/dev/disk/by-label')/label).exists()]
        if devices:break
        time.sleep(1)
    if len({str(p.resolve()) for p in devices})!=1:raise ValueError('Missing or ambiguous NoCloud CD; network stays unconfigured')
    medium=Path('/run/virtualwan-rescue-seed');medium.mkdir(exist_ok=True)
    run('mount','-o','ro,nosuid,nodev,noexec',str(devices[0]),str(medium))
    try:
        network=document(medium/'network-config');user=document(medium/'user-data')
    finally:run('umount',str(medium))
    interfaces={p.read_text().strip().lower():p.parent.name for p in Path('/sys/class/net').glob('*/address') if p.parent.name!='lo'}
    settings=plan(network,interfaces);nic=settings['interface']
    hostname=rescue_hostname(user)
    if hostname:
        run('hostname',hostname)
        Path('/etc/hostname').write_text(hostname+'\n')
        hosts=Path('/etc/hosts')
        lines=[line for line in hosts.read_text().splitlines() if not re.match(r'^\s*127\.0\.1\.1(?:\s|$)',line)]
        hosts.write_text('\n'.join(lines)+'\n127.0.1.1 '+hostname+' '+hostname.split('.')[0]+'\n')
        settings['hostname']=hostname
    for key in ('accept_ra','autoconf','use_tempaddr'):
        Path('/proc/sys/net/ipv6/conf',nic,key).write_text('0')
    run('ip','link','set','dev',nic,'mtu',str(settings['mtu']),'up')
    run('ip','-4','addr','replace',settings['ipv4'],'dev',nic)
    source=settings['ipv4'].split('/')[0]
    run('ip','-4','route','replace',settings['gateway4']+'/32','dev',nic,'src',source)
    run('ip','-4','route','replace','default','via',settings['gateway4'],'dev',nic,'onlink','src',source)
    if settings.get('ipv6'):
        run('ip','-6','addr','replace',settings['ipv6'],'dev',nic)
        run('ip','-6','route','replace','default','via',settings['gateway6'],'dev',nic)
    resolver=Path('/etc/resolv.conf')
    if resolver.is_symlink():resolver.unlink()
    resolver.write_text(''.join('nameserver '+dns+'\n' for dns in settings['dns']))
    account=pwd.getpwnam('rescue');ssh=Path(account.pw_dir)/'.ssh';ssh.mkdir(mode=0o700,exist_ok=True);ssh.chmod(0o700)
    authorized=ssh/'authorized_keys';authorized.write_text(''.join(key+'\n' for key in authorized_keys(user)));authorized.chmod(0o600)
    os.chown(ssh,account.pw_uid,account.pw_gid);os.chown(authorized,account.pw_uid,account.pw_gid)
    Path('/run/virtualwan-rescue-network.json').write_text(json.dumps(settings,indent=2)+'\n')
    print('VIRTUALWAN_RESCUE_NETWORK_READY '+json.dumps(settings),flush=True)
    print('SSH login: rescue (guest authorized keys only). Disks remain unmounted.',flush=True)
    print(run('ssh-keygen','-lf','/etc/ssh/ssh_host_ed25519_key.pub'),flush=True)

if __name__=='__main__':main()
