#!/usr/bin/env python3
"""Cluster registry with backward-compatible single-segment and multiple-router isolation."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

REGISTRY=Path('/etc/pve/virtualwan-isolation.json')
BASE=Path('/usr/local/lib/virtualwan-isolation')
STATE=Path('/run/virtualwan-isolation')

def run(*args):return subprocess.check_output(args,text=True)

def module():
    spec=importlib.util.spec_from_file_location('policy',BASE/'public-policy.py')
    obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj);return obj

def table_name(segment):
    return 'virtualwan_isolation' if segment=='legacy' else 'virtualwan_isolation_'+hashlib.sha256(segment.encode()).hexdigest()[:12]

def segments(registry):
    result=dict(registry.get('segments',{}))
    if 'clients' in registry and 'nodes' in registry:
        if 'legacy' in result:raise ValueError('Reserved segment name')
        result['legacy']={k:v for k,v in registry.items() if k!='segments'}
    if not result:raise ValueError('No configured segment')
    vmids=set();public=set();macs=set();bridges=set()
    for name,config in result.items():
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',name):raise ValueError('Invalid segment name')
        for node,params in config['nodes'].items():
            pair=(node,params['bridge'])
            if pair in bridges:raise ValueError('A bridge cannot belong to two segments')
            bridges.add(pair);module().validate({**config,**params,'table_name':table_name(name)})
        for v in config['clients']:
            for value,bucket in ((v['vmid'],vmids),(v['public_ip'],public),(v['mac'].lower(),macs)):
                if value in bucket:raise ValueError('Duplicate VM/MAC/public identity across segments')
                bucket.add(value)
    return result

def apply(force=False):
    registry=json.loads(REGISTRY.read_text());all_segments=segments(registry);node=run('hostname','-s').strip()
    configurations=[{**c,**c['nodes'][node],'table_name':table_name(name)} for name,c in all_segments.items() if node in c['nodes']]
    if not configurations:raise ValueError('Node not registered')
    tables=[c['table_name'] for c in configurations];data=''.join(module().generate(c) for c in configurations)
    STATE.mkdir(exist_ok=True);digest=hashlib.sha256(data.encode()).hexdigest()
    previous=(STATE/'digest').read_text() if (STATE/'digest').exists() else None
    if not force and digest==previous and all(subprocess.run(['nft','list','table','bridge',t],capture_output=True).returncode==0 for t in tables):return
    old=json.loads((STATE/'tables.json').read_text()) if (STATE/'tables.json').exists() else []
    for t in set(old)-set(tables):
        if not re.fullmatch(r'virtualwan_isolation(?:_[a-f0-9]{12})?',t):raise ValueError('Invalid owned table')
        if subprocess.run(['nft','list','table','bridge',t],capture_output=True).returncode==0:data+='delete table bridge '+t+'\n'
    candidate=STATE/'candidate.nft';candidate.write_text(data);run('nft','-c','-f',str(candidate));run('nft','-f',str(candidate))
    (STATE/'digest').write_text(digest);(STATE/'tables.json').write_text(json.dumps(tables))
    print('Isolation loaded:',digest)

def pre_start(vmid):
    registry=json.loads(REGISTRY.read_text());node=run('hostname','-s').strip()
    found=[(c,v) for c in segments(registry).values() for v in c['clients'] if v['vmid']==vmid]
    if len(found)!=1:raise ValueError('VM identity is not registered')
    c,v=found[0]
    if node not in c['nodes']:raise ValueError('Destination node does not host this public segment')
    files=list(Path('/etc/pve/nodes').glob(f'*/qemu-server/{vmid}.conf'))
    if len(files)!=1:raise ValueError('Ambiguous/missing VM configuration')
    conf=files[0].read_text();match=re.search(r'^net0: (.+)$',conf,re.M)
    opts=dict(x.split('=',1) for x in match[1].split(',') if '=' in x) if match else {}
    if opts.get('bridge')!=c['nodes'][node]['bridge'] or opts.get('virtio','').lower()!=v['mac'].lower() or 'tag' in opts or 'trunks' in opts:raise ValueError('Unexpected client network identity')
    if re.search(r'^(hostpci|usb|virtiofs|args|net[1-9])',conf,re.M):raise ValueError('Additional NIC/passthrough/shared filesystem/custom QEMU args require review')
    apply()

def register(args):
    registry=json.loads(REGISTRY.read_text());items=segments(registry)
    if args.segment not in items:raise ValueError('Unknown segment; bootstrap it before provisioning')
    target=registry if args.segment=='legacy' else registry['segments'][args.segment]
    item={'vmid':args.vmid,'mac':args.mac.lower(),'public_ip':args.public_ip,'next_hop':args.next_hop}
    old=next((v for v in target['clients'] if v['vmid']==args.vmid),None)
    if old and old!=item:raise ValueError('Registered identity conflict')
    if not old:
        target['clients'].append(item);segments(registry)
        temp=REGISTRY.with_suffix('.json.next');temp.write_text(json.dumps(registry,indent=2));os.replace(temp,REGISTRY)
    apply()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['apply','pre-start','register','status','capabilities'])
    parser.add_argument('--force',action='store_true');parser.add_argument('--segment',default='legacy')
    parser.add_argument('--vmid',type=int);parser.add_argument('--mac');parser.add_argument('--public-ip');parser.add_argument('--next-hop')
    args=parser.parse_args()
    if args.action=='capabilities':print(json.dumps({'multi_segment':True,'legacy_registry':True}));return
    with open('/run/virtualwan-isolation.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if args.action=='apply':apply(args.force)
        elif args.action=='register':register(args)
        elif args.action=='pre-start':pre_start(args.vmid)
        else:
            apply();node=run('hostname','-s').strip()
            for name,c in segments(json.loads(REGISTRY.read_text())).items():
                if node in c['nodes']:print(run('nft','list','table','bridge',table_name(name)))

if __name__=='__main__':main()
