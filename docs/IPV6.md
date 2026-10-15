# Routed public IPv6

IPv6 is optional per router profile. Existing IPv4-only profiles require no new
fields. The base Debian cloud-init template stays generic: public addresses are
supplied in each VM's network snippet, never baked into a shared image.

## Prerequisites

Route an unused public /64 from your upstream router to pfSense. Configure an
address from that /64 on the dedicated client interface, with no upstream gateway
on that interface. The pfSense IPv6 default route must already use the intended
uplink. Disable router advertisements and DHCPv6 on the client VLAN. Use the real
link-local address of pfSense's CLIENT interface as the VM gateway, not its WAN
link-local address. The IPv4 WireGuard route is unchanged.

The controller does not reconfigure a Freebox, select physical interfaces, purchase
an IPv6 service, create an upstream default route or add NAT66. An OVH vRack block
must reach pfSense through a compatible router first. A prefix from a residential
ISP remains tied to that uplink and will not automatically fail over to another ISP.

## Environment configuration

These optional overrides use the same single-profile selection as the connection
variables. Set `VWAN_PROFILE_ID` when more than one profile exists. Each value is
one line. Documentation addresses below MUST be replaced with a real routed prefix
before enabling; the application rejects non-public documentation addresses.

```dotenv
VWAN_IPV6_ENABLED=true
VWAN_IPV6_NETWORK=2001:db8:1234:1::/64
VWAN_IPV6_GATEWAY_ADDRESS=2001:db8:1234:1::1
VWAN_IPV6_GATEWAY_LINK_LOCAL=fe80::1
VWAN_IPV6_PROTECTED_NETWORKS=2001:db8:1234::/60
VWAN_IPV6_WAN_INTERFACE=wan
VWAN_IPV6_PUBLIC_ALIAS=VWAN_CLIENTS_V6
VWAN_IPV6_PROTECTED_ALIAS=VWAN_PROTECTED_V6
```

`protected_networks` must include ALL personal/infrastructure public IPv6 prefixes,
not just private IPv6 space. The client /64, ULA, link-local, multicast and unspecified/
loopback destinations are also protected automatically. Explicitly registered clients
in the same segment may talk via pfSense using their public IPv6; direct Ethernet
communication remains blocked. Different profiles need separate /64s and alias
names. Cross-profile traffic inside a protected aggregate needs a separately reviewed
policy; it is not automatically exempted by this per-segment implementation.

Equivalent profile configuration under `config.ipv6`:

```json
{"enabled":true,"network":"2001:db8:1234:1::/64","gateway_address":"2001:db8:1234:1::1","gateway_link_local":"fe80::1","protected_networks":["2001:db8:1234::/60"],"wan_interface":"wan","public_alias":"VWAN_CLIENTS_V6","protected_alias":"VWAN_PROTECTED_V6"}
```

Do not put a per-client `address` in a profile. New allocations are derived from
VMID: VM120 gets `prefix::120`, VM121 gets `prefix::121`. The suffix uses the textual
VMID as hexadecimal digits; it is a stable naming convention, not a DHCP lease.
The cluster registry checks collisions and preserves existing registered addresses.
A profile cannot change prefix, gateway or aliases while its deployed IPv6 clients
remain attached. Cloning a router profile deliberately disables IPv6 until another
unused /64 is configured.

## Provisioning and retries

1. Inspect pfSense topology, existing DNS and authoritative PVE registry. Reject
   conflicting prefixes, foreign aliases/rules or duplicate addresses.
2. Save the allocated address in the run config. Keep it on retry.
3. Back up and upgrade the owned isolation generator/service on EVERY node in the
   registered segment, using existing pinned cluster SSH keys. Preserve all existing
   clients/segments and IPv4 rules. Compilation/application failure stops deployment;
   no client VM is started with a partially installed IPv6 policy.
4. Clone the VM stopped. Its cloud-init snippet has public IPv4 /32 and IPv6 /128,
   IPv4 on-link gateway and IPv6 link-local gateway. No private VM address is added.
   Debian static IPv6 ifupdown disables RA/autoconf; a per-interface sysctl file also
   disables RA, autoconf and temporary addresses persistently.
5. Register the MAC, IPv4, IPv6 and EUI-64 link-local identity. Configure scoped
   pfSense aliases/rules through HTTPS native forms. IPv6 follows the existing IPv6
   default route; IPv4 retains WireGuard policy routing.
6. Start and verify the exact global addresses, routes and public egress, including
   the optional reboot verification. Publish DNS-only AAAA only after successful
   guest verification. IPv4 A and OVH IPv4 reverse behavior are unchanged.

Logs include allocations, node updates, encrypted snapshots, pfSense changes,
verification and AAAA operations in the normal run journal. Partial failures retain
resources for inspection/retry; addresses are not silently made available again.
The app does not manage IPv6 reverse DNS, since this depends on the prefix provider.

The cloud-init path is validated for the existing Debian template with the ENI
renderer and EUI-64 link-local addressing. Other OS/renderers require validation
before customer use. SSH reachability checks originate from the controller, not an
independent external probe. IPv6 HTTP/ICMP external validation was performed on the
pilot; the new full lifecycle is covered by local adapter tests, not destructive
creation/deletion of customer VMs.

## Release and deletion

All three existing operations clean the VM's IPv6 allocation and managed AAAA:

| Operation | VM/disks | IPv4 | IPv6 |
|---|---|---|---|
| Release network | Keep stopped, detach public NIC | Return to stock | Remove AAAA, firewall membership and registry identity |
| Delete VM only | Delete | Keep reserved | Remove AAAA, firewall membership and registry identity |
| Delete and release | Delete | Return to stock | Remove AAAA, firewall membership and registry identity |

Ownership checks precede VM stop. Foreign AAAA aliases or modified pfSense objects
block the operation for review. Only the exact client AAAA is removed; another
client's address and rules remain. When the last IPv6 client leaves, the five shared
IPv6 rules and empty client alias are removed; the static protected-networks alias
is kept for future provisioning. Only states matching the departing IPv6 are killed.
There is no global firewall/state flush. A failure leaves the IP quarantined until
retry completes. The detached VM's old addresses remain historical metadata; a
future attachment requires fresh network configuration, as with the existing IPv4
workflow. No old address is reactivated automatically.

## Existing manual pilot / upgrade

Configure the profile with the same prefix, gateways and protected networks as the
pilot, retaining its existing alias names. Select **Read VMs / Lire les VM** after
upgrading: registered IPv6, link-local and policy metadata are imported without
changing guest networks. Existing matching `Virtualwan IPv6 ...` rules/aliases are
recognized and reused. Pilot AAAA records marked `Virtualwan IPv6 pilot VM...` are
recognized alongside `Virtualwan managed`. A foreign DNS record is never deleted
merely because it has the same IP.

No shared template edit is needed, and enabling the profile does NOT automatically
add IPv6 to existing IPv4-only VMs. New VMs use dual-stack cloud-init. Existing manual
VMs keep their current address/configuration; allocation and cleanup use the actual
registry, rather than guessing the VMID-derived address.

## Validation commands

```sh
python -m unittest discover -s tests -q
# With cloud-init + ifupdown installed, no network activation:
python tests/cloudinit_ipv6_render.py
```

Tests cover IPv4 compatibility, IPv6 allocation/collisions, manual address import,
public-only snippets, DNS foreign records, environment setup, three lifecycle modes,
last-client cleanup, retained clients, pfSense ownership and idempotent retries.
