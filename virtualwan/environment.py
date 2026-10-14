"""Declarative inventories and simple per-profile connection overrides."""
from copy import deepcopy
import json
import os
import re
from urllib.parse import urlparse
from . import domain

OVERRIDES={
    'VWAN_PFSENSE_URL':('pfsense','url'),
    'VWAN_PFSENSE_CONNECT_IP':('pfsense','connect_ip'),
    'VWAN_PVE_HOST':('pve','host'),
    'VWAN_PVE_PORT':('pve','port'),
    'VWAN_PVE_NODE':('pve','node'),
    'VWAN_PVE_USERNAME':('pve','username'),
}


def apply_environment(control):
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
                    item['config'][section][field]=value
        seen=set()
        for item in items:
            if not isinstance(item,dict):raise ValueError(variable+' : objet JSON attendu.')
            key=item.get('id')
            if not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',key) or key in seen:
                raise ValueError(variable+' : chaque objet nécessite un id stable et unique.')
            seen.add(key)
            data={k:v for k,v in item.items() if k!='id'}
            normalized=domain.profile(data) if kind=='profile' else domain.model(data)
            with control.store.tx() as db:
                setting='environment:'+kind+':'+key
                old=db.execute('SELECT value FROM settings WHERE name=?',(setting,)).fetchone()
                fingerprint=domain.digest(normalized)
                if old and old['value']==fingerprint:continue
                method(data,key)
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(setting,fingerprint))
            control.store.event(key,'configuration.environment.loaded',kind=kind)
