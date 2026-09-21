# ADR 0100: A Shared `localhost.hams.com` Certificate, With Its Private Key On Every Relay

## Status
Accepted

## Context

`hams_local_relay` terminates TLS itself, because a browser on an `https://` page will not open a
plain `ws://` connection (mixed content). It already obtains a per-user certificate for
`u<id>.local.hams.com` for itself (`local_cert_acme.rs`, DNS-01 through `ham_dns`; the key never
leaves the operator's machine). That certificate serves LAN devices well. It does not serve a
browser on the same machine as the relay well: such a browser has no per-user hostname to use, and
Safari and other browsers that do not treat plain `localhost` as a secure context will not connect
to the loopback address at all without a publicly trusted certificate for a name.

A second, independent constraint favours a central certificate: every per-user certificate counts
against Let's Encrypt's new-certificates-per-registered-domain limit, so per-user certificates alone
cannot scale to the whole membership.

The cost of a central certificate is real and was put to Bruce plainly before he decided: the private
key of one shared `localhost.hams.com` certificate has to sit on every relay, delivered over the
authenticated daemon_token channel. Anyone who compromises one relay holds a valid TLS key for that
name.

Bruce's decision, 2026-09-20, verbatim: "Yes, build both certificates." His reasoning, accepted here:
the relay is authenticated at the application layer (a pinned key plus a Noise handshake), so a copied
TLS key cannot impersonate a relay. The residual risk is someone who controls a victim's DNS answering
for `localhost.hams.com`, and even that attacker obtains a TLS channel to nothing the relay's own
authentication would accept.

## Decision

**1. Both certificates coexist; neither replaces the other.** The relay serves the per-user
`u<id>.local.hams.com` certificate (LAN devices; key stays on the machine) and one shared
`localhost.hams.com` certificate (a browser on the same machine). The relay chooses by SNI:
`localhost.hams.com` gets the shared certificate; every other name, an IP address or no SNI at all
gets what the relay served before (its per-user certificate when it has one, otherwise the
distributed wildcard, otherwise self-signed).

**2. One central renewal daemon owns the shared certificate** (`daemons/localhost_cert_renewal`). It
uses `certbot` with DNS-01 on the Cloudflare zone that hosts hams.com, the same ACME client
`relay_cert_renew` already uses; there is no second ACME client. It renews at 30 days or fewer
remaining, keeps serving the old pair until the new one is verified (parses, names exactly
`localhost.hams.com`, key matches, valid now, chain present and signed link by link, newer than what is
served), replaces atomically, backs off exponentially on failure, and alerts by failing its systemd
unit, which the existing pager_duty systemd-failure check pages (also when 14 days or fewer remain).
`localhost.hams.com` resolves to `127.0.0.1` and `::1` through DNS-only A and AAAA records in that
zone.

**3. The key lives on the server as files, never in the database** (ADR 0095). It is owned by a
dedicated `localhost_cert` account, mode 0640 in a 0750 directory, readable only through that
account's group, of which the web tier's account is the sole other member. It is never in a log line,
an exception message or chatter. (The older `*.relay.hams.com` wildcard key is kept in
`ir.config_parameter`; that predates this ADR and is not a pattern to copy.)

**4. Distribution is one narrow authenticated route**, `/api/relay_bridge/shared_tls_cert_bundle`,
with the same authentication as `noise_signing_key`: `node_id` plus `daemon_token`, compared in
constant time. It answers over TLS only, with `Cache-Control: no-store`, rate limited per node, and
serves only from the renewal daemon's fixed directory. The relay fetches at startup and every six
hours, refuses anything that does not pass the same validation the daemon applies (and requires the
exact name `localhost.hams.com`), and replaces its saved copy by staged writes and renames with the key
at 0600.

**5. Reconciliation with ADR 0097.** A central daemon and a central endpoint are a single point of
failure for the shared certificate, and this is acceptable only because nothing depends on it. The
relay persists the last good certificate and keeps serving it, with hams.com down, until that copy's
own expiry. A failed, refused or rate-limited fetch never touches what is being served. With no shared
certificate at all, the relay serves exactly what it served before this ADR, and the per-user
certificate remains the default for every name but `localhost.hams.com`. No radio function requires
this certificate; it only removes a browser warning for a browser on the same machine.

**6. The web client** tries `wss://localhost.hams.com:7388` first, ahead of the existing `127.0.0.1`
candidate, and falls through the existing queue on any failure, so a relay without the shared
certificate behaves exactly as before.

## Consequences

A compromised relay exposes the shared TLS key. That is accepted for the reasons above; it must not be
reused for anything a TLS key alone would authenticate. Rotating the certificate (renewal is at most
every 60 days) bounds any leak. Any future feature that would let TLS alone stand in for the relay's
application-layer authentication must revisit this ADR first, because that is the assumption it rests
on. Any change that moves the key into the database, a log or a cache, or widens who can read the
served directory, is a regression against this ADR and ADR 0095.
