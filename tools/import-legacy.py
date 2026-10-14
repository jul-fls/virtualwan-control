#!/usr/bin/env python3
"""Import an existing generic CLI configuration/registry into the private application volume.

Does not contact providers, modify networking, buy anything or create/start VMs.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import time
sys.path.insert(0,str(Path(__file__).parents[1]))
from virtualwan.control import Control
from virtualwan.store import Store,ident

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--registry',type=Path)
    parser.add_argument('--data',type=Path,default=Path(os.environ.get('VWAN_DATA_DIR','data')))
    parser.add_argument('--name',default='Existing router');parser.add_argument('--secrets-json',type=Path)
    parser.add_argument('--known-hosts',type=Path);parser.add_argument('--segment',default='legacy')
    parser.add_argument('--deployments-json',type=Path,help='Optional verified per-IP metadata (hostname/node/last_verified)')
    args=parser.parse_args();store=Store(args.data)
    with store.tx() as db:
        mode=db.execute("SELECT value FROM settings WHERE name='mode'").fetchone()
        if mode and mode['value']!='live':raise ValueError('Use a separate live volume, not the demo database.')
        db.execute("INSERT OR REPLACE INTO settings VALUES('mode','live')")
    config=json.loads(args.config.read_text(encoding='utf-8-sig'))
    config=copy.deepcopy(config);config.pop('public_ip',None);config.pop('client_isolation_verified',None)
    config['pve']['vmid']=None;config['pfsense']['next_hop']=None
    config['pve']['known_hosts']=config['vps']['known_hosts']='/data/ssh-known-hosts'
    if args.segment!='legacy':config['pve']['isolation_segment']=args.segment
    for item in ('pve','vps'):
        if config[item].get('private_key'):raise ValueError('Mount and configure private key paths under data explicitly before import.')
    config['guest']['ssh_public_key_file']=''
    control=Control(store)
    matching=next((store.document('profiles',p['id']) for p in store.rows('SELECT id FROM profiles') if store.document('profiles',p['id'])['config']['vps'].get('primary_ip')==config['vps']['primary_ip']),None)
    profile=control.add_profile({'name':args.name,'provider':'ovh','capacity':16,'ready':True,'config':config},matching['id'] if matching else None)
    if args.secrets_json:
        for name,value in json.loads(args.secrets_json.read_text()).items():store.secret_set(name,value)
    if args.known_hosts:shutil.copyfile(args.known_hosts,store.root/'ssh-known-hosts');os.chmod(store.root/'ssh-known-hosts',0o600)
    imported=0
    metadata=json.loads(args.deployments_json.read_text()) if args.deployments_json else {}
    if args.registry:
        registry=json.loads(args.registry.read_text())
        if args.segment!='legacy':registry=registry['segments'][args.segment]
        for client in registry['clients']:
            ip=client['public_ip']
            existing=store.rows('SELECT * FROM ip_pool WHERE ip=?',(ip,))
            if existing and existing[0]['profile_id']!=profile:raise ValueError('IP already belongs to another profile')
            if existing:continue
            control.add_ip(profile,ip)
            info={'fqdn':metadata.get(ip,{}).get('fqdn',ip),'status':'imported','job_id':ident(),
                  'customer':'Imported existing VM','mac':client['mac'],'next_hop':client['next_hop'],'vmid':client['vmid'],
                  'source':'local registry; verify with infrastructure inventory','imported_at':time.time(),'simulated':False}
            info.update({k:v for k,v in metadata.get(ip,{}).items() if k in ('fqdn','node','last_verified','customer')})
            with store.tx() as db:
                db.execute("UPDATE ip_pool SET state='assigned' WHERE ip=?",(ip,))
                db.execute('INSERT INTO deployments VALUES(?,?,?,?,?)',(ident(),ip,profile,None,json.dumps(info)))
            imported+=1
    if not store.rows('SELECT id FROM models'):
        control.add_model({'name':'Imported standard','range':'Standard',**{k:config['pve'][k] for k in ('cores','memory_mb','disk_gb','template')}})
    store.event(profile,'legacy.imported',clients=imported,network_changes=False,purchases=False)
    print(json.dumps({'profile_id':profile,'clients_imported':imported,'private_data_directory':str(store.root),'secrets_values_printed':False}))

if __name__=='__main__':main()
