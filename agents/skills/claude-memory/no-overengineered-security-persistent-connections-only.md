---
name: no-overengineered-security-persistent-connections-only
description: "Bruce's standing policy against over-engineered security: authenticate once at the start of persistent TLS/WebRTC connections, no short-lived tokens, no 120-second windows."
metadata: 
  node_type: memory
  pinned: true
  modified: 2026-10-08T17:00:00.000Z
---

# Authenticate Once at Connection Start: No Over-Engineered Security

On 2026-10-08, Bruce established an absolute, standing rule regarding authentication architecture across hams.com, hams_local_relay, and client connections:

> "I am loath to over-engineer security. We set this system up to authenticate once at the start of a WebRTC connection, via the normal TLS channel, and not again. We did that after multiple agents had to be constrained from over-engineering security and we had to re-engineer the relay twice because of this. I do not want ANY short-lived tokens on the system, only long-lived ones. I do not want any 120 second windows, only persistent connections. This is critically important, it is a severe bug in agents that their training causes them to over-engineer security."

### Architectural Directives:
1. **Authenticate once at connection start**: WebRTC and relay uplink connections authenticate when established over the standard TLS/DTLS channel, and NOT on subsequent frames or calls.
2. **NO short-lived tokens**: Do not introduce bearer tokens, temporary session tokens, or ephemeral ticket mechanisms. Only long-lived credentials (like browser member identity certificates or persistent relay pairing keys) are permitted.
3. **NO 120-second time windows**: Do not build per-request timestamp freshness windows (e.g. now - stamp < 120s). They introduce fragile clock-synchronization failure modes and over-complicate simple RPC routes.
4. **Persistent connections**: Use persistent connections (WebRTC data channels, authenticated RPC uplinks) to carry ongoing operations instead of repeatedly re-authenticating individual RPC requests.
5. **Fail-open on network trouble**: A relay must never block an amateur radio operator from transmitting or using their station because an upstream server or authentication check is temporarily unreachable.
