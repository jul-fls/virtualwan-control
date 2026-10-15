"""Client identities. Passwords are hashed; TOTP secrets are encrypted in the volume."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
from urllib.parse import quote
from werkzeug.security import check_password_hash,generate_password_hash
from . import domain
from .store import ident


def email(value):
    value=str(value).strip().lower()
    if len(value)>254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',value):raise ValueError('Adresse e-mail invalide.')
    return value


def password_hash(value):
    if not isinstance(value,str) or not 12<=len(value)<=1024:raise ValueError('Mot de passe : 12 à 1024 caractères.')
    return generate_password_hash(value)


def totp(secret,step):
    raw=base64.b32decode(secret+'='*((-len(secret))%8))
    value=hmac.new(raw,struct.pack('>Q',step),hashlib.sha1).digest();offset=value[-1]&15
    return str((struct.unpack('>I',value[offset:offset+4])[0]&0x7fffffff)%1000000).zfill(6)


class Accounts:
    def __init__(self,store,origin):
        self.store=store;self.origin=origin
        self.dummy=generate_password_hash(secrets.token_urlsafe(24))
    def list(self):
        result=[]
        for row in self.store.rows('SELECT id,email,name,enabled,created_at,totp FROM accounts ORDER BY created_at DESC'):
            row['mfa_enabled']=bool(row.pop('totp'));row['enabled']=bool(row['enabled']);result.append(row)
        return result
    def save(self,body,key=None):
        key=key or ident();address=email(body['email']);name=domain.label(body['name'])
        with self.store.tx() as db:
            old=db.execute('SELECT * FROM accounts WHERE id=?',(key,)).fetchone()
            if db.execute('SELECT id FROM accounts WHERE email=? AND id!=?',(address,key)).fetchone():raise domain.Conflict('Cet e-mail est déjà utilisé.')
            enabled=body.get('enabled',True)
            if not isinstance(enabled,bool):raise ValueError('État du compte invalide.')
            hashed=password_hash(body['password']) if body.get('password') else old['password_hash'] if old else password_hash('')
            if old:
                db.execute('UPDATE accounts SET email=?,name=?,enabled=?,password_hash=? WHERE id=?',(address,name,int(enabled),hashed,key))
                if body.get('password') or not enabled or old['email']!=address:db.execute('DELETE FROM client_sessions WHERE account_id=?',(key,))
            else:db.execute('INSERT INTO accounts(id,email,name,password_hash,enabled,created_at) VALUES(?,?,?,?,?,?)',(key,address,name,hashed,int(enabled),time.time()))
        self.store.event(ident(),'account.saved',account_id=key,email=address,enabled=enabled)
        return key
    def reset_mfa(self,key):
        with self.store.tx() as db:
            self.store.one('SELECT id FROM accounts WHERE id=?',(key,))
            db.execute("UPDATE accounts SET totp=NULL,last_totp=-1,recovery='[]' WHERE id=?",(key,))
            db.execute('DELETE FROM passkeys WHERE account_id=?',(key,));db.execute('DELETE FROM client_sessions WHERE account_id=?',(key,));db.execute('DELETE FROM client_challenges WHERE account_id=?',(key,))
        self.store.event(ident(),'account.authentication.reset',account_id=key)
    def second_factor(self,row,code):
        if not row['totp']:return True
        code=str(code or '').strip()
        if not re.fullmatch(r'[0-9]{6}|[a-fA-F0-9-]{20,30}',code):return False
        step=int(time.time()//30)
        secret=self.store.cipher.decrypt(row['totp']).decode()
        for tick in (step-1,step,step+1):
            if tick>row['last_totp'] and hmac.compare_digest(totp(secret,tick),code):
                with self.store.tx() as db:
                    changed=db.execute('UPDATE accounts SET last_totp=? WHERE id=? AND last_totp<?',(tick,row['id'],tick)).rowcount
                return bool(changed)
        hashed=hashlib.sha256(code.replace('-','').encode()).hexdigest()
        with self.store.tx() as db:
            current=db.execute('SELECT recovery FROM accounts WHERE id=?',(row['id'],)).fetchone()
            codes=json.loads(current['recovery'])
            if hashed in codes:
                codes.remove(hashed);db.execute('UPDATE accounts SET recovery=? WHERE id=?',(json.dumps(codes),row['id']));return True
        return False
    def login(self,address,password,code):
        try:address=email(address)
        except ValueError:address=''
        rows=self.store.rows('SELECT * FROM accounts WHERE email=?',(address,));row=rows[0] if rows else None
        valid=check_password_hash(row['password_hash'] if row else self.dummy,str(password)[:1025])
        if not row or not valid or not row['enabled'] or not self.second_factor(row,code):return None
        return self.session(row['id'],'password')
    def session(self,key,method):
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);now=time.time()
        with self.store.tx() as db:
            if not db.execute('SELECT 1 FROM accounts WHERE id=? AND enabled=1',(key,)).fetchone():raise domain.Conflict('Compte indisponible.')
            db.execute('DELETE FROM client_sessions WHERE expires<?',(now,))
            db.execute('INSERT INTO client_sessions VALUES(?,?,?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,key,now+28800,now,method))
        self.store.event(ident(),'client.login',account_id=key,method=method)
        return {'token':token,'csrf':csrf}
    def authenticate(self,token):
        rows=self.store.rows('SELECT s.*,a.email,a.name FROM client_sessions s JOIN accounts a ON a.id=s.account_id WHERE s.token=? AND s.expires>? AND a.enabled=1',(hashlib.sha256(token.encode()).hexdigest(),time.time()))
        return rows[0] if rows else None
    def reauth(self,session,body):
        if session['method']=='passkey' and session['authenticated_at']>time.time()-300:return
        row=self.store.one('SELECT * FROM accounts WHERE id=?',(session['account_id'],))
        if not check_password_hash(row['password_hash'],str(body.get('current_password',''))) or not self.second_factor(row,body.get('code')):raise domain.Conflict('Mot de passe actuel et code MFA valide requis.')
    def challenge(self,key,purpose,data,ttl=300):
        cid=secrets.token_urlsafe(32)
        with self.store.tx() as db:
            db.execute('DELETE FROM client_challenges WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO client_challenges VALUES(?,?,?,?,?)',(cid,key,purpose,json.dumps(data),time.time()+ttl))
        return cid
    def consume(self,cid,purpose,key=None):
        with self.store.tx() as db:
            row=db.execute('SELECT * FROM client_challenges WHERE id=? AND purpose=? AND expires>?',(cid,purpose,time.time())).fetchone()
            if not row or (key is not None and row['account_id']!=key):raise domain.Conflict('Demande expirée ou déjà utilisée.')
            db.execute('DELETE FROM client_challenges WHERE id=?',(cid,))
        return row['account_id'],json.loads(row['data'])
    def begin_totp(self,session,body):
        self.reauth(session,body)
        row=self.store.one('SELECT totp FROM accounts WHERE id=?',(session['account_id'],))
        if row['totp']:raise domain.Conflict('MFA déjà activée.')
        secret=base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
        challenge=self.challenge(session['account_id'],'totp',{'encrypted':self.store.cipher.encrypt(secret.encode()).decode(),'session':session['token']})
        uri='otpauth://totp/'+quote('Virtualwan:'+session['email'],safe='')+'?secret='+secret+'&issuer=Virtualwan&digits=6&period=30'
        import qrcode
        import qrcode.image.svg
        svg=qrcode.make(uri,image_factory=qrcode.image.svg.SvgPathImage).to_string()
        return {'challenge':challenge,'secret':secret,'uri':uri,'qr':'data:image/svg+xml;base64,'+base64.b64encode(svg).decode()}
    def complete_totp(self,session,body):
        key,data=self.consume(body['challenge'],'totp',session['account_id'])
        if data['session']!=session['token']:raise domain.Conflict('Session différente.')
        step=int(time.time()//30);secret=self.store.cipher.decrypt(data['encrypted'].encode()).decode()
        if not any(hmac.compare_digest(totp(secret,n),str(body.get('code',''))) for n in (step-1,step,step+1)):raise ValueError('Code MFA invalide ; recommencer l’activation.')
        codes=[secrets.token_hex(10) for _ in range(10)]
        with self.store.tx() as db:
            db.execute('UPDATE accounts SET totp=?,last_totp=?,recovery=? WHERE id=? AND totp IS NULL',(data['encrypted'].encode(),step,json.dumps([hashlib.sha256(c.encode()).hexdigest() for c in codes]),key))
            db.execute('DELETE FROM client_sessions WHERE account_id=? AND token!=?',(key,session['token']))
        self.store.event(ident(),'client.mfa.enabled',account_id=key)
        return {'recovery_codes':codes}
    def fido(self):
        from urllib.parse import urlparse
        from fido2.server import Fido2Server
        if not self.origin.startswith('https://') and urlparse(self.origin).hostname!='localhost':raise domain.Conflict('Les passkeys exigent une URL publique HTTPS (ou localhost pour les tests).')
        return Fido2Server({'id':urlparse(self.origin).hostname,'name':'Virtualwan'},verify_origin=lambda origin:origin==self.origin)
    def credentials(self,key):
        from fido2.webauthn import AttestedCredentialData
        from fido2.utils import websafe_decode
        return [AttestedCredentialData(websafe_decode(r['credential'])) for r in self.store.rows('SELECT credential FROM passkeys WHERE account_id=?',(key,))]
    def passkey_begin(self,key,session=None):
        server=self.fido();credentials=self.credentials(key)
        if session:
            row=self.store.one('SELECT email,name FROM accounts WHERE id=?',(key,))
            options,state=server.register_begin({'id':key.encode(),'name':row['email'],'displayName':row['name']},credentials,user_verification='required')
            state['session']=session['token'];purpose='passkey_register'
        else:options,state=server.authenticate_begin(credentials,user_verification='required');purpose='passkey_login'
        return {'challenge':self.challenge(key,purpose,state),'options':dict(options)}
    def passkey_complete(self,body,session=None):
        from fido2.utils import websafe_encode
        key,state=self.consume(body['challenge'],'passkey_register' if session else 'passkey_login',session['account_id'] if session else None)
        server=self.fido()
        if session:
            if state.pop('session')!=session['token']:raise domain.Conflict('Session différente.')
            auth=server.register_complete(state,body['credential']);credential=auth.credential_data
            with self.store.tx() as db:
                db.execute('INSERT INTO passkeys VALUES(?,?,?,?,?,?)',(websafe_encode(credential.credential_id),key,domain.label(body.get('name','Passkey')),websafe_encode(bytes(credential)),auth.counter,time.time()))
            self.store.event(ident(),'client.passkey.added',account_id=key);return {'ok':True}
        credential=server.authenticate_complete(state,self.credentials(key),body['credential'])
        from fido2.webauthn import AuthenticationResponse
        response=AuthenticationResponse.from_dict(body['credential']);count=response.response.authenticator_data.counter
        with self.store.tx() as db:
            row=db.execute('SELECT sign_count FROM passkeys WHERE id=? AND account_id=?',(websafe_encode(credential.credential_id),key)).fetchone()
            if not row or ((row['sign_count'] or count) and count<=row['sign_count']):raise domain.Conflict('Compteur de passkey invalide.')
            db.execute('UPDATE passkeys SET sign_count=? WHERE id=?',(count,websafe_encode(credential.credential_id)))
        return self.session(key,'passkey')
