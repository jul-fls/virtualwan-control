#!/usr/bin/env python3
"""Create a public-source archive from an allowlist; never include operational data."""
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT=Path(__file__).parents[1]
ALLOWED_FILES={'README.md','LICENSE','NOTICE','SECURITY.md','CONTRIBUTING.md','Dockerfile','requirements.txt','requirements.lock','compose.yaml','compose.https.yaml','Caddyfile','.env.example','.gitignore','.dockerignore','pyproject.toml'}
ALLOWED_DIRS={'virtualwan','examples','docs','tools','tests','.github'}

def files():
    for p in sorted(ROOT.rglob('*')):
        if not p.is_file():continue
        relative=p.relative_to(ROOT)
        if '__pycache__' in relative.parts or p.suffix in ('.pyc','.key','.pem','.private'):continue
        if len(relative.parts)==1 and p.name in ALLOWED_FILES:yield p
        elif len(relative.parts)>1 and relative.parts[0] in ALLOWED_DIRS:yield p

def main():
    selected=list(files());errors=[]
    for p in selected:
        data=p.read_text(encoding='utf-8',errors='replace')
        if re.search(r'-----BEGIN (?:OPENSSH |RSA |EC |ENCRYPTED )?PRIVATE KEY-----',data):errors.append(str(p.relative_to(ROOT)))
    if errors:raise ValueError('Private key content detected: '+', '.join(errors))
    out=ROOT/'dist';out.mkdir(exist_ok=True);target=out/'virtualwan-control-source.zip'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        for p in selected:archive.write(p,'virtualwan-control/'+p.relative_to(ROOT).as_posix())
    manifest={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in selected}
    (out/'source-manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'archive':str(target),'files':len(manifest),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'excluded':'data, environment, keys, imports, operational parent'}))

if __name__=='__main__':main()
