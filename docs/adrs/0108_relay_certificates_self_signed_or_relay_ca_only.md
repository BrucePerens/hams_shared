# ADR 0108: Relay Certificates Are Self-Signed or From the Relay CA, Nothing Else (Reverses ADR 0100)

## Status
Accepted (Bruce, 2026-10-04, NIGHT_PLAN decisions 126 and 127). Reverses ADR 0100 in full.

## Context

By 2026-10-04 a relay could present four certificates: the fleet-shared `*.relay.hams.com` wildcard
(key on every relay), a per-user ACME certificate for `u<id>.local.hams.com` (DNS-01 through `ham_dns`),
the shared `localhost.hams.com` certificate of ADR 0100 (key on every relay, refreshed from hams.com
every six hours), and, newest, a leaf from the Relay Issuing CA for `<relay-id>.local` and LAN addresses.
The relay redesign audit (hams_com `docs/proposals/RELAY_REDESIGN_EMERGENCY_FIRST.md`, rows C2 to C5)
found that no browser candidate used the wildcard or the per-user name, that the shared certificate
needed DNS and a correct clock to do anything (so offline it did nothing), that three of the four put
a secret key on machines hams.com does not control, that seventeen clock checks gated the local control
path, and that the startup fetch of the wildcard awaited the network with no timeout before the listener
started.

## Decision

1. **A relay presents exactly one of two certificates.** Its own Relay CA leaf, when it holds one that
   has not expired and the client named `<relay-id>.local` or sent no SNI; otherwise its own
   self-signed certificate. There is no third branch (`sni_cert.rs`).
2. **The wildcard, the per-user ACME certificate and the shared `localhost.hams.com` certificate are
   removed** with their code, routes, daemons, accounts, units and tests: `/api/relay_bridge/tls_cert_bundle`,
   `/api/relay_bridge/shared_tls_cert_bundle`, `/api/v1/ham_dns/provision_local_cert`, the relay's
   `/setup_cert`, `daemons/relay_cert_renew`, `daemons/localhost_cert_renewal`, the `localhost_cert` OS
   account, and the browser's `localhost.hams.com` candidate. A source scan in the relay's tests
   (`certificate_cull_source_scan_tests`) fails if any of it returns.
3. **The self-signed click-through always works.** The self-signed certificate is never checked against
   the clock and the relay's startup waits on no network. The Relay CA leaf's notAfter is compared with
   the clock only to choose which certificate to present; failure falls back to the self-signed one, never
   to nothing. The relay serves HTTPS only (decision 127).
4. **Nothing replaces what ADR 0100 gave.** A browser on the same machine reaches `https://127.0.0.1:7388`
   and clicks through once, or installs the relay root (the opt-in padlock). Safari and other browsers
   that do not treat plain localhost as a secure context get the same click-through as every other
   browser; the measured behaviour on phones is in NIGHT_PLAN decision 155.
5. **Kept:** the Relay CA (`relay_ca_cert.rs`, `device_cert_renewal.rs`, `daemons/relay_ca/`, its Odoo
   models, crons, CRL (certificate revocation list) and the `ca_signer` host class). ADR 0097 is unaffected: nothing the relay serves
   depends on a hams.com reachable at connection time.

## Consequences

The shared TLS key and the wildcard key no longer exist on any relay or in `ir.config_parameter`; the
production database's stored wildcard key is deleted by `ham_relay_bridge`'s 1.1 pre-migration. Existing
relays that still hold the old files in their data directory ignore them (a test plants them). The
`localhost.hams.com` DNS records, the leftover `u<id>.local.hams.com` zones and the dev box's certbot
lineage are no longer used; deleting them is operator work (hams_com `docs/BRUCE_ACTION_ITEMS.md`).
The certbot packages stay, because `auth.hams.com` still gets its certificate from certbot on the dev box.
