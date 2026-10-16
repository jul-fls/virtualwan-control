"""Explicit SSH onboarding of a fresh OVH Debian router, isolated from the worker."""
import base64
import hashlib
import json
from pathlib import Path
import sys
from .provision import Journal,Remote


def main():
    config=Path(sys.argv[1]);cfg=json.loads(config.read_text(encoding='utf-8-sig'))
    journal=Journal(cfg)
    source=(Path(__file__).parents[2]/'tools/virtualwan-router.py').read_bytes()
    # In the container tools lives next to virtualwan, not inside the package.
    remote=Remote(cfg['vps'],journal)
    try:
        result=remote.call('router_bootstrap',router=cfg['router_setup'],source_base64=base64.b64encode(source).decode(),source_sha256=hashlib.sha256(source).hexdigest())
    finally:remote.close()
    # A second SSH connection must succeed before cancelling the timed rollback.
    remote=Remote(cfg['vps'],journal)
    try:result.update(remote.call('router_bootstrap_finalize',primary_ip=cfg['vps']['primary_ip']))
    finally:remote.close()
    result['journal']=str(journal.directory)
    config.with_suffix('.result.json').write_text(json.dumps(result))
    journal.event('router.setup.complete',**result)


if __name__=='__main__':main()
