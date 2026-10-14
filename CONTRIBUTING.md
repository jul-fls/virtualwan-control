# Contributing

Use Python 3.12/3.13, install requirements.txt and run the tests. Changes to
provisioning must preserve the primary management route, public /32 without
NAT, MAC/IP isolation and a first preflight before writes.

Provider purchase adapters must implement quotation separately from checkout.
Never create a purchase from a polling job, a GET/email link or an implicit
capacity event. Preserve exactly-once claim, expiration/price verification
and the uncertain state on ambiguous supplier responses.

Use documentation IPs/domains in examples. Include no personal profiles,
credentials, private keys, firewall XML, production inventories or customer
logs. Do not use live supplier purchases or personal networks in CI.

New provider adapters should have explicit capabilities and tests for failures,
ownership/routing conflicts and compensation. An unsupported backend should
fail with a clear error rather than claim a successful simulated operation.

Contributions are submitted under Apache-2.0 unless agreed otherwise with
the maintainer. Report security issues privately as described in SECURITY.md.
