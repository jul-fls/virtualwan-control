# Public-only Debian template

Prepare a dedicated Debian 13 template with cloud-init and QEMU Guest Agent.
Use a verified official Debian image or your own clean Debian installation.
Never overwrite an existing client or a general-purpose template in production.

Inside the template:

```sh
apt-get update
apt-get install cloud-init qemu-guest-agent ifupdown resolvconf curl openssh-server
systemctl enable qemu-guest-agent ssh
```

Install `tools/99-virtualwan-renderer.cfg` as
`/etc/cloud/cloud.cfg.d/99-virtualwan-renderer.cfg`. The ENI renderer supports
the `pointopoint` gateway used for the public /32 with no private guest IPv4.
Remove competing persistent interface configuration or DHCP managers **in
this dedicated template**, using its console. Do not globally restart the
network of a remote guest to make this change.

The template must have no personal SSH key, fixed public/private IP, customer
password, WireGuard private key, management credential or stale cloud-init
instance state. Before shutting it down for conversion, clean that state:

```sh
cloud-init clean --logs --machine-id
```

On Proxmox, add its Cloud-init disk on the configured disk storage, enable
the guest agent, choose the matching NIC/bridge and convert the stopped VM
to a template. Match the template ID, disk storage, shared snippets storage
and path in each router profile. A matching storage NAME on another node is
not proof that its disks are shared; migrate local disks explicitly.

Install the shared isolation hook and attach it to the dedicated template.
The provisioning adapter registers the cloned VM identity before starting it.
The template itself is not a customer identity and should not be started
as a public client. The generated network snippet uses the clone's MAC,
public /32, pointopoint gateway, MTU and configured public DNS servers.

For the first disposable clone, test from its console and an outside source:

```sh
cloud-init status --long
ip -4 addr
ip -4 route
cat /etc/resolv.conf
curl -4 https://api.ipify.org
```

Only its public IPv4 should exist on the client NIC, with a default route
via the private peer gateway. Test SSH with the actual supplied key/account,
incoming TCP to any service you intentionally run, reboot, and isolation
from other clients/private infrastructure. The built-in probe verifies the
SSH banner and network state; it does not prove possession of a customer's
private key or install an HTTP server in every clone.

Sources: [Debian cloud images](https://cloud.debian.org/images/cloud/),
[Proxmox Cloud-init](https://pve.proxmox.com/pve-docs/chapter-qm.html#qm_cloud_init),
[cloud-init ENI renderer](https://github.com/canonical/cloud-init/blob/main/cloudinit/net/eni.py).

## Graphical console for the client portal

New clones are configured with `vga: std` and their inherited `serial0` device
removed while stopped. Cloud-init configures the Debian GRUB console for tty0
and enables `getty@tty1`, so noVNC has a local login prompt. No guest password is
created for SSH-key-only customers: they may set their own local console password
through SSH if needed. The console can still access boot/rescue screens.

An already deployed serial-only VM is not silently stopped or reconfigured by
upgrading the controller. From its own console/SSH, ensure `getty@tty1` is enabled
and GRUB supports tty0, then stop the VM, select Prepare graphical console in
the client portal, and start it again. This removes serial0 and selects std VGA.
Test this on a disposable clone before converting a general-purpose template.
