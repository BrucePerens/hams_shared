# ADR 0101: AllStar SIP Credentials Are Synced to the User's Own Browsers (Bounded Exception to ADR 0095)

## Status
Accepted

## Context
Bruce's direction of 2026-09-21: the browser goes to an AllStar repeater's own gateway directly, with no
local relay and no hams.com relaying in the audio or signalling path ("It should not go through our server
for relaying, allstar repeaters are directly reachable from the browser"). The browser therefore speaks SIP
over WebSocket (WSS) and WebRTC itself, and must present the user's SIP credentials (username and
password) to that gateway. His answer on where the credential lives: "Save it in the browser and server,
refresh the browser from the server when the user logs in, so that all of their browsers will work without
the net when necessary."

ADR 0095 says third-party credentials stay on the user's own system and are not centrally decrypted or
handed around. ADR 0097 says no ham radio function may depend on our server. Both point the same way for
this feature, but they collide on one detail: a browser that is offline must still be able to connect to
the repeater, so it must hold the password locally; and for a user's other browsers to have it too, the
server must hold a copy to sync from.

## Decision
**1. A bounded exception to ADR 0095, for one credential class.** The user's own third-party AllStar SIP
password (`res.users.allstar_password`) is stored on the server, encrypted at rest (Fernet, the existing
`allstar_password_crypt` field), and synced to that user's own browsers. Nothing else in ADR 0095 is
relaxed; every other third-party credential keeps its existing rule. EchoLink has no browser path and is not
part of this exception.

**2. Why the exception is justified.** Offline operation (ADR 0097) requires the browser to hold the
password. Multi-browser operation without the network requires a server copy to refresh each browser from
at login. The password is the user's own login to a repeater run by a third party, not a hams.com
credential, and it is not used by hams.com for anything: hams.com is not in the connection path.

**3. Mitigations, all required.**
  a. One deliberate, narrow endpoint returns it: an authenticated JSON route (`auth="user"`), TLS only,
     `Cache-Control: no-store`, rate limited, returning only the caller's own record. The password is
     never logged and never appears in an error message.
  b. Service accounts and other users can never obtain it. The `res.users` field decrypts only for its
     owner; a service account is refused by the endpoint outright.
  c. The endpoint is separate from the signalling-metadata endpoint
     (`/api/v1/repeater/webrtc_credentials/<id>`), which stays password-free.
  d. In the browser the config is kept in IndexedDB and the password is encrypted with a per-origin,
     per-browser WebCrypto AES-GCM key that is generated non-extractable; the `CryptoKey` object itself is
     kept in IndexedDB. The password is never placed in localStorage, a URL, a log line, an error message
     or telemetry.
  e. The browser copy is refreshed from the server on login/opening the shack while online, and
     periodically while online. It survives account logout (offline operation is the point) unless the user
     chose "Forget on this device", which deletes it.
  f. The credential store is best-effort protection against casual disclosure, not against code running in
     the page origin: script in the hams.com origin can use the key it cannot read. This is stated to users.

**4. Transmit responsibility.** There is no server or relay in this path, so no hams.com licence gate can be
enforced on it (Bruce, 2026-09-21: the operator's login to their own repeater is their authorization). The
endpoint does not apply the "Verified Ham" gate that guards `webrtc_credentials`, because it returns the
user's own secret and is not a transmit authorization. The UI carries a notice that the operator is
responsible for staying within licence and band limits.

## Consequences
The server holds one more decryptable-by-owner secret per user than ADR 0095 alone would allow, and the
browser holds it too. A regression against this ADR is any of: returning the password to anyone but its
owner, logging it, widening the decrypt guard back to service accounts, routing the audio or SIP signalling
through hams.com, or storing the password in browser storage other than the encrypted IndexedDB record.
The relay-side network-only session (`sip_credentials.rs`, `rsipstack`) stays as an optional path
(Bruce, 2026-09-21).
