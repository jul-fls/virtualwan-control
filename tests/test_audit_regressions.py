"""Concrete edge cases found during the pre-publication project audit."""
import asyncio,io,json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from virtualwan import domain
from virtualwan.accounts import totp
from virtualwan.adapters import remote
from virtualwan.adapters.provision import Remote
from virtualwan.server import Application
import test_client_portal as portal_fixture
import test_control as control_fixture


class IdentityAudit(unittest.TestCase):
    setUp=portal_fixture.ClientPortalTests.setUp
    tearDown=portal_fixture.ClientPortalTests.tearDown
    def session(self):
        result=self.accounts.login('a@example.net','CustomerPasswordA123',None)
        return self.accounts.authenticate(result['token'])
    def test_second_pending_mfa_enrollment_cannot_claim_success(self):
        session=self.session();a=self.accounts.begin_totp(session,{'current_password':'CustomerPasswordA123'});b=self.accounts.begin_totp(session,{'current_password':'CustomerPasswordA123'})
        result=self.accounts.complete_totp(session,{'challenge':a['challenge'],'code':totp(a['secret'],int(time.time()//30))})
        before=self.store.one('SELECT totp,recovery FROM accounts WHERE id=?',(self.a,))
        with self.assertRaises(domain.Conflict):self.accounts.complete_totp(session,{'challenge':b['challenge'],'code':totp(b['secret'],int(time.time()//30))})
        self.assertEqual(self.store.one('SELECT totp,recovery FROM accounts WHERE id=?',(self.a,)),before);self.assertEqual(len(result['recovery_codes']),10)
    def test_future_mfa_tick_is_not_reusable_after_enrollment(self):
        with patch('virtualwan.accounts.time.time',return_value=1800000000):
            session=self.session();enrollment=self.accounts.begin_totp(session,{'current_password':'CustomerPasswordA123'});code=totp(enrollment['secret'],60000001)
            self.accounts.complete_totp(session,{'challenge':enrollment['challenge'],'code':code})
            row=self.store.one('SELECT * FROM accounts WHERE id=?',(self.a,));self.assertEqual(row['last_totp'],60000001);self.assertFalse(self.accounts.second_factor(row,code))
    def test_editing_disabled_account_does_not_implicitly_reenable_it(self):
        self.accounts.save({'name':'A','email':'a@example.net','enabled':False},self.a)
        self.accounts.save({'name':'Renamed','email':'a@example.net'},self.a)
        self.assertFalse(self.store.one('SELECT enabled FROM accounts WHERE id=?',(self.a,))['enabled'])
        self.assertIsNone(self.accounts.login('a@example.net','CustomerPasswordA123',None))
    def test_top_level_json_types_fail_cleanly_and_keep_auth_checks(self):
        for body in ([],[{}],'invalid',3):
            self.assertEqual(self.client.post('/api/login',json=body).status_code,400)
            self.assertEqual(self.client.post('/api/portal/login',json=body).status_code,400)
        self.assertEqual(self.client.post('/api/login',json={'username':'admin','password':None}).status_code,401)
        self.assertEqual(self.client.put('/api/portal/account',json=[]).status_code,401)
        login=self.client.post('/api/portal/login',json={'email':'a@example.net','password':'CustomerPasswordA123'})
        self.assertEqual(login.status_code,200);headers={'X-CSRF-Token':login.json['csrf']}
        self.assertEqual(self.client.put('/api/portal/account',json=[],headers=headers).status_code,400)
    def test_non_ascii_csrf_and_mfa_are_rejected_without_server_error(self):
        login=self.client.post('/api/portal/login',json={'email':'a@example.net','password':'CustomerPasswordA123'})
        self.assertEqual(login.status_code,200)
        self.assertEqual(self.client.put('/api/portal/account',json={},headers={'X-CSRF-Token':'é'}).status_code,403)
        session=self.session();enrollment=self.accounts.begin_totp(session,{'current_password':'CustomerPasswordA123'})
        with self.assertRaises(ValueError):self.accounts.complete_totp(session,{'challenge':enrollment['challenge'],'code':'éééééé'})
    def test_logs_redact_longest_secret_and_its_url_encoding(self):
        self.store.redactions.update({'sharedSecret','sharedSecretExtended','abc/secret+value'})
        self.assertEqual(self.store.clean('sharedSecretExtended sharedSecret abc%2Fsecret%2Bvalue'),'[REDACTED] [REDACTED] [REDACTED]')


class ControlAudit(unittest.TestCase):
    setUp=control_fixture.Tests.setUp
    tearDown=control_fixture.Tests.tearDown
    def test_missing_smtp_sender_does_not_fail_a_ready_quote(self):
        with patch.dict(os.environ,{'VWAN_SMTP_HOST':'smtp.example.net','VWAN_APPROVAL_EMAIL':'admin@example.net'},clear=True),patch('smtplib.SMTP') as smtp:
            q=self.control.request_quote({'profile_id':self.profile,'kind':'additional_ip','quantity':1,'planCode':'DEMO-ip','duration':'P1M','pricingMode':'default'})
            self.assertTrue(self.control.process_one());smtp.assert_not_called()
        purchase=self.store.document('purchases',q['purchase_id']);self.assertEqual(purchase['state'],'awaiting_approval')
        self.assertEqual(self.store.one("SELECT state FROM jobs WHERE kind='quote'")['state'],'succeeded')
        self.assertTrue(self.store.rows("SELECT id FROM events WHERE action='approval.email.failed'"))
    def test_delivery_poll_error_does_not_kill_worker(self):
        self.control.demo=False;self.control.stop=MagicMock();self.control.stop.is_set.side_effect=[False,False,True]
        with patch.object(self.store,'rows',side_effect=[RuntimeError('temporary database error'),[]]),patch.object(self.control,'process_one',return_value=False) as process:
            self.control.start();self.control.thread.join(2)
            self.assertFalse(self.control.thread.is_alive());process.assert_called_once()
    def test_bad_guest_credentials_are_validation_errors_before_reservation(self):
        for values in ({'ssh_auth':'key','public_key':None},{'ssh_auth':'password','password':None},{'ssh_auth':'password','password':'longPassword123\nsecondline'},{'ssh_auth':'key','public_key':control_fixture.KEY,'username':None}):
            with self.assertRaises(ValueError):self.control.request_deploy({'profile_id':self.profile,'model_id':self.model,**values})
        self.assertEqual(self.store.rows('SELECT id FROM jobs'),[])


class ExternalOutputAudit(unittest.TestCase):
    def test_remote_subprocess_handles_legacy_output_and_keeps_stdin_utf8(self):
        result=MagicMock(stdout=b'Pr\xe9par\xe9 '+ '東京'.encode(),stderr=b'',returncode=0)
        with patch.object(remote.subprocess,'run',return_value=result) as run,patch.object(remote,'audit'):
            self.assertEqual(remote.run('synthetic-tool',input='clé'),'Préparé 東京');self.assertEqual(run.call_args.kwargs['input'],'clé'.encode());self.assertNotIn('text',run.call_args.kwargs)
    def test_rescue_diagnostics_do_not_crash_decoder(self):
        from virtualwan.adapters.rescue_media import media
        p=MagicMock();p.prefix=[];p.client={'id':'probe','vmid':120,'mac':'02:00:00:00:00:01','ip':'8.8.8.8'}
        out,err=MagicMock(),MagicMock();out.read.return_value=b'';err.read.return_value=b'M\xe9dia refus\xe9';out.channel.recv_exit_status.return_value=1;p.ssh.exec_command.return_value=(MagicMock(),out,err)
        with self.assertRaises(domain.Conflict):media(p,'create',iso='iso:iso/rescue.iso',password='Abcd2345EFGH')
        self.assertEqual(p.store.event.call_args.kwargs['stderr'],'Média refusé')
    def test_provision_stderr_is_read_as_bytes_with_structured_events(self):
        p=Remote.__new__(Remote);p.cfg={'host':'test','sudo':False};p.path='/tmp/synthetic.py';p.log=MagicMock();p.log.run_id='probe';p.ssh=MagicMock()
        out,err=MagicMock(),MagicMock();out.read.return_value=b'{"ok": true}';out.channel.recv_exit_status.return_value=0
        err.channel.makefile_stderr.return_value=io.BytesIO(b'Pr\xe9paration\n{"event":"ready","name":"Jos\xe9"}\n')
        p.ssh.exec_command.return_value=(MagicMock(),out,err)
        self.assertEqual(p.call('inspect'),{'ok':True});err.channel.makefile_stderr.assert_called_once_with('rb')
        end=next(c for c in p.log.event.call_args_list if c.args[0]=='remote.end');self.assertEqual(end.kwargs['stderr'],'Préparation\n')
        step=next(c for c in p.log.event.call_args_list if c.args[0]=='remote.step');self.assertEqual(step.kwargs['details']['name'],'José')
    def test_provision_reader_failure_is_reported_not_silently_lost(self):
        p=Remote.__new__(Remote);p.cfg={'host':'test','sudo':False};p.path='/tmp/synthetic.py';p.log=MagicMock();p.log.run_id='probe';p.ssh=MagicMock()
        out,err=MagicMock(),MagicMock();out.read.return_value=b'{"ok": true}';err.channel.makefile_stderr.side_effect=OSError('broken stream');p.ssh.exec_command.return_value=(MagicMock(),out,err)
        with self.assertRaisesRegex(RuntimeError,'journal SSH'):p.call('inspect')
    def test_websocket_headers_allow_standard_non_utf8_bytes(self):
        messages=[]
        async def send(item):messages.append(item)
        async def receive():return {'type':'websocket.connect'}
        # An unrelated header must not crash the ASGI app before origin rejection.
        app=Application(MagicMock())
        asyncio.run(app({'type':'websocket','path':'/invalid','headers':[(b'x-label',b'caf\xe9')]},receive,send))
        self.assertEqual(messages,[{'type':'websocket.close','code':1008}])
    @unittest.skipIf(os.name=='nt','Remote action requires Linux fcntl')
    def test_retirement_accepts_pve_progress_before_json_task_id(self):
        import test_catalogue_lifecycle as fixture
        import sys
        query,registry,base=fixture.RemoteRetireTests.fixture(self,'pve_retire_stop')
        upid='UPID:pve-a:12345678:12345678:12345678:qmstop:120:root@pam:'
        def command(*args,**kw):
            if args[1]=='create':return 'VM stopped after timeout\n'+json.dumps(upid)+'\n'
            if any('/tasks/' in str(a) for a in args):return json.dumps({'status':'stopped','exitstatus':'OK'})
            if args[-3:]==('/status/current','--output-format','json'):return json.dumps({'status':'running'})
            if any(str(a).endswith('/status/current') for a in args):return json.dumps({'status':'running'})
            return base(*args,**kw)
        with patch.object(sys,'stdin',io.StringIO(json.dumps(query))),patch.object(remote.Path,'read_text',return_value=json.dumps(registry)),patch.object(remote,'run',side_effect=command),patch.object(remote,'emit') as emit:
            remote.main();self.assertTrue(emit.call_args.args[0]['stopped'])
    def test_uploaded_remote_helpers_and_rescue_source_are_standalone(self):
        from virtualwan.adapters.rescue_media import SOURCE
        compile(SOURCE,'<rescue-source>','exec')
        root=Path(remote.__file__).parents[1]
        combined=(root/'text.py').read_bytes()+b'\n'+(root/'ipv6.py').read_bytes()+b'\n'+Path(remote.__file__).read_bytes()
        compile(combined,'<provision-source>','exec')

class DomainValidationAudit(unittest.TestCase):
    def test_invalid_numbers_and_null_names_are_validation_errors(self):
        for value in (None,{},[],float('inf'),float('nan')):
            with self.assertRaises(ValueError):domain.positive(value,65535)
        for value in (None,{},[]):
            with self.assertRaises(ValueError):domain.label(value)
    def test_string_false_does_not_make_a_router_ready(self):
        profile=json.loads((Path(__file__).parents[1]/'examples/profile.json').read_text(encoding='utf-8'))
        profile['ready']='false'
        with self.assertRaisesRegex(ValueError,'booléen'):domain.profile(profile)
