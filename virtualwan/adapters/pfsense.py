"""pfSense WebGUI HTTPS adapter: session cookies, CSRF and native forms only."""
import hashlib
import ipaddress
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urljoin

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
import socket
from unittest.mock import patch


class PinnedTLSAdapter(HTTPAdapter):
    def __init__(self, fingerprint):
        if not re.fullmatch(r'[0-9a-fA-F]{64}', fingerprint.replace(':', '')):
            raise ValueError('Invalid TLS SHA-256 fingerprint')
        self.fingerprint = fingerprint.replace(':', '')
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        kwargs['assert_fingerprint'] = self.fingerprint
        super().init_poolmanager(connections, maxsize, block=block, **kwargs)

    def cert_verify(self, conn, url, verify, cert):
        # Fingerprint validation replaces CA/name validation for a pinned self-signed leaf.
        conn.cert_reqs = 'CERT_NONE'
        conn.ca_certs = None
        conn.ca_cert_dir = None


def fields(html, required):
    soup = BeautifulSoup(html, 'html.parser')
    for form in soup.find_all('form'):
        names = {x.get('name') for x in form.select('[name]')}
        if not set(required) <= names:
            continue
        data = {}
        for el in form.select('input[name],select[name],textarea[name]'):
            name = el['name']
            if el.has_attr('disabled') or el.get('type') in ('submit', 'button', 'file', 'reset'):
                continue
            if el.get('type') in ('checkbox', 'radio') and not el.has_attr('checked'):
                continue
            if el.name == 'select':
                option = el.find('option', selected=True) or el.find('option')
                data[name] = option.get('value', option.text) if option else ''
            else:
                data[name] = el.text if el.name == 'textarea' else el.get('value', 'on' if el.get('type') == 'checkbox' else '')
        csrf = soup.find('input', attrs={'name': '__csrf_magic'})
        if not csrf:
            raise ValueError('pfSense CSRF field missing')
        data['__csrf_magic'] = csrf.get('value', '')
        return data
    raise ValueError('pfSense form differs from expected fields: ' + ','.join(required))


class PfSense:
    def __init__(self, cfg, journal, secret):
        self.cfg, self.log, self.secret = cfg, journal, secret
        self.base = cfg['url'].rstrip('/')
        if urlsplit(self.base).scheme != 'https':
            raise ValueError('pfSense requires HTTPS')
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = cfg.get('ca_bundle') or True
        if cfg.get('certificate_sha256'):
            self.session.mount(self.base + '/', PinnedTLSAdapter(cfg['certificate_sha256']))

    def request(self, method, path, **kwargs):
        self.log.event('pfsense.http.begin', method=method, path=path, data=kwargs.get('data'))
        url = self.base + '/' + path.lstrip('/')
        original_resolver = socket.getaddrinfo
        def resolve(host, *args, **kwargs):
            if self.cfg.get('connect_ip') and host == urlsplit(self.base).hostname:
                host = self.cfg['connect_ip']
            return original_resolver(host, *args, **kwargs)
        for _ in range(6):
            with patch('socket.getaddrinfo', side_effect=resolve):
                response = self.session.request(method, url, timeout=45, allow_redirects=False, **kwargs)
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            url = urljoin(url, response.headers['Location'])
            if (urlsplit(url).scheme, urlsplit(url).netloc) != (urlsplit(self.base).scheme, urlsplit(self.base).netloc):
                raise ValueError('Refusing cross-origin pfSense redirect')
            if response.status_code in (301, 302, 303):
                method, kwargs = 'GET', {}
        else:
            raise ValueError('pfSense redirect loop')
        response.raise_for_status()
        if urlsplit(response.url).netloc != urlsplit(self.base).netloc:
            raise ValueError('Unexpected pfSense redirect host')
        self.log.event('pfsense.http.end', method=method, path=path, status=response.status_code, response_sha256=hashlib.sha256(response.content).hexdigest())
        return response

    def login(self):
        r = self.request('GET', 'index.php')
        data = fields(r.text, ['usernamefld', 'passwordfld'])
        data.update(usernamefld=self.cfg['username'], passwordfld=self.secret(self.cfg['password_env']), login='Login')
        self.request('POST', 'index.php', data=data)
        r = self.request('GET', 'diag_backup.php')
        fields(r.text, ['backuparea', 'download'])
        if BeautifulSoup(r.text, 'html.parser').find('input', attrs={'name': 'usernamefld'}):
            raise ValueError('pfSense authentication failed')

    def snapshot(self, label):
        page = self.request('GET', 'diag_backup.php')
        data = fields(page.text, ['backuparea', 'download'])
        data.update(backuparea='', download='Download configuration as XML', donotbackuprrd='yes')
        for k in ('encrypt', 'encrypt_password', 'nopackages', 'backupdata', 'backupssh'):
            data.pop(k, None)
        r = self.request('POST', 'diag_backup.php', data=data)
        root = ET.fromstring(r.content)
        if root.tag != 'pfsense':
            raise ValueError('Unexpected pfSense configuration download')
        self.log.backup(label + '.xml', r.content)
        return root

    def form(self, path, updates, required, expected=None):
        r = self.request('GET', path)
        data = fields(r.text, required)
        if expected and any(data.get(k) != str(v) for k, v in expected.items()):
            raise ValueError('pfSense form target changed since inventory')
        for key, value in updates.items():
            if re.fullmatch(r'address[0-9]+', key) and data.get(key) and data[key] != str(value):
                raise ValueError('Alias row changed since inventory; refusing to overwrite')
        for key, value in updates.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = str(value)
        data['save'] = 'Save'
        result = self.request('POST', path, data=data)
        soup = BeautifulSoup(result.text, 'html.parser')
        errors = soup.select('.input-errors, .alert-danger')
        if errors:
            raise ValueError('pfSense refused form: ' + ' '.join(e.get_text(' ', strip=True) for e in errors))

    def apply(self, path):
        r = self.request('GET', path)
        soup = BeautifulSoup(r.text, 'html.parser')
        token = soup.find('input', attrs={'name': '__csrf_magic'})
        if not token:
            raise ValueError('CSRF missing for apply')
        r = self.request('POST', path, data={'__csrf_magic': token['value'], 'apply': 'Apply Changes'})
        if BeautifulSoup(r.text, 'html.parser').select('.alert-danger'):
            raise ValueError('pfSense apply failed')

    def preflight(self, root, guest, ip):
        c = self.cfg
        interfaces = root.find('interfaces')
        # Discover internal interface keys from the working setup, never from labels guessed by name.
        for key, field, expected in (('public_interface', 'ipaddr', guest['gateway']), ('wireguard_interface', 'gateway', c['wireguard_gateway'])):
            if not c.get(key):
                candidates = [x.tag for x in interfaces if x.findtext(field) == expected]
                if len(candidates) != 1:
                    raise ValueError('Cannot uniquely discover pfSense ' + key)
                c[key] = candidates[0]
        for key in ('public_interface', 'wireguard_interface'):
            if not c.get(key) or interfaces.find(c[key]) is None:
                raise ValueError(f'Configure actual pfSense internal key: {key}')
        public = interfaces.find(c['public_interface'])
        net = ipaddress.ip_network(public.findtext('ipaddr') + '/' + public.findtext('subnet'), strict=False)
        if not c.get('next_hop'):
            existing = next((gw for gw in root.findall('gateways/gateway_item') if gw.findtext('name') == 'VWAN_' + ip.replace('.', '')), None)
            if existing is not None:
                c['next_hop'] = existing.findtext('gateway')
            else:
                reserved = {m.findtext('ipaddr') for m in root.findall('dhcpd/' + c['public_interface'] + '/staticmap')}
                reserved.update(gw.findtext('gateway') for gw in root.findall('gateways/gateway_item'))
                reserved.update(x.findtext('ipaddr') for x in interfaces)
                dhcp_node = root.find('dhcpd/' + c['public_interface'])
                pools = ([dhcp_node.find('range')] + dhcp_node.findall('pool/range')) if dhcp_node is not None else []
                for candidate in list(net.hosts())[10:]:
                    if str(candidate) in reserved:
                        continue
                    if any(pool is not None and pool.findtext('from') and int(ipaddress.ip_address(pool.findtext('from'))) <= int(candidate) <= int(ipaddress.ip_address(pool.findtext('to'))) for pool in pools):
                        continue
                    c['next_hop'] = str(candidate)
                    break
                if not c.get('next_hop'):
                    raise ValueError('No free private ARP next-hop')
            self.log.event('pfsense.next-hop.selected', next_hop=c['next_hop'])
        hop = ipaddress.ip_address(c['next_hop'])
        if hop not in net or str(net) != c['next_hop_network'] or str(hop) == guest['gateway'] or hop in (net.network_address, net.broadcast_address):
            raise ValueError('Invalid pfSense private ARP next-hop')
        if public.findtext('ipaddr') != guest['gateway']:
            raise ValueError('pfSense public interface and guest gateway mismatch')
        if interfaces.find(c['wireguard_interface']).findtext('gateway') != c['wireguard_gateway']:
            raise ValueError('WireGuard upstream gateway/reply-to configuration must match working setup')
        dhcp = root.find('dhcpd/' + c['public_interface'])
        if dhcp is None or dhcp.find('enable') is None:
            raise ValueError('Existing Kea scope required; will not enable a new DHCP scope blindly')
        if (root.findtext('dhcpbackend') or root.findtext('system/dhcpbackend')) not in ('kea', None):
            raise ValueError('Kea required')
        alias = next((a for a in root.findall('aliases/alias') if a.findtext('name') == c['public_alias']), None)
        if alias is None or alias.findtext('type') != 'host':
            raise ValueError('Existing public host alias required')
        if not any(r.findtext('interface') == c['public_interface'] and r.findtext('type') == 'pass' and r.findtext('source/address') == c['public_alias'] and r.findtext('gateway') == c['wireguard_gateway'] and r.find('disabled') is None for r in root.findall('filter/rule')):
            raise ValueError('Existing outgoing alias rule with WireGuard policy gateway required')
        gwname = 'VWAN_' + ip.replace('.', '')
        if any(r.findtext('network') == ip + '/32' and r.findtext('gateway') != gwname for r in root.findall('staticroutes/route')):
            raise ValueError('Public /32 already routed by a different deployment')
        for pool in [dhcp.find('range')] + dhcp.findall('pool/range'):
            if pool is not None and pool.findtext('from') and int(ipaddress.ip_address(pool.findtext('from'))) <= int(hop) <= int(ipaddress.ip_address(pool.findtext('to'))):
                raise ValueError('Static ARP next-hop falls within dynamic DHCP pool')
        for m in dhcp.findall('staticmap'):
            if m.findtext('ipaddr') == str(hop) and m.findtext('descr') != 'Virtualwan ' + ip:
                raise ValueError('Private next-hop already reserved by another mapping')
        mode = root.findtext('nat/outbound/mode')
        if mode not in ('hybrid', 'advanced'):
            raise ValueError('Existing Hybrid/Manual outbound NAT required; automatic mode migration must be reviewed on the actual firewall')
        for path, required in [
            (f'services_dhcp_edit.php?if={c["public_interface"]}', ['mac', 'ipaddr', 'arp_table_static_entry']),
            ('system_gateways_edit.php', ['interface', 'name', 'gateway', 'monitor_disable']),
            ('system_routes_edit.php', ['network', 'network_subnet', 'gateway']),
            ('firewall_aliases_edit.php', ['name', 'type', 'address0']),
            ('firewall_nat_out_edit.php?after=-1', ['nonat', 'source_type', 'source', 'interface']),
            (f'firewall_rules_edit.php?if={c["wireguard_interface"]}&after=-1', ['type', 'interface', 'srctype', 'dsttype', 'gateway'])
        ]:
            fields(self.request('GET', path).text, required)
        self.log.event('pfsense.preflight.ok', public_network=str(net), next_hop=str(hop), nat_mode=mode,
                       interfaces={x.tag: x.findtext('descr') for x in interfaces})
        for page in ('firewall_aliases.php', 'system_routes.php', 'system_gateways.php', 'firewall_nat_out.php', 'firewall_rules.php', 'services_dhcp.php?if=' + c['public_interface']):
            soup = BeautifulSoup(self.request('GET', page).text, 'html.parser')
            if soup.select('[name="apply"]'):
                raise ValueError('Existing unapplied pfSense changes on ' + page + '; resolve them before provisioning')

    def configure(self, root, ip, fqdn, mac):
        c = self.cfg
        alias = c['public_alias']
        aliases = root.findall('aliases/alias')
        found = next(((i, x) for i, x in enumerate(aliases) if x.findtext('name') == alias), None)
        if not found or found[1].findtext('type') != 'host':
            raise ValueError('Expected existing host alias ' + alias)
        values = found[1].findtext('address', '').split()
        if ip not in values:
            n = len(values)
            self.form(f'firewall_aliases_edit.php?id={found[0]}', {f'address{n}': ip, f'detail{n}': fqdn}, ['name', 'type', 'address0'], expected={'name': alias, 'type': 'host'})
            self.apply('firewall_aliases.php')
            root = self.snapshot('after-alias')
        assert ip in next(a for a in root.findall('aliases/alias') if a.findtext('name') == alias).findtext('address', '').split()
        maps = root.findall('dhcpd/' + c['public_interface'] + '/staticmap')
        match = next(((i, x) for i, x in enumerate(maps) if x.findtext('ipaddr') == c['next_hop']), None)
        if match and match[1].findtext('mac', '').lower() != mac.lower():
            raise ValueError('Existing ARP mapping MAC conflict')
        if not match or match[1].find('arp_table_static_entry') is None:
            path = f'services_dhcp_edit.php?if={c["public_interface"]}' + (f'&id={match[0]}' if match else '')
            self.form(path, {'mac': mac.lower(), 'ipaddr': c['next_hop'], 'hostname': fqdn.split('.')[0], 'descr': 'Virtualwan ' + ip, 'arp_table_static_entry': 'yes'}, ['mac', 'ipaddr', 'arp_table_static_entry'], expected={'ipaddr': c['next_hop']} if match else None)
            self.apply('services_dhcp.php?if=' + c['public_interface'])
            root = self.snapshot('after-arp')
        gwname = 'VWAN_' + ip.replace('.', '')
        gateways = root.findall('gateways/gateway_item')
        gateway = next((x for x in gateways if x.findtext('name') == gwname), None)
        if gateway is not None:
            if gateway.findtext('gateway') != c['next_hop'] or gateway.findtext('interface') != c['public_interface'] or gateway.find('monitor_disable') is None:
                raise ValueError('Gateway conflict')
        else:
            self.form('system_gateways_edit.php', {'interface': c['public_interface'], 'ipprotocol': 'inet', 'name': gwname, 'gateway': c['next_hop'], 'monitor_disable': 'yes', 'action_disable': 'yes', 'descr': fqdn}, ['name', 'gateway', 'interface'])
            self.apply('system_gateways.php')
            root = self.snapshot('after-gateway')
        routes = root.findall('staticroutes/route')
        route = next((x for x in routes if x.findtext('network') == ip + '/32'), None)
        if route is not None and (route.findtext('gateway') != gwname or route.find('disabled') is not None):
            raise ValueError('Route conflict')
        if route is None:
            self.form('system_routes_edit.php', {'network': ip, 'network_subnet': '32', 'gateway': gwname, 'descr': fqdn}, ['network', 'network_subnet', 'gateway'])
            self.apply('system_routes.php')
            root = self.snapshot('after-route')
        # Dedicated per-IP no-NAT goes first; never change the global outbound mode.
        descr = 'Virtualwan no NAT ' + ip
        if not any(x.findtext('descr') == descr for x in root.findall('nat/outbound/rule')):
            self.form('firewall_nat_out_edit.php?after=-1', {'interface': c['wireguard_interface'], 'ipprotocol': 'inet', 'protocol': 'any', 'nonat': 'yes', 'source_type': 'network', 'source': ip, 'source_subnet': '32', 'destination_type': 'any', 'descr': descr, 'after': '-1'}, ['nonat', 'source', 'source_type', 'interface'])
            self.apply('firewall_nat_out.php')
            root = self.snapshot('after-no-nat')
        # Outgoing uses the existing alias/policy gateway. Keep its LAN blocking policy.
        outbound = [x for x in root.findall('filter/rule') if x.findtext('type') == 'pass' and x.findtext('interface') == c['public_interface'] and x.findtext('source/address') == alias and x.findtext('gateway') == c['wireguard_gateway'] and x.find('disabled') is None]
        if not outbound:
            raise ValueError('Existing outbound public-alias policy-routing rule missing')
        descr = 'Virtualwan incoming ' + ip
        if not any(x.findtext('descr') == descr for x in root.findall('filter/rule')):
            self.form(f'firewall_rules_edit.php?if={c["wireguard_interface"]}&after=-1', {'type': 'pass', 'interface': c['wireguard_interface'], 'ipprotocol': 'inet', 'proto': 'any', 'srctype': 'any', 'dsttype': 'single', 'dst': ip, 'dstmask': '32', 'gateway': '', 'descr': descr, 'after': '-1'}, ['type', 'interface', 'srctype', 'dsttype', 'gateway'])
            self.apply('firewall_rules.php?if=' + c['wireguard_interface'])
            root = self.snapshot('after-inbound')
        mapping = next(m for m in root.findall('dhcpd/' + c['public_interface'] + '/staticmap') if m.findtext('ipaddr') == c['next_hop'])
        assert mapping.findtext('mac', '').lower() == mac.lower() and mapping.find('arp_table_static_entry') is not None
        assert any(r.findtext('network') == ip + '/32' and r.findtext('gateway') == gwname for r in root.findall('staticroutes/route'))
        nat = next(r for r in root.findall('nat/outbound/rule') if r.findtext('descr') == 'Virtualwan no NAT ' + ip)
        if nat.find('nonat') is None or nat.findtext('interface') != c['wireguard_interface'] or nat.findtext('source/network') != ip + '/32' or nat.find('disabled') is not None:
            raise ValueError('Managed no-NAT rule is inconsistent')
        incoming = next(r for r in root.findall('filter/rule') if r.findtext('descr') == 'Virtualwan incoming ' + ip)
        if incoming.findtext('type') != 'pass' or incoming.findtext('interface') != c['wireguard_interface'] or incoming.findtext('destination/address') != ip or incoming.find('disabled') is not None or incoming.find('disablereplyto') is not None or incoming.findtext('gateway'):
            raise ValueError('Managed incoming rule is inconsistent')
        self.log.event('pfsense.config.verified', ip=ip, mac=mac, next_hop=c['next_hop'], gateway=gwname)
        return root
