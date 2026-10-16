"""Read actual registered VM metadata; no provisioning/network mutation."""
import json
from pathlib import Path
import sys
from .provision import Journal,Remote

def main():
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'));journal=Journal(config)
    # Keep protocol JSON on stdout; SDK progress goes to stderr.
    from contextlib import redirect_stdout
    with redirect_stdout(sys.stderr):
        remote=Remote(config['pve'],journal)
        try:result=remote.call('pve_inventory',pve=config['pve'])
        finally:remote.close()
    print(json.dumps(result))

if __name__=='__main__':main()
