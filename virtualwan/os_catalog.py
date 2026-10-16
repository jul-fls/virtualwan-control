"""Private administrative catalogue, independent from commercial resource models."""
import json,re
from flask import jsonify,request
from . import domain
from .store import ident

FAMILIES={'debian':'Debian','ubuntu':'Ubuntu','fedora':'Fedora','almalinux':'AlmaLinux','rocky':'Rocky Linux','freebsd':'FreeBSD'}

def images(store):
    rows=store.rows("SELECT value FROM settings WHERE name='os_images'")
    return json.loads(rows[0]['value']) if rows else []

def available_images(store,profile_id):
    return [i for i in images(store) if i['family'] in FAMILIES and i['profile_id']==profile_id and i['enabled']]

def select_image(store,profile,model,key=None,node=None):
    node=node or profile['config']['pve']['node']
    if not key or key=='default':
        return {'id':'default','name':'Debian · template du modèle','family':'debian','interface':profile['config']['guest']['interface'],'template':model.get('template') or profile['config']['pve']['template']}
    if key=='custom':return {'id':'custom','name':'ISO personnalisé','family':'custom','interface':profile['config']['guest']['interface'],'template':model.get('template') or profile['config']['pve']['template']}
    item=next((i for i in available_images(store,profile['id']) if i['id']==key),None)
    if not item:raise domain.Conflict('Ce système n’est pas publié pour ce routeur.')
    template=item['templates'].get(node)
    if not template:raise domain.Conflict('Le template de ce système n’est pas configuré sur le nœud '+node+'.')
    return {**item,'template':template}

def catalogue_view(store,profile_id=None):
    return {'families':FAMILIES,'images':[i for i in images(store) if i['family'] in FAMILIES] if profile_id is None else available_images(store,profile_id)}

def install_os_catalog(app,store,control):
    @app.get('/api/os-images')
    def get_catalogue():return jsonify(catalogue_view(store))
    @app.post('/api/os-images')
    @app.put('/api/os-images/<key>')
    def save_image(key=None):
        b=request.get_json();family=b.get('family')
        if family not in FAMILIES:raise ValueError('Famille de système invalide.')
        profile=store.document('profiles',b['profile_id']);templates=b.get('templates',{})
        if not isinstance(templates,dict) or not templates or len(templates)>32:raise ValueError('Indiquer les templates par nœud.')
        templates={n:domain.positive(t,999999999) for n,t in templates.items() if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}',n)}
        if len(templates)!=len(b['templates']):raise ValueError('Nom de nœud invalide.')
        interface=b.get('interface','vtnet0' if family=='freebsd' else 'ens18')
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.-]{0,14}',interface):raise ValueError('Interface invitée invalide.')
        if type(b.get('enabled',False)) is not bool:raise ValueError('Publication : booléen requis.')
        if b.get('enabled') and b.get('prepared') is not True:raise ValueError('Confirmer la préparation et la validation du template cloud-init avant publication.')
        item={'id':key or ident(),'name':domain.label(b['name']),'family':family,'profile_id':profile['id'],'templates':templates,'interface':interface,'enabled':b.get('enabled',False),'prepared':b.get('prepared') is True}
        with store.tx() as db:
            rows=images(store)
            if key and not any(i['id']==key for i in rows):raise KeyError('Système introuvable.')
            rows=[i for i in rows if i['id']!=item['id']]+[item]
            db.execute("INSERT OR REPLACE INTO settings VALUES('os_images',?)",(json.dumps(rows),))
        store.event(ident(),'os.image.saved',image=item)
        return jsonify(item)
    @app.delete('/api/os-images/<key>')
    def delete_image(key):
        with store.tx() as db:
            rows=images(store)
            if not any(i['id']==key for i in rows):raise KeyError('Système introuvable.')
            db.execute("INSERT OR REPLACE INTO settings VALUES('os_images',?)",(json.dumps([i for i in rows if i['id']!=key]),))
        store.event(ident(),'os.image.removed',image_id=key)
        return jsonify(ok=True)
