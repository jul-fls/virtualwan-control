#!/usr/bin/env python3
"""Executed on an explicitly configured Linux host. Input JSON is never echoed."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import hashlib

if 'ipv6_settings' not in globals():
    from virtualwan.ipv6 import ipv6_settings,ipv6_address,ipv6_validate_address,ipv6_link_local,ipv6_network_data,ipv6_cloud_files


def audit(event, **data):
    print(json.dumps({'event': event, **data}), file=sys.stderr, flush=True)


def run(*args, input=None):
    audit('command.begin', argv=args)
    p = subprocess.run(args, input=input, text=True, capture_output=True)
    audit('command.end', argv=args, exit_code=p.returncode)
    if p.returncode:
        # Command output may contain a guest password; orchestrator scrubs it.
        raise RuntimeError(f'{args[0]} exited {p.returncode}: {p.stderr[-2000:]}')
    return p.stdout


def reload_virtualwan():
    try:return run('systemctl','reload','virtualwan')
    except Exception as error:
        try:
            journal=subprocess.run(['journalctl','-u','virtualwan.service','--since','-2min','-n','80','--no-pager','-o','cat'],text=True,capture_output=True,timeout=15)
            detail=re.sub(r'(?im)(private\s*key\s*[=:]\s*)[^\r\n]+',r'\1[REDACTED]',journal.stdout[-12000:])
            audit('vps.reload.failed',service_journal=detail,journal_exit_code=journal.returncode)
        except Exception:detail='Service journal unavailable'
        raise RuntimeError('virtualwan.service reload failed: '+str(error)+'\n'+detail) from error


def api(p, resource):
    return json.loads(run('pvesh', 'get', f'/nodes/{p["node"]}/{resource}', '--output-format', 'json'))


def emit(value):
    print(json.dumps(value))


def register_isolation(p, q, vmid, mac):
    if Path('/etc/pve/virtualwan-isolation.json').exists():
        extra=['--segment',p['isolation_segment']] if p.get('isolation_segment') else []
        if q.get('ipv6'):
            extra+=['--public-ipv6',q['ipv6']['address'],'--link-local-ipv6',ipv6_link_local(mac)]
        run('/usr/local/sbin/virtualwan-isolation','register','--vmid',vmid,'--mac',mac,'--public-ip',q['ip'],'--next-hop',q['next_hop'],*extra)
        run('qm','set',vmid,'--hookscript',p['snippet_storage']+':snippets/virtualwan-isolation-hook.pl')


def router_bootstrap(q):
    """Fresh Debian router only; never overwrites an existing installation."""
    import base64
    import importlib.util
    c=q['router'];primary=str(ipaddress.IPv4Address(c['primary_ip']))
    route=json.loads(run('ip','-j','-4','route','get','1.1.1.1'))[0]
    if route.get('prefsrc')!=primary:raise ValueError('Primary source mismatch')
    if c.get('wan_interface') and c['wan_interface']!=route['dev']:raise ValueError('Configured WAN differs from actual route')
    c['wan_interface']=route['dev']
    release=Path('/etc/os-release').read_text()
    if not re.search(r'^ID=\"?debian\"?$',release,re.M) or not re.search(r'^VERSION_ID=\"?13\"?$',release,re.M):
        raise ValueError('Automatic onboarding currently supports fresh Debian 13 only')
    target=Path('/etc/virtualwan');binary=Path('/usr/local/sbin/virtualwan')
    if target.exists() or binary.exists() or Path('/etc/wireguard/wg-vwan.conf').exists() or Path('/etc/systemd/system/virtualwan.service').exists():
        raise ValueError('Existing installation: inspect/recover it manually; onboarding is not an upgrade')
    source=base64.b64decode(q['source_base64'],validate=True)
    if hashlib.sha256(source).hexdigest()!=q['source_sha256']:raise ValueError('Installer digest mismatch')
    with tempfile.TemporaryDirectory(prefix='virtualwan-validate-') as temp:
        script=Path(temp)/'router.py';script.write_bytes(source)
        spec=importlib.util.spec_from_file_location('vwan_router',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        config=Path(temp)/'config.json';config.write_text(json.dumps(c));module.load(config)
    # Check interface ownership BEFORE package or network changes.
    if subprocess.run(['ip','link','show','wg-vwan'],capture_output=True).returncode==0:
        raise ValueError('Unmanaged wg-vwan exists')
    import shutil
    if shutil.which('nft') and subprocess.run(['nft','list','table','inet','virtualwan'],capture_output=True).returncode==0:
        raise ValueError('Unmanaged nft table exists')
    audit('router.packages.begin')
    env={**os.environ,'DEBIAN_FRONTEND':'noninteractive'}
    for argv in (['apt-get','update'],['apt-get','install','-y','--no-install-recommends','wireguard-tools','nftables']):
        result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=180)
        audit('router.packages.command',argv=argv,exit_code=result.returncode)
        if result.returncode:raise RuntimeError('Router package installation failed')
    target.mkdir(mode=0o700);binary.write_bytes(source);binary.chmod(0o700)
    config=target/'virtualwan.json';config.write_text(json.dumps(c,indent=2));config.chmod(0o600)
    run(str(binary),'validate')
    # Leave this rollback timer armed until the controller reconnects successfully.
    run('systemd-run','--unit=virtualwan-bootstrap-rollback','--on-active=300s',str(binary),'rollback')
    try:
        run(str(binary),'apply')
        current=json.loads(run('ip','-j','-4','route','get','1.1.1.1'))[0]
        if current.get('dev')!=route['dev'] or current.get('prefsrc')!=primary:raise ValueError('Management route changed')
        key=run('wg','show','wg-vwan','public-key').strip()
        emit({'public_key':key,'wan_interface':route['dev'],'primary_ip':primary,'rollback_armed':True,'home_network_configured':False})
    except Exception:
        # Exact existing router rollback restores only resources owned by this tool.
        subprocess.run([str(binary),'rollback'],capture_output=True)
        raise


def pve_ipv6_allocate(q):
    import fcntl
    c=ipv6_settings(q['ipv6']);p=q['pve'];vmid=int(p['vmid'])
    with open('/run/virtualwan-isolation.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        r=json.loads(Path('/etc/pve/virtualwan-isolation.json').read_text())
        segments=list(r.get('segments',{}).values())+([r] if 'clients' in r else [])
        segment=r['segments'][p['isolation_segment']] if p.get('isolation_segment') else r
        existing=next((v for v in segment['clients'] if v['vmid']==vmid),None)
        if existing and existing['public_ip']!=q['ip']:raise ValueError('VMID belongs to another public IPv4')
        address=existing.get('public_ipv6') if existing else None
        address=ipv6_validate_address(c,address or c.get('address') or ipv6_address(c,vmid))
        if c.get('address') and c['address']!=address:raise ValueError('Saved IPv6 differs from registered VM')
        for s in segments:
            for v in s['clients']:
                if v.get('public_ipv6')==address and (s is not segment or v['vmid']!=vmid):raise ValueError('IPv6 already allocated')
    emit({'public_ipv6':address,'source':'registry' if existing and existing.get('public_ipv6') else 'vmid'})


def pve_ipv6_prepare(q):
    """Upgrade only owned isolation files, with backups and strict host-key checks."""
    import base64,fcntl,shlex,types
    p=q['pve'];c=ipv6_settings(q['ipv6']);registry_path=Path('/etc/pve/virtualwan-isolation.json')
    sources={}
    for name in ('public-policy.py','isolation-service.py'):
        item=q['sources'][name];data=base64.b64decode(item['base64'],validate=True)
        if hashlib.sha256(data).hexdigest()!=item['sha256']:raise ValueError('Isolation source digest mismatch')
        compile(data,name,'exec');sources[name]=data
    registry=json.loads(registry_path.read_text());segment=registry['segments'][p['isolation_segment']] if p.get('isolation_segment') else registry
    if segment['nodes'][p['node']]['bridge']!=p['bridge']:raise ValueError('IPv6 isolation bridge differs')
    previous=segment.get('ipv6')
    if previous:
        old=ipv6_settings({**previous,'enabled':True})
        if any(old[k]!=c[k] for k in ('network','gateway_address','gateway_link_local','protected_networks')):raise ValueError('Existing IPv6 segment differs; reconcile it before changing a live prefix')
    candidate=json.loads(json.dumps(registry));target=candidate['segments'][p['isolation_segment']] if p.get('isolation_segment') else candidate
    target['ipv6']={k:v for k,v in c.items() if k!='address'}
    policy=types.ModuleType('policy');exec(compile(sources['public-policy.py'],'public-policy.py','exec'),policy.__dict__)
    for s in list(candidate.get('segments',{}).values())+([candidate] if 'clients' in candidate else []):
        for definition in s['nodes'].values():policy.validate({**s,**definition})
    backup=Path('/var/lib/virtualwan/runs')/q['run_id'];backup.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not (backup/'isolation-registry.before.json').exists():(backup/'isolation-registry.before.json').write_bytes(registry_path.read_bytes())
    # One shared registry write. A failed node upgrade leaves a retryable stopped VM.
    with open('/run/virtualwan-isolation.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if json.loads(registry_path.read_text())!=registry:raise ValueError('Isolation registry changed during IPv6 preparation')
        temporary=registry_path.with_suffix('.ipv6.next');temporary.write_text(json.dumps(candidate,indent=2));os.replace(temporary,registry_path)
    script='''import json,sys,base64,pathlib,subprocess,hashlib
q=json.load(sys.stdin);b=pathlib.Path('/var/lib/virtualwan/runs')/q['run_id'];b.mkdir(mode=0o700,parents=True,exist_ok=True)
paths={'public-policy.py':pathlib.Path('/usr/local/lib/virtualwan-isolation/public-policy.py'),'isolation-service.py':pathlib.Path('/usr/local/sbin/virtualwan-isolation')}
for name,p in paths.items():
 old=p.read_bytes()
 if (b'virtualwan' not in old.lower()) or (b'def generate(' not in old if name=='public-policy.py' else b'pre-start' not in old):raise ValueError('Unrecognized isolation executable')
 data=base64.b64decode(q['sources'][name]['base64'],validate=True)
 if hashlib.sha256(data).hexdigest()!=q['sources'][name]['sha256']:raise ValueError('Digest mismatch')
 compile(data,str(p),'exec')
 backup=b/(name+'.before')
 if not backup.exists():backup.write_bytes(old);backup.chmod(0o600)
 p.write_bytes(data);p.chmod(0o700)
r=subprocess.run(['/usr/local/sbin/virtualwan-isolation','apply','--force'],capture_output=True,text=True)
if r.returncode:raise RuntimeError(r.stderr)
print(json.dumps({'applied':True}))
'''
    own=run('hostname','-s').strip()
    for node,definition in segment['nodes'].items():
        cmd=['python3','-c',script]
        if node!=own:cmd=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',f'HostKeyAlias={node}','-o',f'UserKnownHostsFile=/etc/pve/nodes/{node}/ssh_known_hosts','root@'+definition['address'],shlex.join(cmd)]
        run(*cmd,input=json.dumps({'run_id':q['run_id'],'sources':q['sources']}));audit('isolation.ipv6.node.ready',node=node)
    emit({'ipv6':True,'nodes':list(segment['nodes'])})


def main():
    q = json.load(sys.stdin)
    action = q['action']
    p = q.get('pve', {})
    if action == 'pve_ipv6_prepare':
        pve_ipv6_prepare(q)
    elif action == 'pve_ipv6_allocate':
        pve_ipv6_allocate(q)
    elif action == 'router_bootstrap':
        router_bootstrap(q)
    elif action == 'router_bootstrap_finalize':
        c=json.loads(Path('/etc/virtualwan/virtualwan.json').read_text())
        route=json.loads(run('ip','-j','-4','route','get','1.1.1.1'))[0]
        if c['primary_ip']!=q['primary_ip'] or route.get('prefsrc')!=q['primary_ip'] or route.get('dev')!=c['wan_interface']:
            raise ValueError('Post-reconnect route verification failed; rollback timer remains armed')
        run('systemctl','stop','virtualwan-bootstrap-rollback.timer')
        emit({'reconnected':True,'primary_egress_verified':True})
    elif action == 'vps_inspect':
        c = json.loads(Path(q['path']).read_text())
        if c['primary_ip'] != q['primary_ip']:
            raise ValueError('VPS primary IP mismatch')
        emit({'config': c, 'route': run('ip', '-4', 'route', 'get', '1.1.1.1')})
    elif action == 'vps_idle_ip':
        import base64
        path=Path(q['path']);before=path.read_bytes();c=json.loads(before)
        ip=str(ipaddress.IPv4Address(q['ip']))
        if c['primary_ip']!=q['primary_ip'] or ip==c['primary_ip']:raise ValueError('Primary IP must never be released')
        route=json.loads(run('ip','-j','-4','route','get','1.1.1.1'))[0]
        if route.get('prefsrc')!=q['primary_ip']:raise ValueError('Management source mismatch')
        binary=Path('/usr/local/sbin/virtualwan');original=binary.read_bytes()
        if b'Virtualwan' not in original and b'virtualwan' not in original:raise ValueError('Unknown router executable')
        source=base64.b64decode(q['source_base64'],validate=True)
        if hashlib.sha256(source).hexdigest()!=q['source_sha256']:raise ValueError('Executable digest mismatch')
        backup=Path('/var/lib/virtualwan/runs')/q['run_id'];backup.mkdir(parents=True,mode=0o700,exist_ok=True)
        (backup/'router.before.py').write_bytes(original);(backup/'router.before.json').write_bytes(before)
        for file in backup.iterdir():file.chmod(0o600)
        c['additional_ips']=[x for x in c['additional_ips'] if x!=ip]
        c['inactive_ips']=sorted(set(c.get('inactive_ips',[]))|{ip})
        binary.write_bytes(source);binary.chmod(0o700)
        try:
            path.write_text(json.dumps(c));path.chmod(0o600)
            run(str(binary),'validate');run('systemctl','reload','virtualwan')
            after=json.loads(run('ip','-j','-4','route','get','1.1.1.1'))[0]
            if after.get('dev')!=route['dev'] or after.get('prefsrc')!=q['primary_ip']:raise ValueError('Management route changed')
        except Exception:
            binary.write_bytes(original);binary.chmod(0o700);path.write_bytes(before)
            run('systemctl','reload','virtualwan');raise
        emit({'inactive_ip':ip,'persistent_blackhole':True,'management_unchanged':True})
    elif action in ('pve_retire_check','pve_retire_stop','pve_retire_destroy','pve_retire_unregister','pve_retire_detach'):
        import fcntl,time
        client=q['client'];vmid=int(client['vmid'])
        if vmid==int(p['template']):raise ValueError('Template deletion forbidden')
        registry_path=Path('/etc/pve/virtualwan-isolation.json');registry=json.loads(registry_path.read_text())
        reg=registry.get('segments',{}).get(p['isolation_segment']) if p.get('isolation_segment') else registry
        if not reg:raise ValueError('Unknown client segment')
        registered=next((v for v in reg['clients'] if int(v['vmid'])==vmid),None)
        def matching(v):return v and all(str(v[k]).lower()==str(client[k]).lower() for k in ('vmid','mac','public_ip','next_hop')) and (client.get('network_detached') or not client.get('public_ipv6') or v.get('public_ipv6')==client['public_ipv6'])
        resources=json.loads(run('pvesh','get','/cluster/resources','--type','vm','--output-format','json'))
        vm=next((v for v in resources if int(v['vmid'])==vmid),None)
        manifest=Path('/var/lib/virtualwan-retire')/(str(vmid)+'.json')
        saved=json.loads(manifest.read_text()) if manifest.exists() else None
        detached=bool(saved and saved.get('detached') and matching(saved['client']))
        detach_pending=detached
        if not matching(registered) and (registered or vm) and not (not registered and detached):raise ValueError('Client registration ownership mismatch')
        node=vm['node'] if vm else client.get('node',p['node'])
        if node not in reg['nodes']:raise ValueError('VM outside managed nodes')
        conf=json.loads(run('pvesh','get',f'/nodes/{node}/qemu/{vmid}/config','--output-format','json')) if vm else None
        if conf:
            net=dict(x.split('=',1) for x in conf.get('net0','').split(',') if '=' in x)
            detached=detach_pending and not conf.get('net0')
            if not registered and not detached:raise ValueError('Unregistered VM is not safely detached')
            if detached:
                if conf.get('net0') or conf.get('name')!=saved['config'].get('name') or {k:v for k,v in conf.items() if re.fullmatch(r'(scsi|virtio|sata|ide)[0-9]+',k)}!=saved['disks']:raise ValueError('Detached VM identity changed')
            if conf.get('template') or (not detached and (net.get('virtio','').lower()!=client['mac'].lower() or net.get('bridge')!=reg['nodes'][node]['bridge'])):
                raise ValueError('Actual VM identity differs from recorded client')
            if conf.get('lock'):raise ValueError('VM locked or migrating; retry after operator inspection')
        if vm and action in ('pve_retire_stop','pve_retire_destroy','pve_retire_detach'):
            actual=json.loads(run('pvesh','get',f'/nodes/{node}/qemu/{vmid}/status/current','--output-format','json'))
            vm['status']=actual['status']
        def task(method,path):
            upid=run('pvesh',method,path,'--output-format','json').strip();upid=json.loads(upid)
            deadline=time.monotonic()+150
            while True:
                status=json.loads(run('pvesh','get',f'/nodes/{node}/tasks/{upid}/status','--output-format','json'))
                if status['status']=='stopped':
                    if status.get('exitstatus')!='OK':raise ValueError('Proxmox task failed')
                    return
                if time.monotonic()>deadline:raise ValueError('Proxmox task still running; no automatic retry')
                time.sleep(1)
        if action=='pve_retire_check':emit({'exists':bool(vm),'config':conf,'node':node,'registered':registered,'ipv6':reg.get('ipv6')})
        elif action=='pve_retire_stop':
            if vm and vm['status']!='stopped':task('create',f'/nodes/{node}/qemu/{vmid}/status/stop')
            emit({'stopped':True})
        elif action=='pve_retire_detach':
            if not vm:raise ValueError('VM missing: cannot preserve it')
            if vm['status']!='stopped':raise ValueError('VM must be stopped before network detachment')
            expected_hook=p['snippet_storage']+':snippets/virtualwan-isolation-hook.pl'
            if conf.get('hookscript') not in (None,expected_hook):raise ValueError('Foreign hookscript: operator intervention required')
            if any(re.fullmatch(r'net[1-9][0-9]*',k) for k in conf):raise ValueError('Additional NIC: operator intervention required')
            if not detached:
                manifest.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
                saved={'client':client,'config':conf,'disks':{k:v for k,v in conf.items() if re.fullmatch(r'(scsi|virtio|sata|ide)[0-9]+',k)},'cicustom':conf.get('cicustom',''),'detached':True}
                manifest.write_text(json.dumps(saved));manifest.chmod(0o600)
            remove=','.join(k for k in ('net0','hookscript') if k in conf)
            if remove:run('pvesh','set',f'/nodes/{node}/qemu/{vmid}/config','--delete',remove,'--digest',conf['digest'])
            actual=json.loads(run('pvesh','get',f'/nodes/{node}/qemu/{vmid}/config','--output-format','json'))
            if actual.get('net0') or actual.get('hookscript'):raise ValueError('Network detachment incomplete')
            emit({'detached':True,'vmid':vmid,'disks_preserved':True})
        elif action=='pve_retire_destroy':
            recovery=Path('/var/lib/virtualwan-retire');recovery.mkdir(parents=True,mode=0o700,exist_ok=True)
            manifest=recovery/(str(vmid)+'.json')
            if vm:
                if vm['status']!='stopped':raise ValueError('VM must be stopped before deletion')
                if not detached:
                    manifest.write_text(json.dumps({'client':client,'cicustom':conf.get('cicustom','')}));manifest.chmod(0o600)
                task('delete',f'/nodes/{node}/qemu/{vmid}')
            if manifest.exists():
                saved=json.loads(manifest.read_text())
                if not matching(saved['client']):raise ValueError('Snippet recovery ownership differs')
                refs=dict(part.split('=',1) for part in saved['cicustom'].split(',') if '=' in part)
                directory=Path(p['snippet_directory']).resolve()
                for kind in ('network','user'):
                    name=f'virtualwan-{vmid}-{kind}.yaml'
                    if refs.get(kind)==p['snippet_storage']+':snippets/'+name:
                        target=(directory/name).resolve()
                        if target.parent!=directory:raise ValueError('Snippet outside configured directory')
                        target.unlink(missing_ok=True)
                manifest.unlink()
            emit({'deleted':True,'vmid':vmid,'snippets_cleaned':True})
        else:
            if vm and not (detached and not conf.get('net0')):raise ValueError('VM still connected: keep isolation registration')
            with open('/run/virtualwan-isolation.lock','w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                current=json.loads(registry_path.read_text());segment=current['segments'][p['isolation_segment']] if p.get('isolation_segment') else current
                existing=next((v for v in segment['clients'] if int(v['vmid'])==vmid),None)
                if existing and not matching(existing):raise ValueError('Registration changed during deletion')
                segment['clients']=[v for v in segment['clients'] if int(v['vmid'])!=vmid]
                temp=registry_path.with_suffix('.json.retiring');temp.write_text(json.dumps(current));os.replace(temp,registry_path)
            import shlex
            for n,definition in reg['nodes'].items():
                cmd=['/usr/local/sbin/virtualwan-isolation','apply','--force']
                if n!=run('hostname','-s').strip():cmd=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',f'HostKeyAlias={n}','-o',f'UserKnownHostsFile=/etc/pve/nodes/{n}/ssh_known_hosts','root@'+definition['address'],shlex.join(cmd)]
                run(*cmd)
            emit({'unregistered':True,'vmid':vmid})
    elif action == 'vps_apply':
        import base64
        path=Path(q['path']);before=path.read_bytes();c=json.loads(before)
        ip=str(ipaddress.IPv4Address(q['ip']))
        if c['primary_ip']!=q['primary_ip'] or ip==q['primary_ip']:raise ValueError('Additional IP or primary mismatch')
        prior_route=run('ip','-4','route','get','1.1.1.1')
        if f'src {q["primary_ip"]}' not in prior_route:raise ValueError('Management source mismatch')
        backup=Path('/var/lib/virtualwan/runs')/q['run_id'];backup.mkdir(parents=True,mode=0o700,exist_ok=False)
        (backup/'vps-config.before.json').write_bytes(before);(backup/'vps-config.before.json').chmod(0o600)
        binary=Path('/usr/local/sbin/virtualwan');original=None;source=None
        if q.get('source_base64'):
            original=binary.read_bytes()
            if b'def firewall(' not in original or b'wg-vwan' not in original or b'virtualwan' not in original:raise ValueError('Unknown router executable')
            source=base64.b64decode(q['source_base64'],validate=True)
            if hashlib.sha256(source).hexdigest()!=q['source_sha256']:raise ValueError('Router executable digest mismatch')
            compile(source.decode('utf-8'),str(binary),'exec')
            (backup/'router.before.py').write_bytes(original);(backup/'router.before.py').chmod(0o600)
        if ip not in c['additional_ips']:c['additional_ips'].append(ip)
        if 'inactive_ips' in c:c['inactive_ips']=[x for x in c['inactive_ips'] if x!=ip]
        fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.virtualwan-')
        try:
            with os.fdopen(fd,'w') as f:json.dump(c,f);f.write('\n')
            if source is not None:
                binary.write_bytes(source);binary.chmod(0o700)
                audit('vps.router.updated',source_sha256=q['source_sha256'],backup=str(backup/'router.before.py'))
            run(str(binary),'validate','--config',temp)
            audit('vps.config.replace.begin',path=str(path),before=json.loads(before),after=c)
            os.replace(temp,path);audit('vps.config.replace.end',path=str(path))
            reload_virtualwan()
            route=run('ip','-4','route','get','1.1.1.1')
            if route!=prior_route:
                if f'src {q["primary_ip"]}' not in route:raise ValueError('Management source changed')
        except Exception:
            audit('vps.rollback.begin',path=str(path))
            if original is not None:binary.write_bytes(original);binary.chmod(0o700)
            path.write_bytes(before);reload_virtualwan();audit('vps.rollback.end',path=str(path))
            raise
        finally:
            if os.path.exists(temp):os.unlink(temp)
        emit({'config':c,'route':route,'backup':str(backup),'router_updated':source is not None})
    elif action == 'pve_inventory':
        assert run('hostname','-s').strip()==p['node']
        registry=json.loads(Path('/etc/pve/virtualwan-isolation.json').read_text())
        reg=registry.get('segments',{}).get(p['isolation_segment']) if p.get('isolation_segment') else registry
        if not reg:raise ValueError('Unknown segment')
        resources={v['vmid']:v for v in json.loads(run('pvesh','get','/cluster/resources','--type','vm','--output-format','json'))}
        clients=[]
        for client in reg['clients']:
            resource=resources.get(client['vmid'])
            if not resource:continue
            node=resource['node']
            conf=json.loads(run('pvesh','get',f'/nodes/{node}/qemu/{client["vmid"]}/config','--output-format','json'))
            match=re.search(r'virtio=([0-9A-Fa-f:]{17})',conf.get('net0',''))
            if not match or match[1].lower()!=client['mac'].lower():raise ValueError('Registered MAC differs from actual VM')
            clients.append({**client,**({'ipv6_config':reg.get('ipv6')} if client.get('public_ipv6') else {}),'node':node,'name':conf.get('name'),'status':resource['status'],'memory_mb':conf.get('memory'),'cores':conf.get('cores'),'bridge':reg['nodes'][node]['bridge']})
        emit({'clients':clients})
    elif action == 'pve_inspect':
        assert run('hostname', '-s').strip() == p['node'], 'SSH host and requested PVE node differ'
        resources = json.loads(run('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'))
        template = api(p, f'qemu/{p["template"]}/config')
        assert template.get('template') == 1
        assert int(template.get('memory', 0)) > 0
        assert Path('/sys/class/net', p['bridge']).is_dir()
        storage = {s['storage']: s for s in api(p, 'storage')}
        for name, content in ((p['disk_storage'], 'images'), (p['snippet_storage'], 'snippets')):
            assert storage[name]['active'] and content in storage[name]['content'].split(',')
        assert Path(p['snippet_directory']).is_dir()
        isolation=None
        registry=Path('/etc/pve/virtualwan-isolation.json')
        if registry.exists():
            reg=json.loads(registry.read_text());nodes={}
            if p.get('isolation_segment'):
                reg=reg.get('segments',{}).get(p['isolation_segment'])
                if not reg:raise ValueError('Isolation segment not bootstrapped')
            table='virtualwan_isolation_'+hashlib.sha256(p['isolation_segment'].encode()).hexdigest()[:12] if p.get('isolation_segment') else 'virtualwan_isolation'
            assert reg['nodes'][p['node']]['bridge']==p['bridge'],'Isolation bridge differs from VM bridge'
            for node,definition in reg['nodes'].items():
                check=['sh','-c','systemctl is-active --quiet virtualwan-isolation.service && nft list table bridge '+table+' >/dev/null && test -x /usr/local/sbin/virtualwan-isolation && printf active']
                if node!=p['node']:
                    import shlex
                    check=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',f'HostKeyAlias={node}','-o',f'UserKnownHostsFile=/etc/pve/nodes/{node}/ssh_known_hosts','root@'+definition['address'],shlex.join(check)]
                nodes[node]=run(*check).strip()
            isolation={'nodes':nodes,'active':all(value=='active' for value in nodes.values())}
        emit({'resources': resources, 'template': template, 'storage': storage, 'isolation': isolation})
    elif action == 'pve_create':
        vmid = str(p['vmid'])
        marker = 'Virtualwan managed: ' + q['fqdn']
        resources = json.loads(run('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'))
        if any(v['vmid'] == p['vmid'] for v in resources):
            cfg = api(p, f'qemu/{vmid}/config')
            if cfg.get('description') != marker:
                raise ValueError('Existing VM ID is not owned by this provisioning target')
            mac = re.search(r'virtio=([0-9A-Fa-f:]{17})', cfg['net0']).group(1)
            expected_refs = ','.join(f'{s}={p["snippet_storage"]}:snippets/virtualwan-{vmid}-{s}.yaml' for s in ('network', 'user'))
            if cfg.get('cicustom') != expected_refs or f'bridge={p["bridge"]}' not in cfg['net0'] or int(cfg['memory']) != p['memory_mb'] or int(cfg['cores']) != p['cores']:
                raise ValueError('Existing VM is incomplete or drifted: inspect before resuming')
            network = json.loads((Path(p['snippet_directory']) / f'virtualwan-{vmid}-network.yaml').read_text())
            if network['config'][0]['mac_address'] != mac.lower() or network['config'][0]['subnets'][0]['address'] != q['ip'] + '/32':
                raise ValueError('Existing VM network snippet conflicts')
            if q.get('ipv6') and network!=ipv6_network_data(q['guest'],q['ip'],mac,q['ipv6']):raise ValueError('Existing dual-stack snippet differs')
            register_isolation(p,q,vmid,mac)
            emit({'existing': True, 'mac': mac, 'config': api(p, f'qemu/{vmid}/config')})
            return
        run('qm', 'clone', str(p['template']), vmid, '--full', '1', '--storage', p['disk_storage'], '--name', q['fqdn'])
        run('qm', 'set', vmid, '--description', marker)
        cfg = api(p, f'qemu/{vmid}/config')
        mac = re.search(r'virtio=([0-9A-Fa-f:]{17})', cfg['net0']).group(1)
        g = q['guest']
        auth = g['ssh_auth']
        user = {'name': g['username'], 'shell': '/bin/bash', 'groups': ['sudo'], 'sudo': ['ALL=(ALL) NOPASSWD:ALL'], 'lock_passwd': auth == 'key'}
        if auth in ('key', 'both'):
            user['ssh_authorized_keys'] = [q['public_key']]
        if auth in ('password', 'both'):
            user['hashed_passwd'] = run('openssl', 'passwd', '-6', '-stdin', input=q['guest_password'] + '\n').strip()
        user_data = {'users': [user], 'disable_root': True, 'ssh_pwauth': auth in ('password', 'both'), 'chpasswd': {'expire': False}, 'fqdn': q['fqdn'], 'hostname': q['fqdn'], 'prefer_fqdn_over_hostname': True}
        network=ipv6_network_data(g,q['ip'],mac,q.get('ipv6'))
        if q.get('ipv6'):user_data['write_files']=ipv6_cloud_files(g['interface'])
        refs = []
        for suffix, data in (('network', json.dumps(network, indent=2)), ('user', '#cloud-config\n' + json.dumps(user_data, indent=2))):
            name = f'virtualwan-{vmid}-{suffix}.yaml'
            audit('snippet.create.begin', path=str(Path(p['snippet_directory']) / name), content_sha256=hashlib.sha256(data.encode()).hexdigest(), sensitive=suffix == 'user')
            with open(Path(p['snippet_directory']) / name, 'x') as f:
                f.write(data + '\n')
            audit('snippet.create.end', path=str(Path(p['snippet_directory']) / name))
            refs.append(f'{suffix}={p["snippet_storage"]}:snippets/{name}')
        run('qm', 'set', vmid, '--net0', f'virtio={mac},bridge={p["bridge"]}', '--cores', str(p['cores']), '--memory', str(p['memory_mb']), '--ciuser', g['username'], '--tags', 'linux;virtualwan', '--cicustom', ','.join(refs))
        run('qm', 'resize', vmid, 'scsi0', str(p['disk_gb']) + 'G')
        run('qm', 'cloudinit', 'update', vmid)
        register_isolation(p,q,vmid,mac)
        emit({'mac': mac, 'config': api(p, f'qemu/{vmid}/config'), 'stopped': True})
    elif action in ('pve_start', 'pve_reboot'):
        vmid = str(p['vmid'])
        status = api(p, f'qemu/{vmid}/status/current')['status']
        if action == 'pve_reboot':
            run('qm', 'reboot', vmid, '--timeout', '60')
        elif status == 'stopped':
            run('qm', 'start', vmid)
        emit({'status': api(p, f'qemu/{vmid}/status/current')['status']})
    elif action == 'pve_verify':
        vmid = str(p['vmid'])
        # Query addresses and routing through QGA, not untrusted user startup code.
        code = "import json,subprocess; r=lambda *a:subprocess.check_output(a,text=True); print(json.dumps({'addresses':json.loads(r('ip','-j','-4','addr')), 'routes':json.loads(r('ip','-j','-4','route')), 'dns':open('/etc/resolv.conf').read(), 'hostname':r('hostname').strip(), 'ssh_fingerprint':r('ssh-keygen','-lf','/etc/ssh/ssh_host_ed25519_key.pub').strip(), 'egress':r('curl','-4','-fsS','--max-time','15','https://api.ipify.org').strip()}))"
        result = json.loads(run('qm', 'guest', 'exec', vmid, '--', 'python3', '-c', code))
        if not result.get('exited') or result.get('exitcode') != 0:
            raise RuntimeError('Guest not ready or verification failed')
        value=json.loads(result['out-data'])
        if q.get('ipv6'):
            code="import subprocess,json;r=lambda *a:subprocess.check_output(a,text=True);print(json.dumps({'addresses':json.loads(r('ip','-j','-6','addr')),'routes':json.loads(r('ip','-j','-6','route')),'egress':r('curl','-6','-fsS','--max-time','15','https://api64.ipify.org').strip()}))"
            v6=json.loads(run('qm','guest','exec',vmid,'--','python3','-c',code))
            if not v6.get('exited') or v6.get('exitcode')!=0:raise RuntimeError('Guest IPv6 not ready')
            value['ipv6']=json.loads(v6['out-data'])
        emit(value)
    else:
        raise ValueError('Unknown remote action')


if __name__ == '__main__':
    main()
