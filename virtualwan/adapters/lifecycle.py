"""Explicit client retirement and stock DNS; never terminates an OVH subscription."""
import base64
import hashlib
import json
from pathlib import Path
import sys
from .provision import Journal,Remote,Cloudflare,Ovh,secret,wait_dns
from .pfsense import PfSense
from ..hostnames import stock_hostname


def main():
    path=Path(sys.argv[1]);cfg=json.loads(path.read_text());log=Journal(cfg)
    ip=cfg['public_ip'];client=cfg.get('retire_client');mode=cfg.get('lifecycle_mode','delete_release')
    if mode not in ('release','delete','delete_release'):raise ValueError('Unknown lifecycle mode')
    release=not client or mode!='delete'
    fqdn=stock_hostname(ip,cfg['domain'],cfg.get('hostname_pattern','vps-{ipv4_compact}.{domain}')) if ip else None
    cf=ovh=None
    if release:
        cf=Cloudflare(cfg['cloudflare'],log);cfg['ovh']['primary_ip']=cfg['vps']['primary_ip']
        ovh=Ovh(cfg['ovh'],log);ovh.inspect(ip)
    pve=vps=None
    try:
        if not client:
            # A sync must never turn an unimported existing client into unused stock.
            pve=Remote(cfg['pve'],log);inventory=pve.call('pve_inventory',pve=cfg['pve'])
            if any(v['public_ip']==ip for v in inventory['clients']):raise ValueError('IP déjà affectée dans le registre PVE : utiliser Lire les VM avant de préparer le stock.')
            pf=PfSense(cfg['pfsense'],log,secret);pf.login()
            if any(r.findtext('network')==ip+'/32' for r in pf.snapshot('stock.preflight').findall('staticroutes/route')):
                raise ValueError('Route pfSense existante pour cette IP ; elle ne peut pas être classée libre automatiquement.')
        if client:
            pve=Remote(cfg['pve'],log)
            before=pve.call('pve_retire_check',pve=cfg['pve'],client=client)
            log.backup('vm.before.json',json.dumps(before).encode())
            if before.get('exists'):pve.call('pve_retire_stop',pve=cfg['pve'],client=client)
        if ip and (client or cfg.get('stock_routing_ready',True)):
            vps=Remote(cfg['vps'],log)
            source=(Path(__file__).parents[2]/'tools/virtualwan-router.py').read_bytes()
            vps.call('vps_idle_ip',path=cfg['vps']['config_path'],primary_ip=cfg['vps']['primary_ip'],ip=ip,
                     source_base64=base64.b64encode(source).decode(),source_sha256=hashlib.sha256(source).hexdigest())
        if client:
            if ip:
                pf=PfSense(cfg['pfsense'],log,secret);pf.login();pf.retire(ip,client)
            pve.call('pve_retire_detach' if mode=='release' else 'pve_retire_destroy',pve=cfg['pve'],client=client)
            pve.call('pve_retire_unregister',pve=cfg['pve'],client=client)
        if release:
            cf.ensure(fqdn,ip);wait_dns(fqdn,ip,log);ovh.ensure(ip,fqdn,force=True)
        if client and release:
            # Retain foreign DNS entries; do not silently reassign their target IP.
            old=client.get('fqdn')
            if old and old!=ip and old!=fqdn:
                records=cf.inspect(old,ip)
                for record in records:
                    if record.get('comment')!='Virtualwan managed':raise ValueError('Ancien DNS non marqué Virtualwan : suppression manuelle requise avant restitution de l’IP.')
                    cf.call('DELETE','/'+record['id'])
            # Remove only the old inverted stock alias created by earlier versions.
            legacy='-'.join(reversed(ip.split('.')))+'.'+cfg['domain']
            if legacy!=fqdn:
                for record in cf.inspect(legacy,ip):
                    if record.get('comment')=='Virtualwan managed':cf.call('DELETE','/'+record['id'])
            aliases=cf.call('GET',params={'type':'A','content':ip,'per_page':100})
            if any(r['name']!=fqdn for r in aliases):raise ValueError('D’autres enregistrements A utilisent cette IP ; les examiner avant restitution au stock.')
        result={'fqdn':fqdn,'public_ip':ip,'retired':bool(client) and mode!='release','mode':mode,'dns_verified':release,'journal':str(log.directory)}
        path.with_suffix('.result.json').write_text(json.dumps(result));log.event('lifecycle.complete',**result)
    finally:
        for remote in (pve,vps):
            if remote:remote.close()


if __name__=='__main__':main()
