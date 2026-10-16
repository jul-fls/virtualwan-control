import json,os,tempfile,unittest
from unittest.mock import MagicMock,patch
from virtualwan.adapters.client_pve import ClientPVE,GraphicalPreparationError
from virtualwan.web import create_app
from virtualwan import domain

ISO='iso:iso/rescue.iso'
AUTH={'volid':'iso:iso/virtualwan-rescue-auth-120-'+('a'*32)+'.iso','session':'a'*32}
class RescueWorkflow(unittest.TestCase):
    def setUp(self):
        self.store=MagicMock();self.p=ClientPVE(self.store,{}, {'id':'vm','vmid':120,'mac':'02:00:00:00:00:01','ip':'198.51.100.23'})
        self.p.node='pve2';self.p.base='/nodes/pve2/qemu/120'
        self.config={'digest':'0','boot':'order=virtio0;ide2','virtio0':'disk:vm-120-disk-0','ide2':'disk:vm-120-cloudinit,media=cdrom'}
        self.p.config=dict(self.config);self.calls=[];self.fail_boot=False;self.fail_start=False
        def api(method,suffix,**params):
            self.calls.append((method,suffix,params))
            if suffix=='/config':
                if method=='get':return dict(self.config)
                if self.fail_boot and 'boot' in params:raise domain.Conflict('Échec du changement boot')
                self.assertEqual(params.pop('digest'),self.config['digest'])
                for key in params.pop('delete','').split(','):
                    if key:self.config.pop(key,None)
                self.config.update(params);self.config['digest']=str(int(self.config['digest'])+1)
            elif suffix.endswith('/content'):return [{'volid':ISO}]
            elif suffix=='/status/start' and self.fail_start:raise domain.Conflict('Démarrage refusé')
            elif suffix.startswith('/status/'):return 'UPID:task'
        self.api=patch.object(self.p,'api',side_effect=api);self.api.start();self.addCleanup(self.api.stop)
        self.status=patch.object(self.p,'status',return_value={'status':'stopped'});self.status.start();self.addCleanup(self.status.stop)
        self.media=patch('virtualwan.adapters.rescue_media.media',return_value=AUTH);self.media_mock=self.media.start();self.addCleanup(self.media.stop)
    def test_configure_verifies_media_then_boot_and_persists_before_return(self):
        checkpoints=[]
        plan=self.p.configure_rescue(ISO,password='Abcd2345EFGH',checkpoint=lambda p,c:checkpoints.append((dict(p),c)))
        self.assertEqual(self.config['boot'],'order=ide0;virtio0')
        self.assertEqual([c for m,p,c in self.calls if m=='set' and p=='/config'][0].keys(),{'ide0','ide1'})
        self.assertEqual([c for m,p,c in self.calls if m=='set' and p=='/config'][1],{'boot':'order=ide0;virtio0'})
        self.assertEqual([c for p,c in checkpoints],[False,True]);self.assertEqual(self.config['ide2'],'disk:vm-120-cloudinit,media=cdrom')
        self.p.configure_rescue('',True,plan,checkpoint=lambda p,c:None)
        self.assertEqual(self.config['boot'],'order=virtio0;ide2');self.assertNotIn('ide0',self.config);self.assertNotIn('ide1',self.config)
        # Retry when detach succeeded but cleanup was interrupted.
        before=len(self.calls);self.p.configure_rescue('',True,plan)
        self.assertFalse(any(m=='set' for m,p,c in self.calls[before:]))
    def test_partial_attach_resumes_same_media_and_retains_original_boot(self):
        checkpoints=[];self.fail_boot=True
        with self.assertRaises(domain.Conflict):self.p.configure_rescue(ISO,password='Abcd2345EFGH',checkpoint=lambda p,c:checkpoints.append(dict(p)))
        self.assertIn('ide1',self.config);plan=checkpoints[0];self.fail_boot=False;self.p.config=dict(self.config)
        result=self.p.configure_rescue(ISO,saved=plan,password='Abcd2345EFGH')
        self.assertEqual(result,plan);self.assertEqual(self.config['boot'],'order=ide0;virtio0')
        self.media_mock.assert_called_with(self.p,'create',iso=ISO,password='Abcd2345EFGH',saved=plan)
    def test_foreign_media_never_overwritten(self):
        self.config['ide0']='customer:iso/other.iso,media=cdrom';self.p.config=dict(self.config)
        with self.assertRaises(domain.Conflict):self.p.configure_rescue(ISO,password='Abcd2345EFGH')
        self.media_mock.assert_not_called();self.assertFalse(any(m=='set' for m,p,c in self.calls))
    def test_old_orphan_is_verified_replaced_and_cleaned_up(self):
        self.config.update(ide0=ISO+',media=cdrom,size=1M',ide1=AUTH['volid']+',media=cdrom',boot='order=ide0;virtio0;ide1');self.p.config=dict(self.config)
        plan=self.p.configure_rescue(ISO,password='Abcd2345EFGH')
        self.assertEqual(plan['boot'],'order=virtio0')
        self.assertEqual([c.args[1] for c in self.media_mock.call_args_list],['inspect','create','delete'])
    def test_switch_shuts_down_waits_configures_checkpoints_then_starts(self):
        with patch.object(self.p,'status',side_effect=[{'status':'running'},{'status':'stopped'}]),patch.object(self.p,'check_identity'),patch.object(self.p,'wait_task') as task,patch.object(self.p,'wait_power') as power,patch.object(self.p,'configure_rescue',return_value={'saved':True}) as configure:
            def config(*a):
                self.assertEqual(self.calls[-1][1],'/status/shutdown');self.assertEqual(task.call_count,1);self.assertEqual(power.call_count,1);return {'saved':True}
            configure.side_effect=config
            result=self.p.rescue(ISO,password='Abcd2345EFGH')
            self.assertEqual(result,{'saved':True});self.assertEqual(task.call_count,2);self.assertEqual(power.call_count,2)
            self.assertIn(('create','/status/shutdown',{'timeout':60,'forceStop':1}),self.calls);self.assertEqual(self.calls[-1][1],'/status/start')
    def test_shutdown_failure_never_changes_config(self):
        with patch.object(self.p,'status',return_value={'status':'running'}),patch.object(self.p,'wait_task',side_effect=GraphicalPreparationError('Arrêt non confirmé')),patch.object(self.p,'configure_rescue') as configure:
            with self.assertRaisesRegex(domain.Conflict,'Arrêt non confirmé'):self.p.rescue(ISO,password='Abcd2345EFGH')
            configure.assert_not_called()
    def test_pvesh_config_notification_is_not_json_and_reads_stay_strict(self):
        self.api.stop()
        with patch.object(self.p,'command',return_value='update VM 120: -boot order=ide0;virtio0\n'):
            self.assertIsNone(self.p.api('set','/config',boot='order=ide0;virtio0'))
            with self.assertRaises(json.JSONDecodeError):self.p.api('get','/config')
        with patch.object(self.p,'command',return_value='VM stopped after timeout\n"UPID:pve2:12345678:12345678:12345678:qmshutdown:120:root@pam:"\n'):
            self.assertTrue(self.p.api('create','/status/shutdown').startswith('UPID:'))
        with patch.object(self.p,'command',return_value='update VM 120: -description marker\n\nClient: example\n'):
            self.assertIsNone(self.p.api('set','/config',description='marker\n\nClient: example'))
        with patch.object(self.p,'command',return_value='unexpected garbage'):
            with self.assertRaises(json.JSONDecodeError):self.p.api('set','/config',boot='order=ide0;virtio0')
    def test_media_failure_logs_without_duplicate_action_argument(self):
        self.media.stop()
        from virtualwan.adapters.rescue_media import media
        out=MagicMock();out.read.return_value=b'';out.channel.recv_exit_status.return_value=1
        err=MagicMock();err.read.return_value=b'synthetic failure'
        self.p.ssh=MagicMock();self.p.prefix=[];self.p.ssh.exec_command.return_value=(MagicMock(),out,err)
        # A real-signature event rejects duplicate action keyword arguments.
        def event(run,action,**values):self.assertEqual(values['media_action'],'create')
        self.store.event.side_effect=event
        with self.assertRaisesRegex(domain.Conflict,'média rescue'):media(self.p,'create',iso=ISO,password='Abcd2345EFGH')
