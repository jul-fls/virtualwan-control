"""Stdlib-only cloud-init helpers, also embedded in the standalone PVE adapter."""
import ipaddress,re

def os_network_data(guest,ipv4,mac,config=None):
    data=ipv6_network_data(guest,ipv4,mac,config)
    # v1 static6 rejects these keys; sysctl files disable RA/autoconfiguration.
    for subnet in data['config'][0]['subnets']:
        if subnet['type']=='static6':
            subnet.pop('accept-ra',None);subnet.pop('autoconf',None)
    subnet=data['config'][0]['subnets'][0]
    subnet.pop('pointopoint',None)
    subnet['routes']=[{'destination':guest['gateway']+'/32','gateway':'0.0.0.0'}]
    if guest.get('os_family','debian')=='debian':
        # ifupdown must install the link route before the default route.
        subnet.pop('gateway',None)
        subnet['routes'].append({'destination':'0.0.0.0/0','gateway':guest['gateway']})
    return data

def os_user_data(guest,fqdn,ipv4,mac,access,hashed_password='',config=None):
    family=guest.get('os_family','debian');bsd=family=='freebsd';rpm=family in ('fedora','almalinux','rocky')
    interface=guest['interface']
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,14}',interface):raise ValueError('Invalid guest interface')
    user={'name':access['username'],'shell':'/bin/sh' if bsd else '/bin/bash','groups':['wheel' if bsd or rpm else 'sudo'],'sudo':['ALL=(ALL) NOPASSWD:ALL'],'lock_passwd':access['ssh_auth']=='key' and not bsd}
    if access.get('public_key'):user['ssh_authorized_keys']=[access['public_key']]
    if hashed_password:user['hashed_passwd']=hashed_password
    data={'users':[user],'disable_root':True,'ssh_pwauth':access['ssh_auth']!='key','chpasswd':{'expire':False},'ssh_deletekeys':True,'fqdn':fqdn,'hostname':fqdn,'prefer_fqdn_over_hostname':True}
    if bsd:
        # Require a template with the Python cloud-init NoCloud datasource (not
        # nuageinit-only); rc.conf retains /32 routing across future boots.
        gw=str(ipaddress.IPv4Address(guest['gateway']));ip=str(ipaddress.IPv4Address(ipv4));mtu=int(guest['mtu'])
        rc=f'\n# Virtualwan public routing\nhostname="{fqdn}"\nifconfig_{interface}="inet {ip} netmask 255.255.255.255 mtu {mtu}"\ndefaultrouter="NO"\nstatic_routes="virtualwan_gateway virtualwan_default"\nroute_virtualwan_gateway="-host {gw} -iface {interface}"\nroute_virtualwan_default="default {gw}"\nsshd_enable="YES"\n'
        if config:
            addr=str(ipaddress.IPv6Address(config['address']));link=str(ipaddress.IPv6Address(config['gateway_link_local']))
            rc+=f'ifconfig_{interface}_ipv6="inet6 {addr} prefixlen 128 -accept_rtadv auto_linklocal"\nipv6_defaultrouter="{link}%{interface}"\n'
        rc+='keymap="fr"\n'
        data.update(packages=['sudo','curl','python3','qemu-guest-agent'],write_files=[{'path':'/etc/rc.conf','append':True,'content':rc},{'path':'/boot/loader.conf','append':True,'content':'\nconsole="vidconsole"\n'},{'path':'/etc/resolv.conf','content':''.join('nameserver '+str(ipaddress.ip_address(n))+'\n' for n in guest['dns'])}],bootcmd=[['install','-d','-m','0755','/home'],['sh','-c',f'route add -host {gw} -iface {interface} 2>/dev/null || true; route change default {gw} 2>/dev/null || route add default {gw}']],runcmd=[['sysrc','qemu_guest_agent_enable=YES'],['sh','-c','service qemu-guest-agent onestatus >/dev/null 2>&1 || service qemu-guest-agent start']])
        if config:data['bootcmd'].append(['sh','-c',f'ifconfig {interface} inet6 -accept_rtadv; ifconfig {interface} inet6 {addr} prefixlen 128 alias 2>/dev/null || true; route change -inet6 default {link}%{interface} 2>/dev/null || route add -inet6 default {link}%{interface}'])
    else:
        data['packages']=['qemu-guest-agent','curl','python3','sudo']+(['kbd'] if rpm else ['keyboard-configuration','console-setup'])
        data['write_files']=[{'path':'/etc/default/grub.d/99-virtualwan-console.cfg','permissions':'0644','content':'GRUB_TERMINAL=console\nGRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX} console=tty0"\n'},{'path':'/etc/default/keyboard','permissions':'0644','content':'XKBMODEL="pc105"\nXKBLAYOUT="fr"\nXKBVARIANT=""\nXKBOPTIONS=""\n'}]
        data['runcmd']=([['localectl','set-keymap','fr']] if rpm else [['setupcon','--force']])+[['sh','-c','command -v update-grub >/dev/null && update-grub || true'],['systemctl','enable','--now','getty@tty1.service'],['systemctl','enable','--now','qemu-guest-agent']]
        if rpm:data['runcmd'].append(['sh','-c','command -v grubby >/dev/null && grubby --update-kernel=ALL --remove-args=console --args=console=tty0 || true'])
        if family!='freebsd':
            data['bootcmd']=[['ip','route','replace',str(ipaddress.IPv4Address(guest['gateway']))+'/32','dev',interface],['ip','route','replace','default','via',guest['gateway'],'dev',interface,'src',ipv4]]
        if config:data['write_files']+=ipv6_cloud_files(interface)
    return data

def freebsd_probe_code(dual=False):
    return '''import json,subprocess,re,ipaddress,os
os.environ['PATH']='/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin'
r=lambda *a:subprocess.check_output(a,text=True)
v4=[];v6=[];dev=None
for line in r('ifconfig','-a').splitlines():
 m=re.match(r'^(\\S+): flags',line)
 if m:
  dev=m[1];v4.append({'ifname':'lo' if dev=='lo0' else dev,'addr_info':[]});v6.append({'ifname':'lo' if dev=='lo0' else dev,'addr_info':[]})
 m=re.match(r'\\s+inet (\\S+)',line)
 if m and dev:v4[-1]['addr_info'].append({'local':m[1]})
 m=re.match(r'\\s+inet6 ([0-9a-f:]+)',line)
 if m and dev:v6[-1]['addr_info'].append({'local':m[1],'scope':'global' if ipaddress.ip_address(m[1]).is_global else 'link'})
def gateway(family):
 return re.search(r'gateway: (\\S+)',r('route','-n','get',family,'default'))[1].split('%')[0]
data={'addresses':v4,'routes':[{'dst':'default','gateway':gateway('-inet')}],'dns':open('/etc/resolv.conf').read(),'hostname':r('hostname').strip(),'ssh_fingerprint':r('ssh-keygen','-lf','/etc/ssh/ssh_host_ed25519_key.pub').strip(),'egress':r('curl','-4','-fsS','--max-time','15','https://api.ipify.org').strip()}
''' + ("data['ipv6']={'addresses':v6,'routes':[{'dst':'default','gateway':gateway('-inet6')}],'egress':r('curl','-6','-fsS','--max-time','15','https://api64.ipify.org').strip()}\n" if dual else '')+'print(json.dumps(data))\n'

# Imported in normal modules, already defined when concatenated remotely.
if __name__!='__main__':
    from .ipv6 import ipv6_network_data,ipv6_cloud_files
