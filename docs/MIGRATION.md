# Migration from a working Virtualwan CLI setup

Leave the current routing and services running. The web app uses the same
public-only model and flat isolation registry for an existing single router.
Starting the app does not upgrade the hosts, modify pfSense or redeploy VMs.

1. Stop the application while importing data. Do not run a second controller
   against the same firewall. Keep purchases/provisioning disabled in .env.
2. Put the old generic provisioning config and isolation registry into the
   private /data volume, not into the repository/image. If importing secret
   values, use a private JSON mapping ENV_NAME to value, or enter them later
   through the encrypted vault UI.
3. Copy verified known_hosts or enroll the host keys in the UI. Private SSH
   keys, if used, need explicit paths under data and mode 0600. The import
   refuses an old private-key path to avoid silently copying private files.
4. Import offline, for example in the stopped project's one-off container:

```sh
docker compose run --rm --no-deps control python tools/import-legacy.py \
  --config /data/old-provision.json --registry /data/old-isolation.json \
  --secrets-json /data/import-secrets.json --known-hosts /data/old-known-hosts
```

This command uses an existing private volume. To populate it first, use your
preferred secure copy/container administration method. The script is also
usable locally with `--data /your/private/directory`. Never overwrite an
existing database/key pair with a different imported pair. The import is
idempotent for already known IPs on the same profile.

5. Remove the temporary plaintext secrets import after verifying the vault.
   Keep copies/backups encrypted outside the public source distribution.
6. Start Compose. Import labels reflect a last-known registry, not a live
   health check. Use Lire les VM and Synchroniser to query the actual systems.
   Missing IPs are quarantined; a MAC mismatch is a conflict, not auto-repaired.
7. Configure models and test an owned, disposable VM/IP before enabling
   customer provisioning. Do not reuse an assigned IP as free stock.

## Multiple routing VPS

The old flat registry remains valid. For additional routers on the same PVE
cluster, the optional new service supports:

```json
{
  "segments": {
    "router_east": {
      "gateway_ip": "10.254.251.1",
      "gateway_mac": "02:00:00:00:00:01",
      "bridge": "vmbr12", "uplink": "vlan150",
      "nodes": {"pve-node": {"bridge": "vmbr12", "uplink": "vlan150", "address": "192.168.1.10"}},
      "blocked_networks": ["10.0.0.0/8", "192.168.0.0/16"],
      "administration_public_networks": [], "clients": []
    }
  }
}
```

This is a minimal shape example, not the complete recommended protected list.
The onboarding bundle includes the complete base list. Add your personal public
infrastructure endpoints and their reply-only administration exceptions.
For each cloned profile, pve.isolation_segment selects a segment. The legacy
profile leaves that field empty and keeps its original table/registry format.
Legacy fields may coexist at the top level with additional segments.

On each relevant node, back up the existing script and registry. Install
public-policy.py at /usr/local/lib/virtualwan-isolation/public-policy.py and
isolation-service.py at /usr/local/sbin/virtualwan-isolation (0755), keeping
the existing systemd service/timer and hook. Review/merge the new segment,
`virtualwan-isolation apply --force`, then test before starting clients.
Do not install a segment with placeholder uplinks/MAC/bridge. Never globally
flush nftables or reload all host networking to introduce a dedicated bridge.

Cross-segment public traffic is routed by pfSense according to reviewed
rules; direct Ethernet communication remains blocked. A segment must exist
on every PVE node to which its VMs may migrate. Shared snippets and local
disk migration behavior are still required. No physical switch topology is
discovered/rewired automatically by the console.
