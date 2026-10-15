"""Native HTTPS forms for optional routed IPv6; no full config.xml restore."""
import ipaddress
import re
from bs4 import BeautifulSoup
from ..ipv6 import ipv6_settings,ipv6_validate_address


class PfSenseIPv6:
    @staticmethod
    def _v6_alias(root,name):
        matches=[(i,a) for i,a in enumerate(root.findall('aliases/alias')) if a.findtext('name')==name]
        if len(matches)>1:raise ValueError('Duplicate IPv6 alias')
        return matches[0] if matches else (None,None)

    @staticmethod
    def _v6_values(alias):
        return (alias.findtext('address','').split() if alias is not None else [])

    def _v6_alias_write(self,root,name,kind,values):
        from .pfsense import fields
        index,alias=self._v6_alias(root,name)
        page='firewall_aliases_edit.php'+('?id='+str(index) if alias is not None else '')
        data=fields(self.request('GET',page).text,['name','type','address0'])
        if alias is not None:
            if data['name']!=name or data['type']!=kind:raise ValueError('IPv6 alias target changed')
            observed=[]
            for key,value in data.items():
                if re.fullmatch(r'address\d+',key) and value:
                    suffix=key[7:];mask=data.get('address_subnet'+suffix,'128')
                    observed.append(str(ipaddress.IPv6Network(value+'/'+mask,strict=False)) if kind=='network' else str(ipaddress.IPv6Address(value)))
            expected=[str(ipaddress.IPv6Network(x)) if kind=='network' else str(ipaddress.IPv6Address(x)) for x in self._v6_values(alias)]
            if set(observed)!=set(expected):raise ValueError('IPv6 alias changed concurrently')
        for key in list(data):
            if re.fullmatch(r'(address|address_subnet|detail)\d+',key):data.pop(key)
        data.update(name=name,type=kind,descr='Virtualwan IPv6 '+('clients' if kind=='host' else 'protected networks'),save='Save')
        for i,value in enumerate(values):
            net=ipaddress.IPv6Network(value,strict=False)
            data.update({f'address{i}':str(net.network_address),f'address_subnet{i}':str(net.prefixlen),f'detail{i}':'Virtualwan IPv6'})
        response=self.request('POST',page,data=data)
        if BeautifulSoup(response.text,'html.parser').select('.input-errors, .alert-danger'):raise ValueError('pfSense refused IPv6 alias')
        self.apply('firewall_aliases.php')
        after=self.snapshot('ipv6.alias.after');_,actual=self._v6_alias(after,name)
        expected={str(ipaddress.IPv6Network(x)) if kind=='network' else str(ipaddress.IPv6Address(x)) for x in values}
        observed={str(ipaddress.IPv6Network(x)) if kind=='network' else str(ipaddress.IPv6Address(x)) for x in self._v6_values(actual)}
        if actual is None or observed!=expected:raise ValueError('IPv6 alias readback failed')
        self.log.event('pfsense.ipv6.alias.updated',alias=name,addresses=sorted(expected))
        return after

    def _v6_specs(self,c):
        a=c['public_alias'];blocked=c['protected_alias'];interface=self.cfg['public_interface']
        return [('firewall',interface,'block',a,'(self)'),('clients',interface,'pass',a,a),('protected',interface,'block',a,blocked),('internet',interface,'pass',a,'2000::/3'),('incoming',c['wan_interface'],'pass',None,a)]

    def _v6_rules(self,root,c):
        specs=self._v6_specs(c);owned={}
        for index,r in enumerate(root.findall('filter/rule')):
            if r.findtext('source/address')!=c['public_alias'] and r.findtext('destination/address')!=c['public_alias']:continue
            signature=(r.findtext('interface'),r.findtext('type'),r.findtext('source/address'),r.findtext('destination/address') or r.findtext('destination/network'))
            candidates=[spec for spec in specs if tuple(spec[1:])==signature]
            if len(candidates)!=1 or not r.findtext('descr','').startswith('Virtualwan IPv6 ') or r.findtext('ipprotocol')!='inet6' or r.find('disabled') is not None or r.findtext('gateway') or r.findtext('protocol') not in (None,'any') or any(r.find(x) is not None for x in ['source/not','destination/not','source/port','destination/port','floating']) or r.findtext('sched'):
                raise ValueError('Foreign or modified rule references IPv6 alias '+c['public_alias'])
            key=candidates[0][0]
            if key in owned:raise ValueError('Duplicate managed IPv6 rule')
            owned[key]=(index,r)
        order=[owned[x][0] for x in ['firewall','clients','protected','internet'] if x in owned]
        if order!=sorted(order):raise ValueError('IPv6 rules out of order; reconcile before provisioning')
        return owned

    def preflight_ipv6(self,root,settings):
        c=ipv6_settings(settings)
        if c is None:return
        interface=root.find('interfaces/'+self.cfg['public_interface'])
        if interface is None or interface.findtext('ipaddrv6')!=c['gateway_address'] or interface.findtext('subnetv6')!='64' or interface.findtext('gatewayv6'):
            raise ValueError('Configure the routed IPv6 /64 on the pfSense client interface first, with no upstream gateway')
        if root.find('interfaces/'+c['wan_interface']) is None:raise ValueError('IPv6 WAN interface missing')
        for name,kind in [(c['public_alias'],'host'),(c['protected_alias'],'network')]:
            _,a=self._v6_alias(root,name)
            if a is None:continue
            if a.findtext('type')!=kind or not a.findtext('descr','').startswith('Virtualwan IPv6 '):raise ValueError('IPv6 alias ownership mismatch: '+name)
            values=self._v6_values(a)
            if kind=='host':
                for address in values:ipv6_validate_address(c,address)
            elif list(ipaddress.collapse_addresses(ipaddress.IPv6Network(x) for x in values))!=[ipaddress.IPv6Network(x) for x in c['protected_networks']]:
                raise ValueError('IPv6 protected networks differ from profile')
        self._v6_rules(root,c)

    def configure_ipv6(self,root,settings,address):
        c=ipv6_settings(settings);address=ipv6_validate_address(c,address);self.preflight_ipv6(root,c)
        _,blocked=self._v6_alias(root,c['protected_alias'])
        if blocked is None:root=self._v6_alias_write(root,c['protected_alias'],'network',c['protected_networks'])
        _,clients=self._v6_alias(root,c['public_alias']);values=self._v6_values(clients)
        if address not in values:root=self._v6_alias_write(root,c['public_alias'],'host',values+[address])
        for key,interface,action,source,dest in reversed(self._v6_specs(c)):
            if key in self._v6_rules(root,c):continue
            updates={'type':action,'interface':interface,'ipprotocol':'inet6','proto':'any','srctype':'single' if source else 'any','src':source or 'any','srcmask':'128','dsttype':'(self)' if dest=='(self)' else 'network' if dest=='2000::/3' else 'single','dst':'2000::' if dest=='2000::/3' else dest,'dstmask':'3' if dest=='2000::/3' else '128','gateway':'','after':'-1','descr':'Virtualwan IPv6 '+c['public_alias']+' '+key}
            if action=='block':updates['log']='yes'
            self.form('firewall_rules_edit.php?if='+interface+'&after=-1',updates,['type','interface','srctype','dsttype','gateway']);self.apply('firewall_rules.php?if='+interface)
            root=self.snapshot('ipv6.rule.after')
        if len(self._v6_rules(root,c))!=5:raise ValueError('IPv6 rules incomplete')
        self.log.event('pfsense.ipv6.configured',address=address,alias=c['public_alias'])

    def retire_ipv6(self,settings,address,check_only=False):
        c=ipv6_settings(settings);address=ipv6_validate_address(c,address);root=self.snapshot('ipv6.retire.before');self.preflight_ipv6(root,c)
        _,alias=self._v6_alias(root,c['public_alias']);values=self._v6_values(alias)
        if address not in values:
            if not check_only:self.native_post('diag_dump_states.php',{'filter':address,'killfilter':'yes'})
            return
        retained=[x for x in values if x!=address]
        owned=self._v6_rules(root,c)
        if not retained:
            allowed={id(r) for _,r in owned.values()}
            for node in root:
                for child in node:
                    if id(child) in allowed:continue
                    if any(c['public_alias'] in (x.text or '').split() for x in child.iter() if x.tag in ('address','network')):raise ValueError('IPv6 alias referenced by a foreign object')
        if check_only:return
        if retained:self._v6_alias_write(root,c['public_alias'],'host',retained)
        else:
            for key in list(owned):
                root=self.snapshot('ipv6.retire.rule.before');current=self._v6_rules(root,c)
                if key not in current:continue
                index,r=current[key];page='firewall_rules.php?if='+r.findtext('interface')
                self.native_post(page,{'act':'del','id':str(index)});self.apply(page)
                if key in self._v6_rules(self.snapshot('ipv6.retire.rule.after'),c):raise ValueError('IPv6 rule cleanup incomplete')
            root=self.snapshot('ipv6.retire.alias.before');index,a=self._v6_alias(root,c['public_alias'])
            # Never remove an alias still referenced by another object.
            if any(c['public_alias'] in (x.text or '').split() for x in root.iter() if x.tag in ('address','network')):raise ValueError('IPv6 alias still referenced')
            if a is not None:
                self.native_post('firewall_aliases.php',{'act':'del','id':str(index)});self.apply('firewall_aliases.php')
                if self._v6_alias(self.snapshot('ipv6.retire.alias.after'),c['public_alias'])[1] is not None:raise ValueError('IPv6 alias cleanup incomplete')
        self.native_post('diag_dump_states.php',{'filter':address,'killfilter':'yes'})
        self.log.event('pfsense.ipv6.retired',address=address,retained=retained)
