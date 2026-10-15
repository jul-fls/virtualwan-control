"""A real HTTP/WebSocket loopback probe; fake SSH bytes, never a production host."""
import asyncio
import json
import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import requests
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus,ConnectionClosed
from virtualwan.server import Application
from virtualwan.web import create_app


class Channel:
    def __init__(self):self.data=queue.Queue();self.data.put(b'RFB 003.008\n');self.closed=False
    def recv(self,size):
        if self.closed:return b''
        try:return self.data.get(timeout=.05)
        except queue.Empty:raise socket.timeout()
    def recv_ready(self):return not self.data.empty()
    def exit_status_ready(self):return self.closed
    def sendall(self,value):self.data.put(value)
    def close(self):self.closed=True


class Backend:
    def __init__(self,*args):self.channel=Channel()
    def __enter__(self):return self
    def __exit__(self,*args):self.channel.close()
    def console(self,password):
        assert len(password)>=20
        return self.channel


class ConsoleWebSocketTests(unittest.TestCase):
    def test_authenticated_bidirectional_socket_one_use_and_revocation(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('VWAN_TEST_TMPDIR')) as directory:
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];origin=f'http://localhost:{port}'
            with patch.dict(os.environ,{'VWAN_PUBLIC_URL':origin},clear=True):
                app=create_app({'TESTING':True,'START_WORKER':False,'ADMIN_PASSWORD':'AdminPasswordForTests123','DATA_DIR':directory,'DEMO':False,'CLIENT_PVE_FACTORY':Backend,'CONSOLE_CHECK_INTERVAL':.05})
                accounts=app.extensions['accounts'];store=app.extensions['store'];control=app.extensions['control'];control.allow_provision=True
                profile=control.add_profile(json.loads((Path(__file__).parents[1]/'examples/profile.json').read_text()))
                account=accounts.save({'name':'Client','email':'socket@example.net','password':'CustomerPassword123'})
                with store.tx() as db:
                    db.execute('INSERT INTO deployments VALUES(?,?,?,?,?)',('testvm','8.8.8.8',profile,None,json.dumps({'status':'active','vmid':120,'mac':'02:00:00:00:00:01'})))
                    db.execute('INSERT INTO client_vms(deployment_id,account_id) VALUES(?,?)',('testvm',account))
                server=uvicorn.Server(uvicorn.Config(Application(app),log_level='error',access_log=False,lifespan='off'))
                thread=threading.Thread(target=lambda:asyncio.run(server.serve(sockets=[sock])),daemon=True);thread.start()
                try:
                    for _ in range(100):
                        if server.started:break
                        time.sleep(.03)
                    session=requests.Session();r=session.post(origin+'/api/portal/login',json={'email':'socket@example.net','password':'CustomerPassword123'},timeout=5);self.assertEqual(r.status_code,200)
                    csrf=r.json()['csrf'];ticket=session.post(origin+'/api/portal/vps/testvm/console',json={},headers={'X-CSRF-Token':csrf},timeout=5).json()
                    cookie='vwan_client='+session.cookies.get('vwan_client')
                    async def exercise():
                        with self.assertRaises(InvalidStatus):
                            async with connect(f'ws://localhost:{port}/ws/console',origin='https://evil.example',additional_headers={'Cookie':cookie}):pass
                        with self.assertRaises(InvalidStatus):
                            async with connect(f'ws://localhost:{port}/ws/console',origin=origin):pass
                        async with connect(f'ws://localhost:{port}/ws/console',origin=origin,additional_headers={'Cookie':cookie}) as ws:
                            await ws.send(json.dumps({'ticket':ticket['ticket']}))
                            self.assertEqual(await asyncio.wait_for(ws.recv(),3),b'RFB 003.008\n')
                            await ws.send(b'RFB 003.008\n');self.assertEqual(await asyncio.wait_for(ws.recv(),3),b'RFB 003.008\n')
                            async with connect(f'ws://localhost:{port}/ws/console',origin=origin,additional_headers={'Cookie':cookie}) as duplicate:
                                await duplicate.send(json.dumps({'ticket':ticket['ticket']}))
                                with self.assertRaises(ConnectionClosed):await asyncio.wait_for(duplicate.recv(),3)
                            with store.tx() as db:db.execute('DELETE FROM client_sessions WHERE account_id=?',(account,))
                            with self.assertRaises(ConnectionClosed):await asyncio.wait_for(ws.recv(),3)
                    asyncio.run(exercise())
                finally:
                    server.should_exit=True;thread.join(timeout=5);sock.close()
                self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main()
