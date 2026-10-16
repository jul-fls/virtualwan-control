"""One HTTP/WS listener; TLS remains the operator's reverse proxy responsibility."""
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import time
from urllib.parse import urlparse
from a2wsgi import WSGIMiddleware
import uvicorn
from .adapters.client_pve import ClientPVE
from .web import create_app
from .store import ident


class Application:
    def __init__(self,app):
        self.app=app;self.http=WSGIMiddleware(app,workers=8);self.active=0;self.by_account={}
    async def __call__(self,scope,receive,send):
        if scope['type']=='lifespan':
            while True:
                event=await receive()
                if event['type']=='lifespan.startup':await send({'type':'lifespan.startup.complete'})
                elif event['type']=='lifespan.shutdown':
                    self.app.extensions['control'].stop.set();await send({'type':'lifespan.shutdown.complete'});return
        if scope['type']!='websocket':return await self.http(scope,receive,send)
        from http.cookies import SimpleCookie
        headers={k.decode().lower():v.decode() for k,v in scope['headers']}
        origin=os.environ.get('VWAN_PUBLIC_URL','http://localhost:8080').rstrip('/')
        if scope['path'] not in ('/ws/console','/ws/admin-console') or headers.get('origin')!=origin or urlparse('http://'+headers.get('host','')).hostname!=urlparse(origin).hostname:
            await send({'type':'websocket.close','code':1008});return
        cookies=SimpleCookie();cookies.load(headers.get('cookie',''))
        admin=scope['path']=='/ws/admin-console'
        token=cookies['vwan_session' if admin else 'vwan_client'].value if ('vwan_session' if admin else 'vwan_client') in cookies else ''
        accounts=self.app.extensions['accounts'];store=self.app.extensions['store'];control=self.app.extensions['control']
        def authenticate():
            if not admin:return accounts.authenticate(token)
            rows=store.rows('SELECT * FROM sessions WHERE token=? AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time()))
            return {**rows[0],'account_id':'admin:'+rows[0]['user']} if rows else None
        session=await asyncio.to_thread(authenticate)
        if not session or self.active>=32 or self.by_account.get(session['account_id'],0)>=2:
            await send({'type':'websocket.close','code':1008});return
        if not control.demo and not control.allow_provision:
            await send({'type':'websocket.close','code':1008});return
        await receive();await send({'type':'websocket.accept'})
        backend=None;channel=None;tasks=[];counted=False;trace=None
        try:
            event=await asyncio.wait_for(receive(),10)
            if event['type']!='websocket.receive' or len(event.get('text',''))>2048:raise ValueError('Authentication required')
            handshake=json.loads(event['text']);account,data=await asyncio.to_thread(accounts.consume,handshake['ticket'],'console',session['account_id'])
            if data['session']!=session['token'] or bool(data.get('admin'))!=admin:raise ValueError('Wrong session')
            rows=store.rows('SELECT * FROM deployments WHERE id=?',(data['deployment_id'],)) if admin else store.rows('SELECT d.* FROM deployments d JOIN client_vms v ON v.deployment_id=d.id WHERE d.id=? AND v.account_id=?',(data['deployment_id'],account))
            if not rows:raise ValueError('Ownership changed')
            row=rows[0];client={**json.loads(row['data']),**{k:v for k,v in row.items() if k!='data'}}
            if (not admin and client.get('management_locked')) or client.get('status') not in ('active','stopped','imported','simulated'):raise ValueError('Unavailable')
            if self.active>=32 or self.by_account.get(account,0)>=2:raise ValueError('Console limit reached')
            self.active+=1;self.by_account[account]=self.by_account.get(account,0)+1;counted=True
            trace=ident();client['_operation_run']=trace;store.redactions.add(data['password'])
            store.event(trace,'client.console.opened',account_id=account,deployment_id=data['deployment_id'])
            if control.demo:
                await send({'type':'websocket.close','code':1000});return
            factory=self.app.config.get('CLIENT_PVE_FACTORY',ClientPVE)
            backend=factory(store,store.document('profiles',client['profile_id']),client)
            await asyncio.to_thread(backend.__enter__)
            channel=await asyncio.to_thread(backend.console,data['password'])
            async def incoming():
                while True:
                    event=await receive()
                    if event['type']=='websocket.disconnect':return
                    chunk=event.get('bytes')
                    if chunk is None or len(chunk)>1024*1024:raise ValueError('Binary frame required')
                    await asyncio.to_thread(channel.sendall,chunk)
            async def outgoing():
                while True:
                    if not channel.recv_ready():
                        if channel.closed or channel.exit_status_ready():return
                        await asyncio.sleep(.01);continue
                    chunk=await asyncio.to_thread(channel.recv,65536)
                    if not chunk:return
                    await send({'type':'websocket.send','bytes':chunk})
            async def expiry():
                started=time.monotonic()
                while time.monotonic()-started<3600:
                    await asyncio.sleep(self.app.config.get('CONSOLE_CHECK_INTERVAL',10))
                    live=await asyncio.to_thread(authenticate)
                    owner=store.rows('SELECT data FROM deployments WHERE id=?',(client['id'],)) if admin else store.rows('SELECT d.data FROM client_vms v JOIN deployments d ON d.id=v.deployment_id WHERE v.deployment_id=? AND v.account_id=?',(client['id'],account))
                    if owner and ((not admin and json.loads(owner[0]['data']).get('management_locked')) or json.loads(owner[0]['data']).get('status') not in ('active','stopped','imported','simulated')):return
                    if not live or not owner:return
            tasks=[asyncio.create_task(c()) for c in (incoming,outgoing,expiry)]
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:task.result()
        except Exception as error:
            if trace:store.event(trace,'client.console.failed',error_type=type(error).__name__,error=str(error))
            logging.getLogger(__name__).info('Client console closed or refused')
        finally:
            if channel:channel.close()
            for task in tasks:task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            if backend:await asyncio.to_thread(backend.__exit__,None,None,None)
            if counted:
                self.active-=1;self.by_account[account]-=1
            try:await send({'type':'websocket.close','code':1000})
            except Exception:pass


def main():
    logging.basicConfig(level=logging.INFO)
    proxy=os.environ.get('VWAN_TRUSTED_PROXY','').strip()
    if proxy:proxy=str(ipaddress.ip_address(proxy))
    # Access logs are disabled: challenges and account paths belong in scoped audit logs.
    uvicorn.run(Application(create_app()),host='0.0.0.0',port=8080,proxy_headers=bool(proxy),forwarded_allow_ips=proxy or '',access_log=False,ws_max_size=1024*1024,timeout_keep_alive=10)


if __name__=='__main__':main()
