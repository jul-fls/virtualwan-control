"""Documentation addresses only; these objects are visibly simulated."""
import json
from pathlib import Path
import time
from .store import ident

def seed(control):
    example=json.loads((Path(__file__).parent.parent/'examples'/'profile.json').read_text())
    example.update(name='Routeur Europe · démo',ready=True)
    example['config']['ovh']['expected_service']='vps-demo.example.net'
    key=control.add_profile(example)
    models=[]
    for name,group,cpu,ram,disk in [('Starter','Essentiel',1,1024,20),('Standard','Essentiel',2,2048,40),('Performance','Performance',4,8192,100)]:
        models.append(control.add_model({'name':name,'range':group,'cores':cpu,'memory_mb':ram,'disk_gb':disk,'template':9001}))
    with control.store.tx() as db:
        for ip,status in [('192.0.2.20','assigned'),('192.0.2.21','available'),('192.0.2.22','available'),('192.0.2.23','available')]:
            db.execute('INSERT INTO ip_pool VALUES(?,?,?,?,?)',(ip,key,status,None,time.time()))
        info={'fqdn':'vps-1920220.hosting.example.net','status':'simulated','job_id':ident(),'customer':'Client de démonstration','mac':'02:00:00:00:00:20','next_hop':'10.254.251.11','vmid':120,'simulated':True}
        db.execute('INSERT INTO deployments VALUES(?,?,?,?,?)',(ident(),'192.0.2.20',key,models[1],json.dumps(info)))
