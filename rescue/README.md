# Shared Virtualwan Rescue ISO

Debian 13 amd64 live, BusyBox plus filesystem/partition/RAID/LVM/LUKS recovery,
network diagnostics, rsync, ddrescue and TestDisk. No desktop, no installer and
no automatic mounting or repair of customer disks. BIOS and UEFI bootloaders,
automatic boot after three seconds. Secure Boot is not certified by this build.

The read-only NoCloud CD already attached to each Virtualwan VM supplies its
public IPv4 /32, point-to-point gateway, MTU, DNS and optional public IPv6 /128
with link-local gateway. NIC selection uses the original MAC rather than the
normal guest's interface name. This keeps the same PVE isolation/firewall rules.
The ISO has no customer IPs, credentials, host keys or private keys embedded.

Only declarative network data and plain authorized public SSH keys are read.
NoCloud user-data commands/passwords/users are **not executed**. With no valid
seed, networking fails closed; the local console remains available. DHCP/RA
are not used as a fallback, and there is no extra private guest IPv4.

The live console uses French AZERTY. Select Français in the console keyboard
selector if the normal OS uses a saved QWERTY preference.

Console login and SSH use `rescue`, with a new 12-character password for each
activation (uppercase/lowercase/digits, no punctuation or ambiguous characters).
The owner can reveal/copy it in the portal while rescue is active. It is encrypted
in the application database, omitted from inventory and audit logs, and deleted
when returning to the system disk. A new activation always rotates it. `sudo -i`
opens a root shell. Existing NoCloud SSH public keys remain valid. Without a
valid activation credential CD, the password is locked and SSH is key-only;
console autologin and root SSH are disabled. SSH host keys are ephemeral and
the fingerprint is printed in the console; expect it to differ from the normal
OS. Verify it and use a separate known-hosts file for a rescue SSH session.

## Build

On an amd64 Docker Linux engine, from the repository root:

```sh
docker build -t virtualwan-rescue-builder -f rescue/Dockerfile .
mkdir -p dist/rescue
docker run --name virtualwan-rescue-build --privileged \
  --mount type=bind,src="$PWD/rescue",dst=/source,readonly \
  --mount type=bind,src="$PWD/dist/rescue",dst=/output \
  virtualwan-rescue-builder
```

Build privileges are for live-build's chroot/mount operations on the local
builder, not for the management application or a PVE node. A normal Debian
build host can run `rescue/build.sh` as root with the same build packages.
Validate the resulting image with `test-boot.py` in a builder container with
`/source` and `/output` mounted and access to `/dev/kvm` (or the slower TCG
fallback). It boots disposable BIOS/UEFI guests, tests IPv4/IPv6 gateways and
actual SSH-key login, then tests missing-media behavior. No production guest
is involved.

All runtime packages come from authenticated Debian repositories. Save the
package manifest and SHA-256 alongside each published ISO. Rebuild to pick up
security updates; do not silently replace an ISO while guests are using it.

Upload the ISO atomically into the actual directory for PVE storage `iso`, then
verify its SHA-256 and visibility from all eligible nodes. The image is shared;
no per-customer rebuild is needed. Configure the controller:

```dotenv
VWAN_RESCUE_ISO=iso:iso/virtualwan-rescue-debian13-amd64-v2.iso
```

Recreate the management container after changing its environment. In the portal,
stop the VM, enable rescue, start it, repair from console/SSH, stop it, return
to the normal disk, then start it. The cloud-init CD must remain attached. The
existing portal stores/restores boot order; the rescue image does not change
normal guest networking, its SSH keys or disk contents automatically.

Useful commands: `lsblk -f`, `ip -br addr`, `ip route`, `ip -6 route`,
`cat /run/virtualwan-rescue-network.json`,
`journalctl -u virtualwan-rescue`, `sudo mount /dev/vda1 /mnt`,
`sudo cryptsetup open ...`, `sudo vgchange -ay`, `sudo mdadm --assemble --scan`.
Identify the correct disk/filesystem before manual repair or mounting.

Design follows [Debian Live](https://live-team.pages.debian.net/live-manual/html/live-manual/the-basics.en.html)
and the VM's [NoCloud media](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html).

## Per-activation credentials

The v2 ISO contains `/virtualwan-rescue.json` (`credential_protocol: 1`).
The portal refuses older images for password-enabled activation. `isoinfo`,
`genisoimage` and `openssl` must be installed on the PVE connection node.
While the VM is stopped, the common live ISO goes on `ide0`, a small individual
`VWAN_RESCUE_AUTH` ISO on `ide1`, and the usual NoCloud CD stays on `ide2`.
Existing `ide0`/`ide1` devices are never overwritten. The individual CD contains
only a salted SHA-512-crypt hash bound to the activation and NIC MAC; it never
contains the plaintext password. The live checks this MAC before unlocking the
account. Password transport is pinned SSH stdin, never process arguments.
Returning to normal detaches both rescue CDs, restores boot order, deletes the
individual ISO and encrypted credential. Interrupted cleanup can be retried.
Keep the shared application master key with its data volume across restarts;
losing it also prevents decryption of active rescue credentials.

The UI displays a yellow **Rescue** badge on the server card/detail and admin
inventory while rescue boot is configured. This describes selected boot mode,
not a guest-agent assertion that the live is currently running. Legacy active
sessions without stored credentials must exit/re-enter rescue to generate them.
