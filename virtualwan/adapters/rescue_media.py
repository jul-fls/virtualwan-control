"""Pinned-SSH helper: plaintext password is sent on stdin only, never in argv/logs."""
SOURCE = r'''
import ipaddress,json,os,re,subprocess,sys,tempfile
from pathlib import Path
p=json.load(sys.stdin)
def run(*args,**kw):return subprocess.run(args,check=True,capture_output=True,text=True,**kw).stdout.strip()
vmid=str(int(p['vmid']));session=p['session']
assert re.fullmatch(r'[a-f0-9]{32}',session)
storage=p['storage'];assert re.fullmatch(r'[A-Za-z0-9_-]+',storage)
volid=storage+':iso/virtualwan-rescue-auth-'+vmid+'-'+session+'.iso'
path=Path(run('pvesm','path',volid));assert not path.is_symlink()
if p['action']=='delete':
 if path.exists():
  data=json.loads(run('isoinfo','-R','-i',str(path),'-x','/credential.json'))
  assert data['session']==session and str(data['vmid'])==vmid
  path.unlink()
 print(json.dumps({'deleted':True}));sys.exit(0)
iso=Path(run('pvesm','path',p['iso']))
try:manifest=json.loads(run('isoinfo','-R','-i',str(iso),'-x','/virtualwan-rescue.json'))
except Exception:print(json.dumps({'error':'unsupported_iso'}));sys.exit(0)
if manifest.get('credential_protocol')!=1:print(json.dumps({'error':'unsupported_iso'}));sys.exit(0)
assert not path.exists()
assert re.fullmatch(r'[A-Za-z0-9]{12}',p['password'])
assert re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',p['mac'])
ip=str(ipaddress.IPv4Address(p['ip']))
hashed=run('openssl','passwd','-6','-stdin',input=p['password']+'\n')
assert re.fullmatch(r'\$6\$[A-Za-z0-9./]{1,16}\$[A-Za-z0-9./]{86}',hashed)
with tempfile.TemporaryDirectory(prefix='virtualwan-rescue-',dir='/run') as directory:
 seed=Path(directory);(seed/'credential.json').write_text(json.dumps({'version':1,'vmid':int(vmid),'session':session,'mac':p['mac'],'ipv4':ip,'password_hash':hashed}))
 temporary=path.with_name('.'+path.name+'.partial')
 try:
  run('genisoimage','-quiet','-V','VWAN_RESCUE_AUTH','-J','-r','-o',str(temporary),str(seed))
  os.chmod(temporary,0o600);os.replace(temporary,path)
 finally:temporary.unlink(missing_ok=True)
print(json.dumps({'volid':volid,'session':session}))
'''

def media(pve,action,*,iso=None,password=None,saved=None):
    import json,secrets,shlex
    from .. import domain
    saved=saved or {};session=saved.get('credential_session') or secrets.token_hex(16)
    storage=(saved.get('credential_volid') or iso).split(':',1)[0]
    payload={'action':action,'storage':storage,'session':session,'vmid':pve.client['vmid']}
    if action=='create':payload.update(iso=iso,password=password,mac=pve.client['mac'].lower(),ip=pve.client['ip'])
    stdin,out,err=pve.ssh.exec_command(shlex.join(pve.prefix+['python3','-c',SOURCE]),timeout=60)
    stdin.write(json.dumps(payload));stdin.channel.shutdown_write()
    raw=out.read(65536).decode();errors=err.read(65536).decode()
    if out.channel.recv_exit_status():
        pve.store.event(pve.client.get('_operation_run',pve.client['id']),'client.rescue.media.failed',action=action,stderr=errors)
        raise domain.Conflict('La préparation du média rescue a échoué. Consulter le journal de cette opération.')
    result=json.loads(raw)
    if result.get('error')=='unsupported_iso':raise domain.Conflict('L’ISO rescue doit être mis à jour vers la version avec identifiants uniques avant activation.')
    return result
