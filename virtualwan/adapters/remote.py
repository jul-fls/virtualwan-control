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


def api(p, resource):
    return json.loads(run('pvesh', 'get', f'/nodes/{p["node"]}/{resource}', '--output-format', 'json'))


def emit(value):
    print(json.dumps(value))


def register_isolation(p, q, vmid, mac):
    if Path('/etc/pve/virtualwan-isolation.json').exists():
        extra=['--segment',p['isolation_segment']] if p.get('isolation_segment') else []
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


def main():
    q = json.load(sys.stdin)
    action = q['action']
    p = q.get('pve', {})
    if action == 'router_bootstrap':
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
    elif action == 'vps_apply':
        path = Path(q['path'])
        before = path.read_bytes()
        c = json.loads(before)
        assert c['primary_ip'] == q['primary_ip']
        ipaddress.IPv4Address(q['ip'])
        prior_route = run('ip', '-4', 'route', 'get', '1.1.1.1')
        assert f'src {q["primary_ip"]}' in prior_route
        if q['ip'] in c['additional_ips']:
            emit({'unchanged': True, 'config': c, 'route': prior_route})
            return
        backup = Path('/var/lib/virtualwan/runs') / q['run_id']
        backup.mkdir(parents=True, mode=0o700, exist_ok=False)
        (backup / 'vps-config.before.json').write_bytes(before)
        os.chmod(backup / 'vps-config.before.json', 0o600)
        audit('vps.backup.saved', path=str(backup / 'vps-config.before.json'))
        c['additional_ips'].append(q['ip'])
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.virtualwan-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(c, f, indent=2)
                f.write('\n')
            run('virtualwan', 'validate', '--config', temp)
            audit('vps.config.replace.begin', path=str(path), before=json.loads(before), after=c)
            os.replace(temp, path)
            audit('vps.config.replace.end', path=str(path))
            try:
                run('systemctl', 'reload', 'virtualwan')
                route = run('ip', '-4', 'route', 'get', '1.1.1.1')
                assert f'src {q["primary_ip"]}' in route
            except Exception:
                audit('vps.rollback.begin', path=str(path))
                path.write_bytes(before)
                run('systemctl', 'reload', 'virtualwan')
                audit('vps.rollback.end', path=str(path))
                raise
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        emit({'config': c, 'route': route, 'backup': str(backup)})
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
            clients.append({**client,'node':node,'name':conf.get('name'),'status':resource['status'],'memory_mb':conf.get('memory'),'cores':conf.get('cores'),'bridge':reg['nodes'][node]['bridge']})
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
        network = {'version': 1, 'config': [{'type': 'physical', 'name': g['interface'], 'mac_address': mac.lower(), 'mtu': g['mtu'], 'subnets': [{'type': 'static', 'address': q['ip'] + '/32', 'pointopoint': g['gateway'], 'gateway': g['gateway'], 'dns_nameservers': g['dns']}]}]}
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
        emit(json.loads(result['out-data']))
    else:
        raise ValueError('Unknown remote action')


if __name__ == '__main__':
    main()
