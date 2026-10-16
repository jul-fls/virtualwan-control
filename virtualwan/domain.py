import hashlib
import ipaddress
import json
import re
import time


class Conflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def public_ip(value):
    ip=ipaddress.IPv4Address(value)
    if not ip.is_global or ip.is_multicast:raise ValueError('Une IPv4 publique unicast est requise.')
    return str(ip)


def positive(value,maximum):
    if isinstance(value,bool):raise ValueError('Nombre entier requis.')
    try:integer=int(value)
    except (TypeError,ValueError,OverflowError):raise ValueError('Nombre entier requis.') from None
    if str(integer)!=str(value) or not 1<=integer<=maximum:raise ValueError('Valeur hors limites.')
    return integer


def model(data):
    return {'name':label(data['name']), 'range':label(data.get('range','Standard')),
            'cores':positive(data['cores'],256),'memory_mb':positive(data['memory_mb'],1048576),
            'disk_gb':positive(data['disk_gb'],65536),
            'template':positive(data.get('template',9001),999999999)}


def label(value):
    if not isinstance(value,str):raise ValueError('Nom invalide.')
    value=value.strip()
    if not 1<=len(value)<=100 or any(ord(c)<32 for c in value):raise ValueError('Nom invalide.')
    return value


def profile(data):
    config=data['config']
    if not isinstance(config,dict):raise ValueError('Configuration JSON attendue.')
    for section in ('vps','pve','guest','pfsense','ovh','cloudflare'):
        if not isinstance(config.get(section),dict):raise ValueError('Section manquante : '+section)
    def check(obj):
        if isinstance(obj,dict):
            for key,value in obj.items():
                if re.search(r'password|secret|token|private_key',key,re.I) and not (key.endswith('_env') or key in ('private_key','key_passphrase_env','sudo_password_env')):
                    raise ValueError('Utiliser le coffre de secrets et les références *_env, pas un secret dans le JSON.')
                if key.endswith('_env') and value is not None and not re.fullmatch(r'[A-Z][A-Z0-9_]{2,79}',str(value)):
                    raise ValueError('Référence de secret invalide.')
                check(value)
        elif isinstance(obj,list):
            for x in obj:check(x)
        elif isinstance(obj,str) and 'PRIVATE KEY-----' in obj:raise ValueError('Une clé privée doit être montée comme fichier, pas stockée dans le profil.')
    check(config)
    domain=config.get('domain','')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,250}',domain) or '..' in domain:raise ValueError('Domaine DNS invalide.')
    network=ipaddress.IPv4Network(config['pfsense']['next_hop_network'])
    if network.num_addresses>65536:raise ValueError('Sous-réseau de transit trop grand.')
    if ipaddress.IPv4Address(config['guest']['gateway']) not in network:raise ValueError('Gateway hors du réseau de transit.')
    from .ipv6 import ipv6_settings
    v6=ipv6_settings(config.get('ipv6'))
    if v6:
        if v6.get('address'):raise ValueError('IPv6 address is allocated per VM, not in a profile.')
        config['ipv6']=v6
    if not isinstance(data.get('ready',False),bool):raise ValueError('État prêt du profil : booléen requis.')
    capacity=positive(data.get('capacity',16),16 if data.get('provider','ovh')=='ovh' else 256)
    return {'name':label(data['name']),'provider':data.get('provider','ovh'),
            'capacity':capacity,'ready':bool(data.get('ready',False)),'config':config}


def quote_binding(quote):
    # Prices/contracts/line items are bound; supplier URLs or order date may change.
    return {key:quote.get(key) for key in ('prices','priceWithTax','priceWithoutTax','tax','contracts','details')}


def amount(quote):
    from decimal import Decimal,InvalidOperation
    price=(quote.get('prices') or {}).get('withTax') or quote.get('priceWithTax')
    if not isinstance(price,dict) or not re.fullmatch(r'[A-Z]{3}',str(price.get('currencyCode',''))):raise ValueError('Montant fournisseur/devise indisponible ; aucun achat possible.')
    try:value=Decimal(str(price['value']))
    except (InvalidOperation,KeyError):raise ValueError('Montant fournisseur invalide.')
    if not value.is_finite() or value<0:raise ValueError('Montant fournisseur invalide.')
    return price


def approval_valid(purchase,body,fresh):
    if purchase['state']!='awaiting_approval':raise Conflict('Ce devis a déjà été traité.')
    if purchase['expires_at']<=time.time():raise Conflict('Le devis a expiré ; préparer un nouveau devis.')
    if body.get('revision')!=purchase['revision']:raise Conflict('Le devis affiché a changé.')
    if body.get('confirmation')!='COMMANDER '+purchase['id'][:8]:raise ValueError('Saisir la phrase de confirmation affichée.')
    if body.get('accept_contracts') is not True:raise ValueError('Accepter explicitement les contrats du devis.')
    amount(fresh)
    if digest(quote_binding(fresh))!=purchase['quote_digest']:raise Conflict('Prix ou contrats modifiés ; une nouvelle confirmation est nécessaire.')
