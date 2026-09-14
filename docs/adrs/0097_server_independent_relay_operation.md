# ADR 0097: No Ham Radio Function Depends on Our Server

## Status
Accepted

## Context
Bruce's own words, verbatim, stated as a requirement to make true, not just an aspiration: "no ham
radio function is dependent on our server if you have installed the local relay and your remote
relays are directly reachable over internet. Logging will be queued until our server is up. You
should be able to control your radios, your friends and clubs authorized radios, and all repeaters
that are reachable via the Internet and for which you are authorized."

This lands the same night as several real, concrete instances of the opposite pattern being found
and fixed: `daemons/lotw_eqsl_sync`/`daemons/clublog_sync` centrally decrypting and using a user's
own third-party credentials from hams.com's own server (ADR 0095), and `ham_shack`'s WebRTC-direct-
client-offload path handing an AllStar/EchoLink password to the browser from the server on every
connection (`docs/proposals/ALLSTAR_ECHOLINK_RELAY_INTEGRATION.md`). Those are two concrete
instances of a general problem this ADR names explicitly: hams.com's own server has, in several
real places, become load-bearing for actions that are, in their real nature, a direct relationship
between a ham, their own equipment (or equipment they're genuinely authorized to use), and the
Internet -- not something that should ever require hams.com's own uptime to exercise.

Ham radio's own cultural and regulatory identity is partly built on operating when other
infrastructure fails (emergency communications, EMCOMM). A platform that manages ham radio
equipment and then makes routine operation of that equipment depend on its own central server's
uptime is working against that identity, not with it -- independent of ordinary reliability
engineering concerns, which apply here too.

## Decision

**Given `hams_local_relay` installed and running, and the specific remote endpoint (a friend's/
club's relay, or a repeater) directly reachable over the Internet, the following must work with
hams.com's own server completely unreachable:**

1. **Controlling your own radio** -- CAT control, PTT, audio I/O. Already true by this codebase's
   existing design (`hams_local_relay` talks to the radio directly; hams.com is not in that loop
   today). This ADR names it explicitly so it is never regressed, not because it needs new work.
2. **Controlling a friend's or club's radio you are authorized to use** (the `ham_station_timeshare`
   /`ham_operator_friendship` pattern) -- this is NOT yet true. `ham_relay_bridge/controllers/api.py`'s
   `validate_browser` -- the real gate for attaching a browser to ANY relay with PTT control,
   including someone else's equipment via timeshare -- is a live Odoo authorization check today,
   requiring hams.com to be reachable at the moment of connection. Making this ADR true requires the
   relay itself (the one being connected TO, i.e. the equipment owner's/club's own relay) to hold a
   locally-cacheable, periodically-refreshed authorization grant it can check without a live
   round-trip to hams.com -- see "Real tension to design around" below.
3. **Connecting to any repeater reachable via the Internet that you are authorized for** -- this
   includes AllStar/EchoLink (in progress, see the sibling proposal) and any other Internet-reachable
   repeater-linking system this platform supports or will support. The same authorization-caching
   requirement in (2) applies: whatever proves you're authorized for a given repeater must be
   checkable without hams.com being reachable at connection time.
4. **Logging queues, it never blocks.** QSO records, usage tracking, and any other data destined for
   Odoo must be written locally first and synced when hams.com becomes reachable again -- this
   pattern already exists (`hams_local_relay`'s own SQLite-backed QSO tracker, three independent
   status flags including `pushed_to_hams_com`) and is the model for every other operational record
   this ADR touches. A logging/sync failure must never be surfaced as an operational failure to the
   ham trying to use their radio.

**This is a standing constraint on all future feature work, not a one-time remediation list.** Any
new capability that requires the relay (or a browser talking to a relay) to make a synchronous call
to hams.com at the moment of an actual operational action -- as opposed to logging, telemetry, or
one-time/periodic credential and authorization refresh while online -- is presumptively a violation
of this ADR. Building such a feature requires an explicit, reasoned exception recorded against this
ADR, not a silent assumption that "the server is usually up."

## Real tension to design around, not paper over

Caching an authorization grant locally so it survives hams.com being unreachable is in real tension
with revocation: if Bruce (or a club admin) revokes someone's timeshare access, or a ham's own
authorization to reach a given repeater lapses, a purely-cached grant on the equipment owner's own
relay could keep honoring it until the cache refreshes. This ADR does not resolve that tension by
picking a specific bounded-staleness window here -- that is real design work (see night_shift_todo.md
for the concrete investigation this ADR requires) -- but it does set the shape of the answer: the
relay's own cache needs an explicit, bounded validity window and a clear, honestly-disclosed
worst-case revocation-propagation delay, not an unbounded "valid until told otherwise" grant. A
security design that achieves offline availability by silently sacrificing timely revocation is not
an acceptable resolution of this tension; the actual bound needs to be a real, considered number
(and probably configurable per equipment owner, since a club's tolerance for a stale grant may differ
from an individual ham's), not left unstated.

## Consequences

This is real, non-trivial architectural work, not a policy that's already satisfied by existing
code -- Bruce's own framing ("this will require some architectural changes") is accurate. It commits
this codebase to: a locally-cacheable authorization/grant mechanism on `hams_local_relay` for
timeshare and repeater access (new work), continuing the credential-locality work already underway
for LoTW/eQSL/ClubLog/AllStar/EchoLink (ADR 0095) as a strict subset of this broader mandate, and a
standing review question for every future feature ("does this need hams.com up at the moment of
use, and if so, why is that unavoidable"). The concrete audit of what in the current codebase still
violates this ADR, and the specific remediation plan for each gap found, is tracked in
`night_shift_todo.md`, not restated here -- this document states the standing policy, independent of
today's compliance status.
