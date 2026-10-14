# Security and trust model

This is an infrastructure administrator console, not a customer portal.
Keep it on a management network or behind an authenticated private tunnel.
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

One Gunicorn process / one job worker; multiple controllers sharing a
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
