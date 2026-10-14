# Validation — 2026-10-14

## Executed locally

- Python suite: 38 portable tests passed on Windows; all 44 tests passed in
  the Linux Docker suite, without networking. Six Linux isolation tests require
  `fcntl` and are skipped on Windows.
- Concurrent requests cannot reserve the same IP twice; failed deployment
  quarantines the IP; primary management IP cannot enter client stock.
- Capacity includes existing stock and pending purchase quantities. Cloned
  profiles require a distinct subnet, service, segment and aliases.
- Approval requires a matching revision, unexpired quote, confirmation phrase,
  accepted contracts and unchanged supplier price/contract/line-item digest.
- Verified the official OVH schema: cart prices are nested under `prices`
  (`withTax`, `withoutTax`, tax, reduction), unlike billing-order prices.
  Unknown/negative/non-finite prices cannot be approved. Currency is checked.
- Checkout claim is once-only. Timeout and restart produce uncertain state,
  never a second automatic checkout. Supplier delivery must be `delivered`
  before assignment; notPaid does not start a VM.
- Session auth, Host/Origin checks, CSRF mutations, security headers, encrypted
  vault and non-return of secret values; demo volume cannot switch to live.
- End-to-end HTTP tests exercise profile/model editing, cloning, inventory job
  queues, simulated creation, quotation/approval, event views and cancellation.
- Docker image build completed with locked dependency versions, non-root user,
  no host privileges, private data volume and required customized admin password.
- Final image health response passed in a network-disabled, read-only container
  with dropped capabilities and temporary data, without publishing a port.
- JS syntax check and Python compilation passed.
- Both Compose variants passed configuration validation; neither was deployed.
- Headless Chrome visual/function test: eight views, login, simulated client
  from available stock, quote ready and approval dialog. No approval was sent
  through that UI test and no external requests were allowed by the browser.
  No page/JavaScript errors. Desktop and mobile screenshots reviewed.
- Private migration kit generated from the local existing CLI config/registry,
  with encrypted credentials. It made no network request or deployment and is
  excluded from Git, Docker context and public source export.

The integrated page-automation helper failed to initialize; visual verification
used a separate headless development test of this application, not automation
of a user's real account/browser state.

## Not executed or claimed

- No purchase, preferred-payment debit, supplier cart attached to a real account,
  customer VM creation or physical network change by this new application.
- No deployment of Docker Compose to the management VM; the operator will do it.
- No SMTP message to a real recipient. Configure/test delivery separately.
- No claim that the new multi-segment nft extension was exercised on the actual
  two-node PVE cluster, or after host/firewall reboots. The earlier single-segment
  implementation was validated separately; keep it intact until a new-segment
  test is scheduled.
- GitHub CI file prepared but not executed on GitHub; no public repository/push
  was created by these local development checks.

## First controlled live recipe

1. Deploy privately with purchases/provisioning disabled, restore/import the
   private volume, check SSH/TLS pins and read actual inventory.
2. Use an owned spare IP and disposable VM to validate the new wrapper without
   purchasing. Confirm DNS/PTR, guest SSH key/account, public-only IPv4, traffic,
   reboot and the actual isolation paths before admitting a customer.
3. Enable purchase rights explicitly, prepare an actual catalogue offering,
   inspect prices/renewal terms/contracts and approve one small order yourself.
4. Observe actual fulfillment domains/IPs and the combined deployment. If
   the supplier does not expose an unambiguous order→IP relationship, reconcile
   the purchased stock manually; do not enable guessed-difference assignment.
5. For another router, first approve its own purchase, then review onboarding
   artifacts and test a distinct tunnel/VLAN/bridge/segment. Test multi-node
   migration and startup order on your hosts before client production.

No automated live purchase is required to run this test suite or publish code.

## Revision: integrated server, root CI, environment and SSH onboarding

- Active workflow moved to the Git root; its Docker context is platform/.
  actionlint passed. PR builds do not log in or publish; main/version tags publish
  only after the Python matrix succeeds. GitHub Actions/GHCR were not executed.
- Caddy Compose variant removed. The single-container image was built locally.
  Previous Gunicorn HTTP/TLS checks (superseded by the HTTP-only revision) passed in a read-only,
  non-root, network-disabled ephemeral container without exposed host ports.
- Added environment profile/model imports and environment-first credentials,
  session invalidation on administrator credential changes and explicit fresh-
  router SSH jobs. Auth/CSRF, duplicate requests, /30 overlap and refusal of
  wrong primary/OS/existing installation are covered by the regression suite.
- New SSH onboarding has not been executed on an actual second OVH VPS. It
  requires an authenticated pinned host, fresh Debian 13, a verified OVH service,
  /30 and pfSense peer public key. It installs only the remote router; first-time
  home pfSense/PVE tunnel/VLAN/bridge configuration remains operator work.
- No management VM deployment, purchase or production network change.

Final revision regression: all 59 tests passed under Linux without networking.
Public export audited against seven actual local secrets and personal network
identifiers: no matches; 51 archive entries, including the root workflow.

## HTTP-only revision

Gunicorn and built-in TLS/certificate configuration removed. Waitress 3.0.2 is
the single HTTP-only server on port 8080. Public HTTPS is terminated externally
by Cloudflare Tunnel/Access or Nginx Proxy Manager; PUBLIC_URL still controls
Secure cookies and origin checks. Optional TRUSTED_PROXY trusts one exact proxy
address for one X-Forwarded-For hop; wildcard trust is not enabled.

Validated: all 59 Linux tests passed with the Waitress image. Read-only isolated
HTTP startup/health passed; login behind a public HTTPS URL sets Secure/HttpOnly
cookies. Untrusted X-Forwarded-For is ignored; the explicitly trusted proxy
correctly separates client login rate limits. No host ports or deployment used.

## Public repository simplification

Distribution now uses the repository root as Docker context. Pipeline reduced
to checkout/login/build/push with the single image tag latest, no Python matrix
and no pinned action revisions. PRs build without login/push. The earlier matrix
workflow description is superseded; local test results remain separate from CI.

## Connection overrides and SSH trust messages

Dedicated pfSense URL/connect IP and PVE host/port/node/username variables apply
to the selected profile, with explicit selection required for multiple profiles.
The SSH preflight now reports the UI enrollment action before spawning PVE
inventory when the host-key file/key is missing. RejectPolicy remains enabled.
This change was tested locally without contacting pfSense, PVE or the router.
