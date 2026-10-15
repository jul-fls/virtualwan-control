#!/usr/bin/env python3
"""Per-activation credentials, provided only as a Linux password hash on a CD."""
import json,os,pwd,re,secrets,subprocess,time
from pathlib import Path

def validate(data,macs):
    if not isinstance(data,dict) or data.get('version')!=1:raise ValueError('Unsupported credential protocol')
    if not re.fullmatch(r'[a-f0-9]{32}',str(data.get('session',''))):raise ValueError('Invalid rescue session')
    if str(data.get('mac','')).lower() not in macs:raise ValueError('Credential media belongs to another NIC')
    hashed=data.get('password_hash','')
    if not re.fullmatch(r'\$6\$[A-Za-z0-9./]{1,16}\$[A-Za-z0-9./]{86}',hashed):raise ValueError('Invalid SHA-512 crypt password hash')
    return hashed

def command(args,**kw):return subprocess.run(args,check=True,capture_output=True,text=True,**kw)

def main():
    # Never leave Debian Live's default password usable, even if media is absent.
    command(['usermod','--lock','rescue'])
    config=Path('/etc/ssh/sshd_config.d/00-virtualwan-rescue.conf')
    config.write_text('PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nAllowUsers rescue\n')
    media=Path('/dev/disk/by-label/VWAN_RESCUE_AUTH')
    for _ in range(10):
        if media.exists():break
        time.sleep(.5)
    if not media.exists():
        print('VIRTUALWAN_RESCUE_AUTH_KEY_ONLY: no credential CD; no shared password is enabled.',flush=True)
        return
    target=Path('/run/virtualwan-rescue-auth');target.mkdir(exist_ok=True)
    command(['mount','-o','ro,nosuid,nodev,noexec',str(media),str(target)])
    try:
        raw=(target/'credential.json').read_bytes()
        if len(raw)>16384:raise ValueError('Credential document too large')
        data=json.loads(raw)
    finally:command(['umount',str(target)])
    macs={p.read_text().strip().lower() for p in Path('/sys/class/net').glob('*/address') if p.parent.name!='lo'}
    hashed=validate(data,macs)
    command(['chpasswd','--encrypted'],input='rescue:'+hashed+'\n')
    config.write_text('PasswordAuthentication yes\nKbdInteractiveAuthentication no\nPermitRootLogin no\nAllowUsers rescue\n')
    # This is non-secret lifecycle data, not a password or its hash.
    Path('/run/virtualwan-rescue-auth.json').write_text(json.dumps({'session':data['session'],'mac':data['mac'],'ipv4':data.get('ipv4')}))
    print('VIRTUALWAN_RESCUE_AUTH_READY: activation-specific console/SSH password configured.',flush=True)

if __name__=='__main__':main()
