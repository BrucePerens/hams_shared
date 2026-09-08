---
name: hams-local-relay-tls-plan
description: "The planned fix for browser mixed-content blocking on hams_local_relay's ws:// WebSocket server -- issuing a self-owned certificate for 127.0.0.1, vendor-style."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-18T15:29:13.231Z
---

hams.com's `hams_local_relay` Rust daemon (in `daemons/hams_local_relay`) runs a WebSocket server on the operator's own machine (default port 7388) that the browser frontend (`ham_shack/static/src/js/web_transceiver.js`) connects to directly for local/LAN/NAT-direct radio control and audio, using plain `ws://` since the daemon has no TLS support today. Because the hams.com page itself loads over `https://`, browsers block these `ws://` connections outright under mixed-content rules (no exception exists for `127.0.0.1` or private IPs) -- so the local-mode and local/NAT fallback legs of the connection queue cannot currently establish from a real browser tab.

The user's stated plan for resolving this is to issue their own TLS certificate for `127.0.0.1`, the same approach other vendors (e.g. Plex) have taken for local-daemon-to-browser connections, rather than working around mixed-content blocking some other way. This is a standing architectural direction for this codebase, not an open question -- when this mixed-content limitation comes up again, the answer is "certificate issuance for 127.0.0.1 is the planned fix," not a topic to re-investigate or propose alternatives for.

The concrete production design the user described: a dedicated daemon (not yet built) maintains a Let's Encrypt certificate for a shared hostname, e.g. `localhost.hams.com`, obtained via the DNS-01 challenge -- DNS-01 is the natural fit because hams.com already controls its own DNS zone (the `ham_dns` module manages DNS records/zones in this codebase, and is the likely integration point for programmatically creating the `_acme-challenge` TXT record). That hostname is then mapped, via a normal DNS A/AAAA record, to `127.0.0.1` -- the same loopback trick vendors like Plex use with `*.plex.direct` -- so a single centrally-renewed, publicly-trusted certificate works for every operator's `hams_local_relay` instance without needing a per-machine cert. The renewing daemon then has to hand that certificate (and its private key) to each running `hams_local_relay` instance over a secure channel; the exact distribution protocol wasn't specified but should reuse this codebase's existing Odoo-authenticated daemon relationship (daemon_token / API key) rather than inventing a new trust mechanism.

As of this note, none of this had been implemented yet -- only the direction was confirmed. In the interim, `hams_local_relay` generates and persists its own self-signed certificate (via `daemons/hams_local_relay/src/tls.rs`, using the `rcgen` crate) purely so `wss://`/`https://` work end-to-end for development and testing; browsers will show an untrusted-certificate warning on that self-signed cert until it's replaced by the real DNS-01-issued one described above.
