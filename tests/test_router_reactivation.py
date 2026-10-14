import base64,hashlib,io,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from virtualwan.adapters import remote
from test_control import TESTROOT

@unittest.skipIf(sys.platform=='win32','Remote actions require Linux')
class RouterReactivation(unittest.TestCase):
    def exercise(self,fail=False,bad_hash=False):
        source=(Path(__file__).parents[1]/'tools/virtualwan-router.py').read_bytes()
        with tempfile.TemporaryDirectory(dir=TESTROOT) as directory:
            root=Path(directory);cfg=root/'virtualwan.json';binary=root/'virtualwan'
            before={'primary_ip':'9.9.9.9','additional_ips':['1.1.1.1'],'inactive_ips':['8.8.8.8'],'wan_interface':'ens3'}
            cfg.write_text(json.dumps(before));original=b'# virtualwan wg-vwan\ndef firewall(c): pass\n';binary.write_bytes(original)
            q={'action':'vps_apply','path':str(cfg),'primary_ip':'9.9.9.9','ip':'8.8.8.8','run_id':'test-run','source_base64':base64.b64encode(source).decode(),'source_sha256':'wrong' if bad_hash else hashlib.sha256(source).hexdigest()}
            def paths(value):
                return {'/usr/local/sbin/virtualwan':binary,'/var/lib/virtualwan/runs':root/'runs'}.get(str(value),Path(value))
            reloads=[]
            def command(*args,**kwargs):
                if args[0]=='ip':return '1.1.1.1 via 9.9.9.1 dev ens3 src 9.9.9.9'
                if args[0]=='systemctl':
                    reloads.append(1)
                    if fail and len(reloads)==1:raise RuntimeError('Reload failed')
                    return ''
                if args[0]==str(binary) and args[1]=='validate':return ''
                raise AssertionError(args)
            with patch.object(sys,'stdin',io.StringIO(json.dumps(q))),patch.object(remote,'Path',side_effect=paths),patch.object(remote,'run',side_effect=command),patch.object(remote,'emit') as emit,patch.object(remote.subprocess,'run',return_value=Mock(stdout='nft syntax error\nPrivateKey = TEST_PRIVATE_KEY',returncode=0)):
                if fail or bad_hash:
                    with self.assertRaises((RuntimeError,ValueError)) as error:remote.main()
                    if fail:self.assertIn('nft syntax error',str(error.exception));self.assertNotIn('TEST_PRIVATE_KEY',str(error.exception))
                    self.assertEqual(json.loads(cfg.read_text()),before);self.assertEqual(binary.read_bytes(),original)
                else:
                    remote.main();after=json.loads(cfg.read_text())
                    self.assertEqual(after['additional_ips'],['1.1.1.1','8.8.8.8']);self.assertEqual(after['inactive_ips'],[])
                    self.assertEqual(binary.read_bytes(),source);self.assertEqual((root/'runs/test-run/router.before.py').read_bytes(),original)
                    self.assertTrue(emit.call_args.args[0]['router_updated'])
    def test_reactivation_updates_installed_tool_and_keeps_backup(self):self.exercise()
    def test_failed_reload_restores_script_and_config(self):self.exercise(fail=True)
    def test_bad_digest_never_changes_script_or_config(self):self.exercise(bad_hash=True)
