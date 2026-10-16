#!/usr/bin/env python3
"""Build a new stopped cloud-init template from an operator-verified local image.

Run on the target PVE node as root. Existing VMs/templates are never modified.
Linux preparation needs virt-customize (libguestfs-tools); FreeBSD must already
contain configured Python cloud-init, QGA, curl, sudo and Python 3.
"""
import argparse,hashlib,json,os,re,shutil,subprocess,tempfile
from pathlib import Path

FAMILIES=('debian','ubuntu','fedora','almalinux','rocky','freebsd')
def run(*args):return subprocess.check_output(args,text=True)
def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--family',choices=FAMILIES,required=True);p.add_argument('--image',type=Path,required=True);p.add_argument('--sha256',required=True)
 p.add_argument('--vmid',type=int,required=True);p.add_argument('--storage',required=True);p.add_argument('--bridge',required=True);p.add_argument('--prepared-freebsd',action='store_true');p.add_argument('--prepared-rpm',action='store_true',help='Import a Linux RPM image prepared and cleaned in a running guest (preserves SELinux labels).')
 args=p.parse_args()
 if os.geteuid()!=0:raise SystemExit('Run on PVE as root.')
 if not 100<=args.vmid<=999999999 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}',args.storage) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}',args.bridge):raise SystemExit('Invalid VMID/storage/bridge.')
 if not re.fullmatch(r'[a-fA-F0-9]{64}',args.sha256):raise SystemExit('A vendor-verified SHA256 digest is required.')
 if not args.image.is_file() or args.image.is_symlink():raise SystemExit('Use a local regular image file.')
 with args.image.open('rb') as f:
  if hashlib.file_digest(f,'sha256').hexdigest()!=args.sha256.lower():raise SystemExit('Image checksum mismatch.')
 resources=json.loads(run('pvesh','get','/cluster/resources','--type','vm','--output-format','json'))
 if any(v['vmid']==args.vmid for v in resources):raise SystemExit('VMID already exists; refusing to modify it.')
 info=json.loads(run('qemu-img','info','--output=json',str(args.image)))
 if info.get('format') not in ('raw','qcow2') or info.get('backing-filename') or info.get('encrypted'):raise SystemExit('Use a standalone unencrypted raw/qcow2 image.')
 if args.family=='freebsd' and not args.prepared_freebsd:raise SystemExit('FreeBSD needs a prepared Python cloud-init image; nuageinit-only images are not interchangeable.')
 if args.family in ('fedora','almalinux','rocky') and not args.prepared_rpm:raise SystemExit('RPM images must be prepared in a running guest to preserve SELinux policy/labels; see docs/SYSTEMES-ET-ISO.md, then use --prepared-rpm.')
 if args.prepared_rpm and args.family not in ('fedora','almalinux','rocky'):raise SystemExit('--prepared-rpm is only valid for Fedora/AlmaLinux/Rocky.')
 if args.family!='freebsd' and not args.prepared_rpm and not shutil.which('virt-customize'):raise SystemExit('Install libguestfs-tools on this preparation host first.')
 # Only the temporary image copy is customized. Vendor original stays intact.
 with tempfile.TemporaryDirectory(prefix='virtualwan-template-') as work:
  disk=Path(work)/'prepared.qcow2';run('qemu-img','convert','-O','qcow2',str(args.image),str(disk))
  if args.family!='freebsd' and not args.prepared_rpm:
   rpm=args.family in ('fedora','almalinux','rocky')
   cloud=Path(work)/'99-virtualwan.cfg';cloud.write_text('datasource_list: [ NoCloud ]\n'+('system_info:\n  network:\n    renderers: [ eni ]\n' if args.family=='debian' else ''))
   packages=['cloud-init','qemu-guest-agent','curl','python3','sudo']+(['kbd','policycoreutils-python-utils'] if rpm else ['keyboard-configuration','console-setup'])
   clean='rm -f /etc/ssh/ssh_host_*; cloud-init clean --logs; truncate -s 0 /etc/machine-id; systemctl enable qemu-guest-agent cloud-init-local cloud-init cloud-config cloud-final cloud-init.target'
   # Remove image DHCP fallbacks only inside this new offline image.
   clean+='; rm -f /etc/NetworkManager/system-connections/*.nmconnection /etc/netplan/*cloud-init*.yaml'
   print(run('virt-customize','-a',str(disk),'--install',','.join(packages),'--upload',str(cloud)+':/etc/cloud/cloud.cfg.d/99-virtualwan.cfg','--run-command',clean),flush=True)
  name='virtualwan-'+args.family+'-'+str(args.vmid)
  run('qm','create',str(args.vmid),'--name',name,'--memory','2048','--cores','2','--net0','virtio,bridge='+args.bridge,'--scsihw','virtio-scsi-pci','--agent','enabled=1','--vga','std','--keyboard','fr','--ostype','other' if args.family=='freebsd' else 'l26')
  # On error, leave this new stopped VM for inspection. Never destroy anything
  # automatically, and never start it on the production public VLAN.
  run('qm','importdisk',str(args.vmid),str(disk),args.storage)
  node=run('hostname','-s').strip()
  cfg=json.loads(run('pvesh','get',f'/nodes/{node}/qemu/{args.vmid}/config','--output-format','json'))
  disks=[v.split(',')[0] for k,v in cfg.items() if k.startswith('unused')]
  if len(disks)!=1:raise SystemExit('Imported disk identity ambiguous; inspect the stopped VM.')
  run('qm','set',str(args.vmid),'--scsi0',disks[0],'--scsi1','none,media=cdrom','--ide2',args.storage+':cloudinit','--citype','nocloud','--boot','order=scsi0','--description',f'Virtualwan {args.family} template. Input image SHA256: {args.sha256.lower()}. Validate /32 + /128 networking before publishing.')
  run('qm','template',str(args.vmid))
  run('qm','set',str(args.vmid),'--boot','order=scsi0','--cpu','x86-64-v2-AES')
 print(json.dumps({'template':args.vmid,'family':args.family,'node':run('hostname','-s').strip(),'published':False,'next':'Validate a disposable clone, then register it in Systems & templates.'}))
if __name__=='__main__':main()
