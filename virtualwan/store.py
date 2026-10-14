from contextlib import contextmanager
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from cryptography.fernet import Fernet


def ident():return uuid.uuid4().hex


class Store:
    def __init__(self,directory):
        self.root=Path(directory).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.local=threading.local();self.path=self.root/'control.sqlite3'
        key=self.root/'master.key'
        if not key.exists():
            fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(Fernet.generate_key())
        self.cipher=Fernet(key.read_bytes());self.redactions=set()
        with self.tx() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,data TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ip_pool(ip TEXT PRIMARY KEY,profile_id TEXT NOT NULL,state TEXT NOT NULL,job_id TEXT,seen_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY,ip TEXT NOT NULL UNIQUE,profile_id TEXT NOT NULL,model_id TEXT,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,result TEXT NOT NULL DEFAULT '{}',created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS purchases(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS secrets(name TEXT PRIMARY KEY,ciphertext BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL,user TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,time REAL NOT NULL,action TEXT NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')
            db.execute("UPDATE jobs SET state='interrupted',updated_at=? WHERE state='running'",(time.time(),))
            # Never replay a possibly accepted supplier checkout after a crash.
            db.execute("UPDATE purchases SET state='uncertain' WHERE state='checking_out'")

    @contextmanager
    def tx(self):
        if getattr(self.local,'db',None) is not None:
            yield self.local.db
            return
        with self.lock:
            db=sqlite3.connect(self.path,timeout=20);db.row_factory=sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA journal_mode=WAL');db.execute('BEGIN IMMEDIATE')
            self.local.db=db
            try:yield db;db.commit()
            except BaseException:db.rollback();raise
            finally:self.local.db=None;db.close()

    def rows(self,sql,args=()):
        with self.tx() as db:return [dict(x) for x in db.execute(sql,args).fetchall()]

    def one(self,sql,args=()):
        rows=self.rows(sql,args)
        if not rows:raise KeyError('Objet introuvable.')
        return rows[0]

    def document(self,table,key):
        if table not in ('profiles','models','purchases'):raise ValueError('Invalid document table')
        row=self.one(f'SELECT * FROM {table} WHERE id=?',(key,))
        return {**json.loads(row.pop('data')),**row}

    def secret_set(self,name,value):
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{2,79}',name) or not isinstance(value,str) or not 1<=len(value)<=16384:raise ValueError('Secret invalide.')
        with self.tx() as db:db.execute('INSERT OR REPLACE INTO secrets VALUES(?,?)',(name,self.cipher.encrypt(value.encode())))
        self.redactions.add(value)

    def secret_get(self,name):
        if not name:return None
        rows=self.rows('SELECT ciphertext FROM secrets WHERE name=?',(name,))
        value=self.cipher.decrypt(rows[0]['ciphertext']).decode() if rows else os.environ.get(name)
        if value:self.redactions.add(value)
        return value

    def clean(self,value):
        if isinstance(value,dict):return {k:('[REDACTED]' if re.search(r'password|passwd|secret|token|csrf|cookie|authorization|hashed_passwd',k,re.I) and not k.endswith('_env') else self.clean(v)) for k,v in value.items()}
        if isinstance(value,list):return [self.clean(x) for x in value]
        if isinstance(value,str):
            for secret in set(self.redactions):
                if secret:value=value.replace(secret,'[REDACTED]')
        return value

    def event(self,run_id,action,**data):
        data=self.clean(data);now=time.time()
        with self.tx() as db:
            db.execute('INSERT INTO events(run_id,time,action,data) VALUES(?,?,?,?)',(run_id,now,action,json.dumps(data)))
            directory=self.root/'runs'/run_id;directory.mkdir(parents=True,exist_ok=True)
            with open(directory/'events.jsonl','a',encoding='utf-8') as stream:
                stream.write(json.dumps({'time':datetime.fromtimestamp(now,timezone.utc).isoformat(),'run_id':run_id,'action':action,**data})+'\n');stream.flush();os.fsync(stream.fileno())

    def job(self,kind,payload,db=None):
        job=ident();now=time.time()
        def write(connection):connection.execute('INSERT INTO jobs(id,kind,state,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)',(job,kind,'queued',json.dumps(payload),now,now))
        if db is not None:write(db)
        else:
            with self.tx() as connection:write(connection)
        return job
