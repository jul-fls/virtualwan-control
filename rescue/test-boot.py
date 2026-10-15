#!/usr/bin/env python3
"""Boot the common ISO offline with disposable disks and per-VM NoCloud data."""
import base64,json,os,secrets,shutil,socket,subprocess,tempfile,time
from pathlib import Path

ISO=Path('/output/virtualwan-rescue-debian13-amd64-v2.iso')

def qga(path,request):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5);s.connect(str(path));s.sendall(json.dumps(request).encode()+b'\n');stream=s.makefile('rb')
        for _ in range(5):
            result=json.loads(stream.readline())
            if 'error' in result:raise RuntimeError(result['error'])
            if 'return' in result:return result['return']
    raise RuntimeError('No QGA response')

def guest(path,code):
    task=qga(path,{'execute':'guest-exec','arguments':{'path':'/usr/bin/python3','arg':['-c',code],'capture-output':True}})
    for _ in range(40):
        result=qga(path,{'execute':'guest-exec-status','arguments':{'pid':task['pid']}})
        if result.get('exited'):
            output=base64.b64decode(result.get('out-data','')).decode()
            if result.get('exitcode'):raise RuntimeError(base64.b64decode(result.get('err-data','')).decode())
            return json.loads(output)
        time.sleep(.2)
    raise RuntimeError('Guest command timeout')

previous_password=None

def boot(mode):
    global previous_password
    with tempfile.TemporaryDirectory(prefix='rescue-boot-') as directory:
        root=Path(directory);seed=root/'seed';seed.mkdir()
        network={'version':1,'config':[{'type':'physical','name':'ens18','mac_address':'52:54:00:12:34:56','mtu':1420,'subnets':[{'type':'static','address':'198.51.100.23/32','gateway':'10.0.2.2','dns_nameservers':['1.1.1.1']},{'type':'static6','address':'2001:db8:23::23/128','gateway':'fe80::2'}]}]}
        (seed/'network-config').write_text(json.dumps(network));(seed/'meta-data').write_text('instance-id: synthetic-rescue-test\n')
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(root/'ssh-key')],check=True)
        public_key=(root/'ssh-key.pub').read_text().strip()
        (seed/'user-data').write_text('#cloud-config\n'+json.dumps({'users':[{'name':'debian','ssh_authorized_keys':[public_key]}],'runcmd':['touch /NO_CLOUD_USERDATA_MUST_NOT_EXECUTE']}))
        subprocess.run(['xorriso','-as','mkisofs','-quiet','-V','cidata','-J','-r','-o',str(root/'seed.iso'),str(seed)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['qemu-img','create','-q','-f','qcow2',str(root/'blank.qcow2'),'64M'],check=True)
        auth=root/'auth';auth.mkdir();password=''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789') for _ in range(12))
        hashed=subprocess.run(['openssl','passwd','-6','-stdin'],input=password+'\n',text=True,capture_output=True,check=True).stdout.strip()
        (auth/'credential.json').write_text(json.dumps({'version':1,'vmid':120,'session':secrets.token_hex(16),'mac':'52:54:00:12:34:56','ipv4':'198.51.100.23','password_hash':hashed}))
        subprocess.run(['xorriso','-as','mkisofs','-quiet','-V','VWAN_RESCUE_AUTH','-J','-r','-o',str(root/'auth.iso'),str(auth)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        qsocket=root/'qga.sock';log=root/'serial.log'
        args=['qemu-system-x86_64','-accel','kvm' if Path('/dev/kvm').exists() else 'tcg','-m','1024','-smp','2','-display','none','-serial','file:'+str(log),'-monitor','none','-no-reboot','-boot','order=d,strict=on','-cdrom',str(ISO),'-drive','file='+str(root/'blank.qcow2')+',if=virtio,format=qcow2','-netdev','user,id=n0,restrict=on,hostfwd=tcp:127.0.0.1:22022-198.51.100.23:22','-device','virtio-net-pci,netdev=n0,mac=52:54:00:12:34:56','-chardev','socket,path='+str(qsocket)+',server=on,wait=off,id=qga0','-device','virtio-serial','-device','virtserialport,chardev=qga0,name=org.qemu.guest_agent.0']
        args+=['-drive','file='+str(root/'auth.iso')+',index=1,media=cdrom,readonly=on']
        if mode!='no-seed':args+=['-drive','file='+str(root/'seed.iso')+',index=3,media=cdrom,readonly=on']
        if mode=='uefi':
            shutil.copyfile('/usr/share/OVMF/OVMF_VARS_4M.fd',root/'vars.fd');args+=['-machine','q35','-drive','if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd','-drive','if=pflash,format=raw,file='+str(root/'vars.fd')]
        with (root/'qemu.log').open('w') as error:
            process=subprocess.Popen(args,stdout=error,stderr=error)
            try:
                deadline=time.monotonic()+180
                while time.monotonic()<deadline:
                    if process.poll() is not None:raise RuntimeError('QEMU exited '+str(process.returncode))
                    try:
                        state=guest(qsocket,"import json,subprocess,pathlib; r=subprocess.run(['systemctl','show','virtualwan-rescue','-p','ActiveState','--value'],capture_output=True,text=True); print(json.dumps({'state':r.stdout.strip()}))")
                        if state['state'] in ('active','failed'):break
                    except (OSError,ValueError,RuntimeError):pass
                    time.sleep(1)
                else:raise RuntimeError('Live ISO boot timeout: '+str(log))
                code="""import json,subprocess,pathlib
r=lambda *a:subprocess.run(a,capture_output=True,text=True)
import time
for _ in range(30):
 if not any(a.get('tentative') for n in json.loads(r('ip','-j','addr').stdout) for a in n['addr_info']):break
 time.sleep(0.2)
print(json.dumps({'addresses':json.loads(r('ip','-j','addr').stdout),'routes4':json.loads(r('ip','-j','-4','route').stdout),'routes6':json.loads(r('ip','-j','-6','route').stdout),'network':json.loads(pathlib.Path('/run/virtualwan-rescue-network.json').read_text()) if pathlib.Path('/run/virtualwan-rescue-network.json').exists() else None,'disk_mounted':r('findmnt','-n','-S','/dev/vda').returncode==0,'userdata_executed':pathlib.Path('/NO_CLOUD_USERDATA_MUST_NOT_EXECUTE').exists(),'busybox':r('busybox','true').returncode==0,'rescue_user':r('id','rescue').returncode==0,'gateway4':r('ping','-c','1','-W','2','10.0.2.2').returncode==0,'gateway6':r('ping','-6','-c','1','-W','2','fe80::2%'+(json.loads(pathlib.Path('/run/virtualwan-rescue-network.json').read_text()).get('interface','eth0') if pathlib.Path('/run/virtualwan-rescue-network.json').exists() else 'eth0')).returncode==0,'ssh_config':r('sshd','-T').stdout}))
"""
                result=guest(qsocket,code)
                assert result['busybox'] and result['rescue_user'] and not result['disk_mounted'] and not result['userdata_executed'],result
                assert 'passwordauthentication yes' in result['ssh_config'] and 'permitrootlogin no' in result['ssh_config']
                if mode=='no-seed':
                    assert result['network'] is None
                    assert not any(a['family']=='inet' for nic in result['addresses'] if nic['ifname']!='lo' for a in nic['addr_info'])
                else:
                    assert result['network']['ipv4']=='198.51.100.23/32' and result['network']['ipv6']=='2001:db8:23::23/128'
                    assert result['network']['interface']!='ens18'
                    assert any(r.get('dst')=='default' and r.get('gateway')=='10.0.2.2' for r in result['routes4'])
                    assert any(r.get('dst')=='default' and r.get('gateway')=='fe80::2' for r in result['routes6'])
                    assert result['gateway4'] and result['gateway6'], 'Gateway unreachable: '+json.dumps(result)
                    addresses=[a['local'] for nic in result['addresses'] if nic['ifname']!='lo' for a in nic['addr_info'] if a['family']=='inet']
                    assert addresses==['198.51.100.23'],addresses
                    login=subprocess.run(['ssh','-i',str(root/'ssh-key'),'-p','22022','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','StrictHostKeyChecking=accept-new','-o','UserKnownHostsFile='+str(root/'known-hosts'),'rescue@127.0.0.1','id -un && sudo -n true'],capture_output=True,text=True,timeout=15)
                    assert login.returncode==0 and login.stdout.strip()=='rescue',login.stderr
                    result['ssh_key_login']=True
                    askpass=root/'askpass';askpass.write_text('#!/bin/sh\nprintenv VWAN_TEST_RESCUE_PASSWORD\n');askpass.chmod(0o700)
                    def password_login(value):
                        env={**os.environ,'DISPLAY':':99','SSH_ASKPASS_REQUIRE':'force','SSH_ASKPASS':str(askpass),'VWAN_TEST_RESCUE_PASSWORD':value}
                        return subprocess.run(['ssh','-p','22022','-o','PreferredAuthentications=password','-o','PubkeyAuthentication=no','-o','NumberOfPasswordPrompts=1','-o','ConnectTimeout=5','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(root/'known-hosts'),'rescue@127.0.0.1','id -un'],env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=15)
                    login=password_login(password);assert login.returncode==0 and login.stdout.strip()=='rescue',login.stderr
                    if previous_password:assert password_login(previous_password).returncode!=0
                    previous_password=password;result['ssh_password_login']=True
                result.pop('ssh_config');(Path('/output')/('validation-'+mode+'.json')).write_text(json.dumps(result,indent=2))
                print(json.dumps({'boot':mode,'pass':True,'network':result['network'],'gateway4':result['gateway4'],'gateway6':result['gateway6']}),flush=True)
            finally:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                for source,name in [(log,'boot-'+mode+'.log'),(root/'qemu.log','qemu-'+mode+'.log')]:
                    if source.exists():shutil.copyfile(source,Path('/output')/name)

if __name__=='__main__':
    local_iso=Path('/tmp/virtualwan-rescue-test.iso')
    shutil.copyfile(ISO,local_iso);ISO=local_iso
    for mode in ('bios','uefi','no-seed'):boot(mode)
