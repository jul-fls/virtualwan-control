import unittest
from unittest.mock import Mock
from virtualwan.adapters.provision import Cloudflare
from virtualwan.adapters.lifecycle import check_aliases

IP='8.8.8.8'
FQDN='vps-8888.hosting.example.net'
def record(name,ip=IP,**extra):return {'id':name,'type':'A','content':ip,'name':name,**extra}
class CloudflareFilters(unittest.TestCase):
    def cf(self,records):
        cf=Cloudflare.__new__(Cloudflare);cf.call=Mock(return_value=records);return cf
    def test_other_ip_cannot_create_alias_conflict(self):
        cf=self.cf([record(FQDN),record('unrelated.example.net','9.9.9.9')])
        check_aliases(cf,IP,FQDN,{},Mock())
        self.assertEqual(cf.call.call_args.kwargs['params']['content.exact'],IP)
    def test_real_alias_is_named_and_not_deleted(self):
        cf=self.cf([record(FQDN),record('legacy.example.net')])
        with self.assertRaisesRegex(ValueError,'legacy.example.net'):check_aliases(cf,IP,FQDN,{},Mock())
        self.assertTrue(all(c.args[0]=='GET' for c in cf.call.call_args_list))
    def test_managed_old_name_can_be_cleaned_after_preflight(self):
        cf=self.cf([record('old.example.net',comment='Virtualwan managed')])
        check_aliases(cf,IP,FQDN,{'fqdn':'old.example.net'},Mock(),preflight=True)
        with self.assertRaises(ValueError):check_aliases(cf,IP,FQDN,{'fqdn':'old.example.net'},Mock())
    def test_foreign_old_name_still_blocks_before_cleanup(self):
        cf=self.cf([record('old.example.net')])
        with self.assertRaises(ValueError):check_aliases(cf,IP,FQDN,{'fqdn':'old.example.net'},Mock(),preflight=True)
    def test_pagination_checks_later_pages(self):
        cf=self.cf([]);cf.call.side_effect=[[record('other'+str(i),'9.9.9.9') for i in range(100)],[record('late.example.net')]]
        self.assertEqual(cf.records_for_ip(IP)[0]['name'],'late.example.net')
        self.assertEqual(cf.call.call_args.kwargs['params']['page'],2)
    def test_name_filter_also_verified_locally(self):
        cf=self.cf([record(FQDN),record('unrelated.example.net','9.9.9.9')])
        self.assertEqual(cf.inspect(FQDN,IP),[record(FQDN)])
        self.assertEqual(cf.call.call_args.kwargs['params']['name.exact'],FQDN)
    def test_repeated_page_refused(self):
        cf=self.cf([record('other'+str(i)) for i in range(100)])
        with self.assertRaisesRegex(ValueError,'Pagination'):cf.records_for_ip(IP)

    def test_foreign_alias_preflight_prevents_any_remote_operation(self):
        import json,sys,tempfile
        from pathlib import Path
        from unittest.mock import patch
        from virtualwan.adapters import lifecycle
        from test_control import TESTROOT
        cf=self.cf([record('legacy.example.net')])
        with tempfile.TemporaryDirectory(dir=TESTROOT) as directory:
            path=Path(directory)/'lifecycle.json'
            path.write_text(json.dumps({'public_ip':IP,'domain':'hosting.example.net','retire_client':{'vmid':120,'fqdn':IP},'cloudflare':{},'ovh':{},'vps':{'primary_ip':'9.9.9.9'}}))
            with patch.object(sys,'argv',['lifecycle',str(path)]),patch.object(lifecycle,'Journal'),patch.object(lifecycle,'Ovh'),patch.object(lifecycle,'Cloudflare',return_value=cf),patch.object(lifecycle,'Remote') as remote:
                with self.assertRaisesRegex(ValueError,'legacy.example.net'):lifecycle.main()
                remote.assert_not_called()
