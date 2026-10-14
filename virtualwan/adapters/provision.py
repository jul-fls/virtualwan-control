#!/usr/bin/env python3
"""Virtualwan provisioning: plan, read-only inspect, then explicit apply."""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import time
from urllib.parse import quote
import uuid
import threading
import getpass

from cryptography.fernet import Fernet
import dns.resolver
import paramiko
import requests

from .pfsense import PfSense


def secret(name):
    if not name or not os.environ.get(name):
        raise ValueError('Missing secret environment variable: ' + str(name))
    return os.environ[name]


def fqdn_for(ip, domain, pattern='vps-{ipv4_compact}.{domain}'):
    a = ipaddress.IPv4Address(ip)
    if not a.is_global or a.is_multicast:
        raise ValueError('Expected a global unicast IPv4')
    if not re.fullmatch(r'[a-z0-9.-]+', domain) or '..' in domain:
        raise ValueError('Invalid domain')
    name=pattern.format(ipv4=str(a),ipv4_compact=str(a).replace('.',''),ipv4_dashed=str(a).replace('.','-'),domain=domain).lower()
    if len(name)>253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) for part in name.split('.')):
        raise ValueError('Invalid generated DNS hostname')
    return name


class Journal:
    @classmethod
    def reopen(cls, directory, cfg):
        obj = cls.__new__(cls)
        obj.directory = Path(directory)
        entries = [json.loads(line) for line in (obj.directory / 'run.jsonl').read_text(encoding='utf-8').splitlines()]
        obj.run_id = entries[0]['run_id']
        if obj.directory.name != obj.run_id or any(e['run_id'] != obj.run_id for e in entries):
            raise ValueError('Run journal identity mismatch')
        obj.sequence = max(e['sequence'] for e in entries)
        obj.secrets = [os.environ[v] for section in cfg.values() if isinstance(section, dict) for k,v in section.items() if k.endswith('_env') and isinstance(v, str) and v in os.environ]
        obj.key_env = cfg['backup_key_env']
        if os.environ.get(obj.key_env):
            obj.secrets.append(os.environ[obj.key_env])
        return obj

    def __init__(self, cfg):
        self.run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        self.directory = Path(cfg['log_directory']) / self.run_id
        self.directory.mkdir(parents=True, mode=0o700)
        self.secrets = [v for k, v in os.environ.items() if k in {str(v) for obj in cfg.values() if isinstance(obj, dict) for key, v in obj.items() if key.endswith('_env')} | {cfg['backup_key_env']} and v]
        self.key_env = cfg['backup_key_env']
        self.sequence = 0

    def clean(self, value):
        if isinstance(value, dict):
            return {k: ('[REDACTED]' if re.search(r'password|passwd|secret|token|csrf|cookie|authorization|hashed_passwd', k, re.I) and not k.endswith('_env') else self.clean(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for s in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(s, '[REDACTED]').replace(quote(s, safe=''), '[REDACTED]')
            return value
        return value

    def event(self, action, **data):
        self.sequence += 1
        entry = {'sequence': self.sequence, 'time': datetime.now(timezone.utc).isoformat(), 'run_id': self.run_id, 'action': action, **self.clean(data)}
        with open(self.directory / 'run.jsonl', 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        print(f'[{self.sequence}] {action}', flush=True)

    def backup(self, name, data):
        cipher = Fernet(secret(self.key_env))
        path = self.directory / (f'{self.sequence:04d}-' + name + '.fernet')
        with open(path, 'xb') as f:
            f.write(cipher.encrypt(data))
        os.chmod(path, 0o600)
        self.event('backup.saved', path=str(path), plaintext_sha256=hashlib.sha256(data).hexdigest())


def require_ssh_host_key(cfg):
    path=Path(cfg['known_hosts'])
    if not path.is_file():
        raise ValueError('Clés hôtes SSH absentes. Dans Routeurs → Configurer, utiliser « Vérifier clé SSH PVE » ou « Vérifier clé SSH routeur », comparer l’empreinte avec la console du serveur puis l’enregistrer.')
    name=cfg['host'] if int(cfg.get('port',22))==22 else f"[{cfg['host']}]:{cfg['port']}"
    hosts=paramiko.HostKeys(str(path))
    if not hosts.lookup(name):
        raise ValueError('Clé SSH non enregistrée pour '+name+'. Dans Routeurs → Configurer, vérifier puis enregistrer la clé SSH du serveur cible.')


class Remote:
    def __init__(self, cfg, journal):
        self.cfg, self.log = cfg, journal
        require_ssh_host_key(cfg)
        self.ssh = paramiko.SSHClient()
        self.ssh.load_host_keys(cfg['known_hosts'])
        self.ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.ssh.connect(cfg['host'], port=cfg['port'], username=cfg['username'], password=secret(cfg['password_env']) if not cfg.get('private_key') else None, key_filename=cfg.get('private_key'), passphrase=secret(cfg['key_passphrase_env']) if cfg.get('key_passphrase_env') else None, look_for_keys=False, allow_agent=False, timeout=15, banner_timeout=15)
        self.log.event('ssh.connected', host=cfg['host'], port=cfg['port'], username=cfg['username'], key_type=self.ssh.get_transport().get_remote_server_key().get_name())
        self.sftp = self.ssh.open_sftp()
        self.path = '/tmp/virtualwan-provision-' + journal.run_id + '.py'
        self.log.event('ssh.script.upload.begin', host=cfg['host'], path=self.path)
        with self.sftp.file(self.path, 'wx') as f:
            f.write(Path(__file__).with_name('remote.py').read_bytes())
        self.sftp.chmod(self.path, 0o600)
        self.log.event('ssh.script.upload.end', host=cfg['host'], path=self.path)
        if cfg.get('sudo'):
            self.log.event('ssh.sudo.check.begin', host=cfg['host'])
            _, output, errors = self.ssh.exec_command('sudo -n true', timeout=15)
            rc = output.channel.recv_exit_status()
            if rc:
                auth_in, auth_out, auth_err = self.ssh.exec_command("sudo -S -p '' -v", timeout=15)
                auth_in.write(secret(cfg['sudo_password_env']) + '\n')
                auth_in.flush()
                auth_in.channel.shutdown_write()
                if auth_out.channel.recv_exit_status():
                    raise RuntimeError('sudo authentication failed')
            self.log.event('ssh.sudo.check.end', host=cfg['host'])

    def call(self, action, **payload):
        self.log.event('remote.begin', host=self.cfg['host'], operation=action, parameters=payload)
        command = 'python3 ' + shlex.quote(self.path)
        if self.cfg.get('sudo'):
            command = 'sudo -n ' + command
        stdin, stdout, stderr = self.ssh.exec_command(command, timeout=360)
        stdin.write(json.dumps({'action': action, 'run_id': self.log.run_id, **payload}))
        stdin.flush()
        stdin.channel.shutdown_write()
        errors = []
        def read_events():
            for line in stderr:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    errors.append(line)
                else:
                    self.log.event('remote.step', host=self.cfg['host'], operation=action, details=event)
        reader = threading.Thread(target=read_events)
        reader.start()
        result = stdout.read().decode()
        reader.join()
        errors = ''.join(errors)
        rc = stdout.channel.recv_exit_status()
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            parsed = None
        self.log.event('remote.end', host=self.cfg['host'], operation=action, exit_code=rc, result=parsed, stderr=errors)
        if rc:
            raise RuntimeError('Remote operation failed: ' + action)
        if parsed is None:
            raise RuntimeError('Remote returned invalid JSON: ' + action)
        return parsed

    def close(self):
        try:
            self.log.event('ssh.script.remove.begin', host=self.cfg['host'], path=self.path)
            self.sftp.remove(self.path)
            self.log.event('ssh.script.remove.end', host=self.cfg['host'], path=self.path)
        finally:
            self.sftp.close()
            self.ssh.close()
            self.log.event('ssh.closed', host=self.cfg['host'])


class Cloudflare:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.s = requests.Session()
        self.s.headers['Authorization'] = 'Bearer ' + secret(cfg['token_env'])
        self.base = 'https://api.cloudflare.com/client/v4/zones/' + cfg['zone_id'] + '/dns_records'

    def call(self, method, suffix='', **kwargs):
        self.log.event('cloudflare.begin', method=method, suffix=suffix, data=kwargs)
        r = self.s.request(method, self.base + suffix, timeout=30, **kwargs)
        data = r.json()
        self.log.event('cloudflare.end', status=r.status_code, result=data)
        r.raise_for_status()
        if not data.get('success'):
            raise RuntimeError('Cloudflare API refused request')
        return data['result']

    def inspect(self, fqdn, ip):
        # Include CNAME and AAAA conflicts; no half-working IPv6 is published.
        records = self.call('GET', params={'name': fqdn, 'per_page': 100})
        for r in records:
            if r['type'] != 'A' or r['content'] != ip:
                raise ValueError('Existing DNS name conflicts with requested public IPv4')
        if len(records) > 1:
            raise ValueError('Multiple DNS records on managed hostname')
        return records

    def ensure(self, fqdn, ip):
        records = self.inspect(fqdn, ip)
        data = {'type': 'A', 'name': fqdn, 'content': ip, 'ttl': self.cfg.get('ttl', 300), 'proxied': False, 'comment': 'Virtualwan managed'}
        if not records:
            self.call('POST', json=data)
        elif records[0].get('proxied'):
            self.call('PATCH', '/' + records[0]['id'], json={'proxied': False})
        verified = self.inspect(fqdn, ip)
        if not verified or verified[0].get('proxied'):
            raise RuntimeError('Cloudflare DNS readback failed')


class Ovh:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.base = cfg['endpoint'].rstrip('/')
        if self.base not in ('https://eu.api.ovh.com/1.0', 'https://ca.api.ovh.com/1.0', 'https://us.api.ovh.com/1.0'):
            raise ValueError('Unsupported OVH API endpoint')
        self.ak, self.sk, self.ck = (secret(cfg[x]) for x in ('application_key_env', 'application_secret_env', 'consumer_key_env'))
        r = requests.get(self.base + '/auth/time', timeout=20)
        r.raise_for_status()
        self.offset = int(r.json()) - int(time.time())

    def call(self, method, path, data=None, allow404=False):
        url = self.base + path
        body = '' if data is None else json.dumps(data, separators=(',', ':'))
        timestamp = str(int(time.time()) + self.offset)
        signature = '$1$' + hashlib.sha1('+'.join((self.sk, self.ck, method, url, body, timestamp)).encode()).hexdigest()
        headers = {'X-Ovh-Application': self.ak, 'X-Ovh-Consumer': self.ck, 'X-Ovh-Timestamp': timestamp, 'X-Ovh-Signature': signature, 'Content-Type': 'application/json'}
        self.log.event('ovh.begin', method=method, path=path, data=data)
        r = requests.request(method, url, headers=headers, data=body, timeout=30)
        self.log.event('ovh.end', status=r.status_code, response=r.text)
        if allow404 and r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json() if r.content else None

    def inspect(self, ip, require_service=True):
        block = self.cfg.get('ip_block')
        if not block:
            blocks = self.call('GET', '/ip')
            candidates = [b for b in blocks if ipaddress.ip_address(ip) in ipaddress.ip_network(b, strict=False)]
            if not candidates:
                raise ValueError('IP not owned by this OVH account')
            block = max(candidates, key=lambda b: ipaddress.ip_network(b).prefixlen)
        if ipaddress.ip_address(ip) not in ipaddress.ip_network(block, strict=False):
            raise ValueError('IP outside configured OVH block')
        self.block = quote(block, safe='')
        info = self.call('GET', '/ip/' + self.block)
        expected = self.cfg.get('expected_service')
        self.log.event('ovh.ip.routing', block=block, routed_to=info.get('routedTo'))
        if not expected and self.cfg.get('primary_ip'):
            candidate = (info.get('routedTo') or {}).get('serviceName')
            if candidate:
                ips = self.call('GET', '/vps/' + quote(candidate, safe='') + '/ips')
                if self.cfg['primary_ip'] in ips:
                    expected = candidate
                    self.cfg['expected_service'] = expected
                    self.log.event('ovh.vps.verified', service=expected, primary_ip=self.cfg['primary_ip'])
        if (expected and (info.get('routedTo') or {}).get('serviceName') != expected) or (require_service and not expected):
            raise ValueError('Set ovh.expected_service to the verified VPS service name; route mismatch or missing value')
        self.path = '/ip/' + self.block + '/reverse/' + quote(ip, safe='')
        return self.call('GET', self.path, allow404=True)

    def ensure(self, ip, fqdn):
        old = self.call('GET', self.path, allow404=True)
        if old and old.get('reverse', '').rstrip('.') == fqdn:
            return
        self.log.backup('ovh-reverse.before.json', json.dumps(old).encode())
        if old:
            self.call('DELETE', self.path)
        try:
            self.call('POST', '/ip/' + self.block + '/reverse', {'ipReverse': ip, 'reverse': fqdn})
        except Exception:
            self.log.event('ovh.reverse.compensation.begin')
            current = self.call('GET', self.path, allow404=True)
            if current is None and old:
                self.call('POST', '/ip/' + self.block + '/reverse', {'ipReverse': ip, 'reverse': old['reverse']})
            raise
        new = self.call('GET', self.path)
        if new.get('reverse', '').rstrip('.') != fqdn:
            raise RuntimeError('OVH reverse readback failed')


def wait_dns(fqdn, ip, log, timeout=180):
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = ['1.1.1.1', '9.9.9.9']
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            answer = {str(x) for x in resolver.resolve(fqdn, 'A', lifetime=5)}
            log.event('dns.check', name=fqdn, answers=sorted(answer))
            if answer == {ip}:
                return
        except dns.exception.DNSException as e:
            log.event('dns.wait', error=type(e).__name__)
        time.sleep(5)
    raise TimeoutError('DNS propagation pending; stop before changing OVH reverse')


def verify_guest(value, cfg, fqdn, log):
    ips = {a['local'] for dev in value['addresses'] if dev['ifname'] != 'lo' for a in dev.get('addr_info', [])}
    if ips != {cfg['public_ip']} or value['egress'] != cfg['public_ip'] or value['hostname'] != fqdn:
        raise ValueError('Guest IP, hostname or public egress does not match target')
    default = [r for r in value['routes'] if r['dst'] == 'default']
    if len(default) != 1 or default[0].get('gateway') != cfg['guest']['gateway']:
        raise ValueError('Guest default route mismatch')
    if not all('nameserver ' + address in value['dns'] for address in cfg['guest']['dns']):
        raise ValueError('Guest DNS mismatch')
    log.event('guest.network.verified', result=value)


def orchestrate(cfg, mode, log, save_config=None):
    ip = cfg['public_ip']
    fqdn = fqdn_for(ip, cfg['domain'],cfg.get('hostname_pattern','vps-{ipv4_compact}.{domain}'))
    p = cfg['pve']
    g = cfg['guest']
    if g['ssh_auth'] not in ('key', 'password', 'both'):
        raise ValueError('guest.ssh_auth must be key/password/both')
    if g['username'] == 'root' or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', g['username']):
        raise ValueError('Use a non-root Linux username')
    if p.get('vmid') is not None and (not 100 <= int(p['vmid']) <= 999999999 or int(p['vmid']) == int(p['template'])):
        raise ValueError('Configure a valid VM ID')
    for name in ('cores', 'memory_mb', 'disk_gb'):
        if not isinstance(p[name], int) or p[name] <= 0:
            raise ValueError('Invalid PVE resource: ' + name)
    public_key = ''
    if g['ssh_auth'] in ('key', 'both'):
        public_key = Path(g['ssh_public_key_file']).read_text().strip()
        if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+(?: [^\r\n]*)?', public_key):
            raise ValueError('Expected one SSH PUBLIC key')
    log.event('plan', fqdn=fqdn, public_ip=ip, configuration=cfg, steps=['preflight', 'clone stopped', 'Cloudflare A DNS-only', 'DNS propagation', 'OVH reverse', 'VPS route', 'pfSense forms', 'VM start and verify', 'optional reboot'])
    if mode == 'plan':
        return
    Fernet(secret(cfg['backup_key_env']))
    vps = pve = None
    try:
        pf = PfSense(cfg['pfsense'], log, secret)
        pf.login()
        root = pf.snapshot('pfsense.before')
        pf.preflight(root, g, ip)
        cf = Cloudflare(cfg['cloudflare'], log)
        cf.inspect(fqdn, ip)
        cfg['ovh']['primary_ip'] = cfg['vps']['primary_ip']
        ovh = Ovh(cfg['ovh'], log)
        ovh.inspect(ip, require_service=mode == 'apply')
        vps = Remote(cfg['vps'], log)
        before = vps.call('vps_inspect', path=cfg['vps']['config_path'], primary_ip=cfg['vps']['primary_ip'])
        log.backup('vps.before.json', json.dumps(before).encode())
        pve = Remote(p, log)
        inventory = pve.call('pve_inspect', pve=p)
        if p.get('vmid') is None:
            occupied = {v['vmid'] for v in inventory['resources']}
            p['vmid'] = next(i for i in range(p.get('vmid_start', 120), 999999999) if i not in occupied)
            log.event('vmid.allocated', vmid=p['vmid'])
        if save_config:
            save_config(cfg)
        log.event('preflight.complete', inventory=inventory)
        if mode == 'inspect':
            return
        if cfg.get('start_vm') and cfg.get('require_client_isolation', True) and not ((inventory.get('isolation') or {}).get('active')):
            raise ValueError('Client isolation service/table is missing on at least one registered PVE node. Restore isolation before starting a client.')
        guest_password = secret(g['password_env']) if g['ssh_auth'] in ('password', 'both') else ''
        if '\n' in guest_password:
            raise ValueError('Guest password cannot contain newline')
        vm = pve.call('pve_create', pve=p, guest=g, ip=ip, fqdn=fqdn, public_key=public_key, guest_password=guest_password,next_hop=pf.cfg['next_hop'])
        cf.ensure(fqdn, ip)
        wait_dns(fqdn, ip, log)
        ovh.ensure(ip, fqdn)
        vps.call('vps_apply', path=cfg['vps']['config_path'], primary_ip=cfg['vps']['primary_ip'], ip=ip)
        pf.configure(pf.snapshot('pfsense.pre-change'), ip, fqdn, vm['mac'])
        if not cfg.get('start_vm'):
            log.event('provisioned.stopped', vmid=p['vmid'], fqdn=fqdn)
            return
        pve.call('pve_start', pve=p)
        for cycle in range(2 if cfg.get('reboot_test') else 1):
            if cycle:
                pve.call('pve_reboot', pve=p)
                time.sleep(10)
            deadline = time.monotonic() + 180
            while True:
                try:
                    verify_guest(pve.call('pve_verify', pve=p), cfg, fqdn, log)
                    break
                except (RuntimeError, ValueError) as e:
                    if time.monotonic() >= deadline:
                        raise
                    log.event('guest.wait', error=str(e))
                    time.sleep(5)
            with socket.create_connection((ip, 22), timeout=10) as connection:
                banner = connection.recv(128).decode(errors='replace')
                if not banner.startswith('SSH-'):
                    raise ValueError('Unexpected SSH banner')
                log.event('guest.ssh.reachable', cycle=cycle, banner=banner, probe_origin='orchestrator host; external origin requires separate verification')
        log.event('deployment.complete', vmid=p['vmid'], fqdn=fqdn, ip=ip)
    finally:
        for remote in (pve, vps):
            if remote:
                remote.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['plan', 'inspect', 'apply'])
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--prompt-passwords', action='store_true')
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding='utf-8-sig'))
    if args.prompt_passwords:
        for item in ('pve', 'vps'):
            connection = cfg[item]
            if not connection.get('private_key') and not os.environ.get(connection['password_env']):
                os.environ[connection['password_env']] = getpass.getpass(item + ' SSH password: ')
    log = Journal(cfg)
    lock = None
    try:
        if args.mode == 'apply':
            ipaddress.IPv4Address(cfg['public_ip'])
            name = 'lock-firewall-' + hashlib.sha256(cfg['pfsense']['url'].encode()).hexdigest()[:16]
            lock = Path(cfg['log_directory']) / name
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, log.run_id.encode())
            os.close(fd)
        orchestrate(cfg, args.mode, log, save_config=lambda state: args.config.write_text(json.dumps(state, indent=2), encoding='utf-8'))
        log.event('run.success', mode=args.mode)
        return 0
    except Exception as e:
        log.event('run.failed', error_type=type(e).__name__, error=str(e), recovery='See successful actions and encrypted backups. No broad automatic rollback or deletion of VM/DNS/pfSense objects.')
        return 1
    finally:
        if lock and lock.exists() and lock.read_text() == log.run_id:
            lock.unlink()
        print('Run journal: ' + str(log.directory / 'run.jsonl'))


if __name__ == '__main__':
    raise SystemExit(main())
