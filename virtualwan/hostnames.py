import ipaddress
import re


def stock_hostname(ip,domain,pattern='vps-{ipv4_compact}.{domain}'):
    address=str(ipaddress.IPv4Address(ip))
    name=pattern.format(ipv4=address,ipv4_compact=address.replace('.',''),ipv4_dashed=address.replace('.','-'),domain=domain).lower()
    if len(name)>253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in name.split('.')):
        raise ValueError('Domaine du reverse DNS invalide.')
    return name
