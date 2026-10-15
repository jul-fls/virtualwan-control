import io,json,tempfile,unittest,sys
from pathlib import Path
from unittest.mock import patch
from virtualwan.adapters import remote
from test_control import TESTROOT

class KeyboardTemplate(unittest.TestCase):
    def test_new_clone_installs_persistent_french_console_keymap_without_changing_network(self):
        with tempfile.TemporaryDirectory(dir=TESTROOT) as directory:
            p={'vmid':123,'template':9001,'disk_storage':'ssd','snippet_storage':'snippets','snippet_directory':directory,'bridge':'vmbr12','cores':2,'memory_mb':2048,'disk_gb':32}
            q={'action':'pve_create','pve':p,'fqdn':'vps-test.example.net','ip':'8.8.8.8','public_key':'synthetic','guest':{'username':'debian','ssh_auth':'key','interface':'ens18','gateway':'10.254.251.1','mtu':1420,'dns':['1.1.1.1']}}
            config={'net0':'virtio=02:00:00:00:00:01,bridge=vmbr12','serial0':'socket'}
            def run(*args,**kwargs):return '[]' if args[0]=='pvesh' else ''
            with patch.object(sys,'stdin',io.StringIO(json.dumps(q))),patch.object(remote,'run',side_effect=run) as command,patch.object(remote,'api',return_value=config),patch.object(remote,'register_isolation'),patch.object(remote,'emit'):
                remote.main()
            user=json.loads((Path(directory)/'virtualwan-123-user.yaml').read_text().split('\n',1)[1])
            files={f['path']:f['content'] for f in user['write_files']}
            self.assertIn('XKBLAYOUT="fr"',files['/etc/default/keyboard'])
            self.assertIn('console-setup',user['packages']);self.assertIn(['setupcon','--force'],user['runcmd'])
            command.assert_any_call('qm','set','123','--vga','std','--keyboard','fr')
            network=json.loads((Path(directory)/'virtualwan-123-network.yaml').read_text())
            self.assertEqual(network['config'][0]['subnets'][0]['address'],'8.8.8.8/32')
