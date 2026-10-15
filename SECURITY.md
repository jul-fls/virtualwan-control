# Security and trust model

The application includes a private administrator console and a separately authenticated
customer portal at /client. Restrict administrator routes at the reverse proxy;
never place client accounts behind an unrestricted administrator session.
Use HTTPS for remote access. The default Compose publishes localhost only.

## Secrets

Profiles contain references (`*_env`), not credential values. Values entered
in the UI are Fernet-encrypted in SQLite, never returned by the API. The
application key is `data/master.key`; protect the volume and encrypt full
backups. Possession of BOTH the database and this key permits decryption.
SSH private keys must be mounted below data, and are not encrypted by the
vault. Do not commit data, keys, .env, personal profiles, imports or logs.
Sessions use random HttpOnly/SameSite cookies and CSRF on all mutation APIs.
The public URL controls accepted Host/Origin and Secure cookies. No GET
request (including an email link) can approve or pay an order.

## Purchasing

Purchases require server-side enablement, authentication, an unexpired
provider quotation, matching revision, explicit contract acceptance and
an exact confirmation phrase. A fresh supplier GET checks prices/contracts
again. A POST checkout is claimed once, audited, and never automatically
retried after a timeout or process crash. Its uncertain state requires
manual reconciliation with the supplier account.
Auto-pay is a separate checkbox, false by default. The application does not
silently waive withdrawal rights. Order-delivery polling cannot create an
order. No VM is assigned to an IP guessed from an unrelated concurrent order.

## Execution

One Uvicorn process / one job worker; multiple controllers sharing a
firewall are not supported. Do not increase --workers or run replicas.
Each IP lease is acquired atomically. Failed deployments retain their IP in
quarantine. Retry keeps the saved VMID; it must refuse foreign/drifted VMs.
No broad rollback, deletion of customer VMs or network reset is automatic.
SSH host keys and pfSense TLS fingerprints/CAs are verified explicitly.
An administrator adding a new host key must check its fingerprint out of band.

Network isolation does not guarantee against a QEMU/kernel VM escape. The
multi-segment isolation service is a new extension: test it on your actual
PVE kernel/firewall paths before installing clients on a new segment.
First-time physical networking/firewall onboarding uses reviewed artifacts,
not guessed interface names or silent whole-config replacement.

For reporting a vulnerability, contact the repository maintainer privately
using the contact method supplied on that repository. Do not publish tokens,
private keys, customer data or full personal firewall snapshots in an issue.

## HTTP behind a reverse proxy

The application serves HTTP only using Uvicorn. TLS/certificates are managed
by the operator's Cloudflare Tunnel/Access or Nginx Proxy Manager. Set
VWAN_PUBLIC_URL to the actual public HTTPS URL and preserve the Host header;
Secure cookies and origin checks remain enabled. Internal admin authentication
is retained even behind Cloudflare Access.

Use a long unique VWAN_ADMIN_PASSWORD. Changing it and restarting revokes all
existing administrator sessions. Environment credentials override vault values; do not publish
.env or Docker inspect output. VWAN_TRUSTED_PROXY optionally trusts the exact
address of one direct proxy for one X-Forwarded-For hop; wildcards are rejected.
Otherwise forwarded client headers are ignored. The controller currently has
one environment-managed administrator account; customer accounts have TOTP MFA
and passkeys. Protect administrator access separately with your trusted proxy.

Fresh-router onboarding requires explicit CONFIGURER confirmation and pinned
SSH host keys. It verifies the OVH service primary, schedules a timed rollback,
and reconnects before cancelling it. It refuses existing installations and does
not mark the home's firewall/bridge configuration as ready. Validate this new
onboarding path on a disposable Debian 13 router before using it in production.

## Client portal boundaries

- Distinct HttpOnly/SameSite session cookies, SQL-backed expiry, CSRF and exact
  Origin checks. Client cookies never authorize administrator API routes.
- A client sees only explicitly assigned deployments. Every VM/status/firewall,
  reverse, rescue and console request checks account ownership server-side.
- Remote commands use fixed verbs, quoted arguments, pinned SSH keys and the
  isolation registry: actual VMID, node, MAC, public IPv4 and bridge must match.
  Existing clients may not modify resource topology, nodes, storage or ISO URLs.
- Client firewall edits preserve administrator rules and their precedence.
  They stage disabled rules before replacing the prior client generation. Each
  edit refreshes the Proxmox digest. The operations are sequential, not a single
  Proxmox transaction: a failure is audited and requires rereading/retrying.
  Global/host firewall settings and existing connection states are never reset.
- Console: local noVNC assets, one-use 60-second grant bound to client session,
  account and assigned VM; authenticated WebSocket on the same HTTP listener,
  SSH tunnel to `qm vncproxy`, no public VNC port and no infrastructure login
  sent to the browser. Sessions/ownership are rechecked every 10 seconds.
  Limits: two concurrent consoles per account, 32 per process, one-hour cap.
- Passwords use Werkzeug scrypt hashes. TOTP seeds are Fernet-encrypted, used
  time steps cannot be replayed, recovery codes are high-entropy one-use hashes.
  WebAuthn uses python-fido2 with exact RP/origin and user verification required.
  Security enrollment/removal and password/email changes require reauthentication
  (password + MFA, or a passkey login within five minutes).
- Email is a login identifier; this version does not verify mailbox ownership,
  send invitations or offer public self-registration/password-recovery links.
  The administrator can reset access after verifying the customer's identity.
- Rescue is opt-in via a pre-existing administrator-selected storage ISO and
  requires a stopped VM. It does not download arbitrary images or format disks.

Account data, authenticator credentials and audit logs are private operational
information. Back up the complete volume before upgrading. The DB is migrated
at startup; test restoration separately. No claim of hypervisor escape prevention
is made by this network/control-plane isolation.
