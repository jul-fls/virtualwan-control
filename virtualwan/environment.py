"""Declarative inventories and simple per-profile connection overrides."""
from copy import deepcopy
import json
import os
import re
from urllib.parse import urlparse
from . import domain
from pathlib import Path

OVERRIDES={
    'VWAN_IPV6_ENABLED':('ipv6','enabled'),
    'VWAN_IPV6_NETWORK':('ipv6','network'),
    'VWAN_IPV6_GATEWAY_ADDRESS':('ipv6','gateway_address'),
    'VWAN_IPV6_GATEWAY_LINK_LOCAL':('ipv6','gateway_link_local'),
    'VWAN_IPV6_PROTECTED_NETWORKS_JSON':('ipv6','protected_networks'),
    'VWAN_IPV6_PROTECTED_NETWORKS':('ipv6','protected_networks'),
    'VWAN_IPV6_WAN_INTERFACE':('ipv6','wan_interface'),
    'VWAN_IPV6_PUBLIC_ALIAS':('ipv6','public_alias'),
    'VWAN_IPV6_PROTECTED_ALIAS':('ipv6','protected_alias'),
    'VWAN_PFSENSE_URL':('pfsense','url'),
    'VWAN_PFSENSE_CONNECT_IP':('pfsense','connect_ip'),
    'VWAN_PVE_HOST':('pve','host'),
    'VWAN_PVE_PORT':('pve','port'),
    'VWAN_PVE_NODE':('pve','node'),
    'VWAN_PVE_USERNAME':('pve','username'),
}


def apply_environment(control):
    bootstrap_environment(control)
    overrides={name:os.environ[name].strip() for name in OVERRIDES if os.environ.get(name,'').strip()}
    target=os.environ.get('VWAN_PROFILE_ID','').strip()
    for variable,kind,method in (
        ('VWAN_PROFILES_JSON','profile',control.add_profile),
        ('VWAN_MODELS_JSON','model',control.add_model),
    ):
        raw=os.environ.get(variable)
        try:items=json.loads(raw) if raw else []
        except json.JSONDecodeError:raise ValueError(variable+' doit contenir un tableau JSON valide.')
        if not isinstance(items,list):raise ValueError(variable+' doit contenir un tableau JSON.')
        if kind=='profile' and overrides:
            if not items:
                items=[control.store.document('profiles',row['id']) for row in control.store.rows('SELECT id FROM profiles')]
                items=[{k:v for k,v in item.items() if k not in ('revision',)} for item in items]
            if not items:raise ValueError('Configurer un profil dans VWAN_PROFILES_JSON ou dans l’UI avant les variables de connexion.')
            if any(not isinstance(item,dict) for item in items):raise ValueError(variable+' : objet JSON attendu.')
            ids=[item.get('id') for item in items]
            if target and target not in ids:raise ValueError('VWAN_PROFILE_ID ne correspond à aucun profil.')
            if not target and len(items)!=1:raise ValueError('Plusieurs routeurs : choisir VWAN_PROFILE_ID pour les variables de connexion.')
            selected=target or ids[0]
            control.environment_profile_id=selected
            items=deepcopy(items)
            for item in items:
                if item.get('id')!=selected:continue
                for name,value in overrides.items():
                    section,field=OVERRIDES[name]
                    if field=='port':value=domain.positive(value,65535)
                    if name=='VWAN_PFSENSE_URL':
                        url=urlparse(value)
                        if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('','/'):
                            raise ValueError('VWAN_PFSENSE_URL doit être une URL HTTPS sans identifiants ni chemin.')
                    if name=='VWAN_IPV6_ENABLED':
                        if value.lower() not in ('true','false','1','0'):raise ValueError('VWAN_IPV6_ENABLED: true/false attendu.')
                        value=value.lower() in ('true','1')
                    if name=='VWAN_IPV6_PROTECTED_NETWORKS':value=[v.strip() for v in value.split(',') if v.strip()]
                    if name=='VWAN_IPV6_PROTECTED_NETWORKS_JSON':
                        value=json.loads(value)
                        if not isinstance(value,list):raise ValueError('IPv6 protected networks must be a JSON array.')
                    item['config'].setdefault(section,{})[field]=value
        seen=set()
        for item in items:
            if not isinstance(item,dict):raise ValueError(variable+' : objet JSON attendu.')
            key=item.get('id')
            if not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',key) or key in seen:
                raise ValueError(variable+' : chaque objet nécessite un id stable et unique.')
            seen.add(key)
            if control.store.rows('SELECT 1 FROM settings WHERE name=?',('environment:deleted:'+kind+':'+key,)):continue
            data={k:v for k,v in item.items() if k!='id'}
            normalized=domain.profile(data) if kind=='profile' else domain.model(data)
            with control.store.tx() as db:
                setting='environment:'+kind+':'+key
                old=db.execute('SELECT value FROM settings WHERE name=?',(setting,)).fetchone()
                fingerprint=domain.digest(normalized)
                existing=db.execute('SELECT id FROM '+('profiles' if kind=='profile' else 'models')+' WHERE id=?',(key,)).fetchone()
                # Legacy JSON is a one-time import, never an owner of UI-managed records.
                if existing and raw:
                    if kind=='profile' and overrides and key==control.environment_profile_id:
                        current=control.store.document('profiles',key)
                        for name in overrides:
                            section,field=OVERRIDES[name]
                            current['config'].setdefault(section,{})[field]=normalized['config'][section][field]
                        override_setting='environment:overrides:'+key
                        override_digest=domain.digest(overrides)
                        previous=db.execute('SELECT value FROM settings WHERE name=?',(override_setting,)).fetchone()
                        if not previous or previous['value']!=override_digest:
                            method(current,key)
                            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(override_setting,override_digest))
                    if not old:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(setting,fingerprint))
                    continue
                if old and old['value']==fingerprint:continue
                method(data,key)
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(setting,fingerprint))
            control.store.event(key,'configuration.environment.loaded',kind=kind)


def flat_profile_fields():
    """Every example leaf has a scalar variable; arrays use comma-separated values."""
    example=json.loads((Path(__file__).parent.parent/'examples/profile.json').read_text(encoding='utf-8-sig'))
    fields={}
    for name,value in example.items():
        if name=='config':
            for section,obj in value.items():
                if isinstance(obj,dict):
                    for field,default in obj.items():
                        prefix='VWAN_ROUTER' if section in ('router','vps') else 'VWAN_'+section.upper()
                        suffix=field.upper() if section!='router' else 'WG_'+field.upper()
                        fields[prefix+'_'+suffix]=(('config',section,field),default)
                else:fields['VWAN_ROUTER_'+section.upper()]=(('config',section),obj)
        else:fields['VWAN_ROUTER_'+name.upper()]=((name,),value)
    return example,fields


def scalar(value,default):
    if isinstance(default,bool):
        if value.lower() not in ('true','false','1','0'):raise ValueError('Valeur booléenne true/false requise.')
        return value.lower() in ('true','1')
    if isinstance(default,int):return int(value)
    if isinstance(default,list):return [v.strip() for v in value.split(',') if v.strip()]
    return value or None if default is None else value


def bootstrap_environment(control):
    if os.environ.get('VWAN_ROUTER_BOOTSTRAP','false').lower()=='true':
        key=os.environ.get('VWAN_PROFILE_ID','router-1').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',key):raise ValueError('VWAN_PROFILE_ID invalide.')
        if not control.store.rows('SELECT id FROM profiles WHERE id=?',(key,)) and not control.store.rows('SELECT 1 FROM settings WHERE name=?',('environment:deleted:profile:'+key,)):
            profile,fields=flat_profile_fields()
            for variable,(path,default) in fields.items():
                if variable not in os.environ:continue
                target=profile
                for part in path[:-1]:target=target[part]
                target[path[-1]]=scalar(os.environ[variable].strip(),default)
            # IPv6 optional leaves are configured by the existing scalar overrides.
            control.add_profile(profile,key)
            control.store.event(key,'configuration.bootstrap.imported',source='scalar_environment',database_owned=True)
    if os.environ.get('VWAN_MODEL_BOOTSTRAP','false').lower()=='true':
        key=os.environ.get('VWAN_MODEL_ID','standard')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',key):raise ValueError('VWAN_MODEL_ID invalide.')
        if not control.store.rows('SELECT id FROM models WHERE id=?',(key,)):
            data={k:os.environ.get('VWAN_MODEL_'+k.upper(),v) for k,v in {'name':'Standard','range':'Standard','cores':2,'memory_mb':2048,'disk_gb':32,'template':9001}.items()}
            control.add_model(data,key)
