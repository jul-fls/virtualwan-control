"""Optional declarative profiles/models supplied entirely through environment."""
import json
import os
import re
from . import domain


def apply_environment(control):
    for variable,kind,method in (
        ('VWAN_PROFILES_JSON','profile',control.add_profile),
        ('VWAN_MODELS_JSON','model',control.add_model),
    ):
        value=os.environ.get(variable)
        if not value:continue
        try:items=json.loads(value)
        except json.JSONDecodeError:raise ValueError(variable+' doit contenir un tableau JSON valide.')
        if not isinstance(items,list):raise ValueError(variable+' doit contenir un tableau JSON.')
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
