# ADR 0098: Public-Key Friend/Club Authorization and Direct Relay Connectivity

## Status
Accepted

**Superseded in part 2026-10-23:** hams.com no longer uses ARRL (American Radio Relay League) Logbook of the World (LoTW) or TQSL (Bruce's decision); LoTW mentions below are kept as history.

## Context
ADR 0097 (no ham radio function depends on our server) named a real, unresolved tension: caching a
timeshare/friendship authorization grant locally on the equipment owner's own relay, so it can admit
a connection without hams.com being reachable, trades off against timely revocation -- and that ADR
deliberately did not pick a specific answer, naming it as real design work still owed.

Bruce's own words, verbatim, resolving that tension with a concrete architectural direction: "We're
going to have to transition the friend and club system to a public key system. The local relays can
hold on to the authorized keys, etc. This offloads a lot of traffic from our Central relay, which
should only be used when the hams local relay is not directly reachable. I think ipv6 is going to be
a very big help here because it mostly does not use NAT (network address translation)."

This is not a green-field cryptography project. `hams_local_relay` already has a real, working
public-key verification pipeline: `noise_channel.rs` implements a Noise Protocol
secure channel (there is no `noise_session.rs`) and `attestation_subcarrier.rs`, `revocation.rs` and `noise_channel.rs` use Ed25519 (`ed25519_dalek`) verification, where a client presents a
signed attestation the relay verifies against a signing key sourced from Odoo (per
`hams_com/docs/proposals/done/TRANSMITTER_HIJACK_PREVENTION.md` section 1). Friend/club authorization via public
keys is a natural extension of infrastructure that already exists and is already proven, not a new
mechanism to build from scratch.

## Decision

**1. Friend/club authorization moves from a live per-connection Odoo lookup to a locally-verified
public-key credential.** Each ham (or their relay/client) holds a real keypair. Granting a
friendship or club timeshare authorization means recording that ham's PUBLIC key as authorized --
not just a database row hams.com checks live. The equipment owner's own relay holds the current set
of public keys authorized to control it, refreshed from hams.com on a real, periodic sync interval
while reachable (mirroring the sync-worker pattern already established for LoTW/eQSL/ClubLog), and
verifies a connecting party's signature against that local set directly -- no live Odoo round-trip
needed at connection time. Revocation means the next sync removes a key from the local set; the
sync interval IS the honest revocation-propagation bound, and must be stated as a real, considered
number (not "however long feels right") once the actual implementation scopes it -- a club's own
risk tolerance for a stale grant may reasonably differ from an individual friendship-based grant,
per ADR 0097's own framing of this same question.

**2. hams.com's own relay infrastructure ("the central relay") is a fallback path, not the default
one.** A connection between two relays (or a browser and a relay) should attempt direct connectivity
first -- relay-to-relay or browser-to-relay, without hams.com's own infrastructure proxying the
actual control/audio traffic -- and only fall back to a hams.com-proxied path when direct
connectivity genuinely isn't available (a NAT/firewall configuration that blocks it). This
offloads real, ongoing bandwidth and compute cost from hams.com's own infrastructure and is a
direct instance of ADR 0097's own goal: the central relay's role shrinks to what it's actually
needed for (connection facilitation when direct reachability fails, and signaling/discovery), not
carrying routine traffic that two directly-reachable endpoints don't need it for.

**3. IPv6 is a first-class connectivity strategy, not an afterthought.** IPv6 substantially reduces
the prevalence of NAT compared to IPv4, meaning direct connectivity (per decision 2) is achievable
far more often when both endpoints have real IPv6 connectivity. Connection-establishment logic
should prefer direct IPv6 connectivity when both endpoints support it, fall back to NAT-traversal
techniques (STUN-equivalent hole-punching, which lets two machines behind home routers open a direct connection by first exchanging their public addresses through a third party) for IPv4/NAT'd endpoints, and only fall back to the
hams.com-proxied path (decision 2) when neither achieves a direct connection.

## Consequences

This is real, substantial engineering, not a policy already satisfied by existing code: a key
distribution/revocation-sync mechanism for the local authorized-key set (extending, not replacing,
the existing Noise/Ed25519 attestation infrastructure), a real connection-establishment strategy
that tries direct connectivity before falling back to hams.com's own relay, and IPv6-aware
connectivity preference in that strategy. The concrete technical scoping (which existing key
material to build on, the real current connection-establishment architecture and how much of it
already attempts direct connectivity vs. defaults to a hams.com-proxied path, and the specific
revocation-sync interval to adopt) is tracked in the night-shift to-do files, not restated here -- this
document states the standing architectural direction, independent of today's implementation status.
This ADR is the concrete resolution of the tension ADR 0097 named without resolving; read them
together.

## Status check 2026-11-12

Checked against the code: decision 1 is built. Browsers register keys (`ham_relay_bridge/controllers/relay_access.py`:
`browser_key/register`, `account_keys`), hams.com signs the per-relay operator list and sends it down at login (`validate_node`
returns `operator_list`), and the relay verifies a browser by key with no live Odoo call
(`docs/proposals/RELAY_PROTOCOL_SIMPLE.md` 1.5 to 1.6, 7.3). The revocation bound is "the next uplink login or change", with no
expiry on a stored list; the owner can also block a key at the relay. For decisions 2 and 3 the relay has ICE (Interactive
Connectivity Establishment) code under `daemons/hams_local_relay/src/ice_control*`, `ice_link*`, `ice_signaling*`, and the
protocol document's section 8.6 covers the mapped address and IPv6; hams.com's signaling ticket route is
`/api/relay_bridge/signaling_ticket`.
