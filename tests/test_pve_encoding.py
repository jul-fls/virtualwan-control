import json,unittest
from unittest.mock import MagicMock
from virtualwan import domain
from virtualwan.adapters.client_pve import ClientPVE,decode_pve_output


class PVENotesEncoding(unittest.TestCase):
    def remote(self,stdout,stderr=b'',exit_code=0):
        out,err=MagicMock(),MagicMock();out.read.return_value=stdout;err.read.return_value=stderr;out.channel.recv_exit_status.return_value=exit_code
        return MagicMock(),out,err
    def backend(self):
        p=ClientPVE(MagicMock(),{}, {'id':'test','vmid':120});p.prefix=[];p.base='/nodes/test/qemu/120';p.ssh=MagicMock();return p
    def test_notes_sync_accepts_legacy_progress_but_verifies_exact_utf8_notes(self):
        p=self.backend();notes='Client : José\nGamme : Hébergé — 東京 🚀'
        p.ssh.exec_command.side_effect=[
            self.remote(json.dumps({'digest':'before','description':'Anciennes notes'}).encode()),
            self.remote(b'update VM 120: -description Client : Jos\xe9\n'+ 'Gamme : Hébergé — 東京 🚀\n'.encode(),b'Avertissement : d\xe9j\xe0 trait\xe9\n'),
            self.remote(json.dumps({'digest':'after','description':notes},ensure_ascii=False).encode('utf-8'))]
        p.set_notes(notes)
        self.assertEqual(p.config['description'],notes)
        # The outgoing command and authoritative readback retain every character.
        self.assertIn(notes,p.ssh.exec_command.call_args_list[1].args[0])
        self.assertEqual(p.ssh.exec_command.call_count,3)
    def test_failed_command_preserves_accented_diagnostic_and_exit_status(self):
        p=self.backend();p.ssh.exec_command.return_value=self.remote(b'',b'Op\xe9ration refus\xe9e',1)
        with self.assertRaises(domain.Conflict):p.command(['pvesh','set',p.base+'/config'])
        self.assertEqual(p.store.event.call_args.kwargs['stderr'],'Opération refusée')
        self.assertEqual(p.store.event.call_args.kwargs['exit_code'],1)
    def test_utf8_and_mixed_encoding_have_no_replacement_or_surrogate_characters(self):
        for raw,expected in [(b'Jos\xe9','José'),('Clé — 東京 🚀'.encode(),'Clé — 東京 🚀'),(b'Jos\xe9 / '+'Clé — 東京 🚀'.encode(),'José / Clé — 東京 🚀')]:
            self.assertEqual(decode_pve_output(raw),expected)
    def test_malformed_response_still_fails_json_validation(self):
        p=self.backend();p.ssh.exec_command.return_value=self.remote(b'R\xe9ponse invalide')
        with self.assertRaises(json.JSONDecodeError):p.api('get','/config')
