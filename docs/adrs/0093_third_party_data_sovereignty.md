# ADR 0093: Third-Party Data Sovereignty Against Vendor Cutoff

## Status
Accepted

## Scope
This ADR governs how this codebase (`hams_com`, `hams_open`, `hams_shared`) treats its dependence
on **non-government, commercial third parties** for data or verification capability this platform
relies on. It explicitly does **not** cover government/regulatory data sources (FCC, ISED, ACMA,
Ofcom, ANATEL, BNetzA, RSM, NCVEC, and similar) -- those already have their own well-established
sync daemons (`daemons/*_sync`) built on the assumption of long-term, stable public-record access,
and a government regulator withdrawing access to its own public licensing records is a different
kind of risk this ADR isn't written to address. It also doesn't cover ordinary code
dependencies (see ADR 0087, third-party dependency *version* tracking) -- this ADR is about losing
*access* entirely, not about a dependency drifting out from under us while access continues.

## Context
Bruce's own words, verbatim, 2026-09-10, prompted while deciding whether to pursue a Feedline Labs
(Greyline Fabric) API integration for real-time repeater activity data: "I don't want to be in the
situation that Greyline withdraws our access and we are left without the data. Thus, suck all of
their data down into the repeater database, update it when possible, but plan on the possibility
that they might shut us out. We should be similarly defensive against the other companies (not
government entities) that we interface with today -- they may be threatened by the size and scope
of our project and shut us out." Followed immediately by a second, sharper instance: "I am betting
that QRZ eventually finds us a threat. Maybe even ARRL."

This names a real, general risk this codebase had no standing answer for: a commercial third party
providing data or a verification capability this platform depends on may, at any point and for
reasons entirely outside our control (a competitive threat perception, a policy change, a business
decision), cut off access -- and by the time that happens, it's too late to have planned for it.
Every current integration with a commercial third party (Greyline Fabric, QRZ, ARRL/LoTW,
OpenWeatherMap, AISHub, OpenSky Network, APRS.fi, and any future one) carries this risk to some
degree, and the risk is sharper, not smaller, the more successful and visible this platform becomes
-- exactly Bruce's own framing ("threatened by the size and scope of our project").

**These third-party dependencies split into two structurally different shapes, needing two
different defenses:**

1. **Data-source dependencies**: a third party's API returns *data* this platform displays, stores,
   or acts on -- repeater activity (Greyline Fabric), weather overlays (OpenWeatherMap), AIS marine
   tracking (AISHub), ADS-B aircraft tracking (OpenSky Network), APRS position data (APRS.fi). This
   data can, in principle, be captured and kept even after the source that produced it disappears.
2. **Capability/verification dependencies**: a third party's API doesn't hand over data to keep --
   it performs a real-time *action or judgment* this platform's own trust model depends on: ARRL's
   LoTW mTLS certificate handshake (proving a specific, real license holder controls that
   certificate right now), QRZ's biography-edit challenge (proving control of a specific QRZ
   profile right now). There is nothing to "cache" here -- the value of the capability is that it
   happens live, at verification time, against the third party's own current infrastructure. Losing
   access to a capability dependency doesn't erase historical data (whatever it already verified
   stays verified, since the *result* -- `is_identity_verified`, `verification_date`,
   `onboarding_method` -- is already a real, persisted field on `res.users`, confirmed via
   `ham_onboarding/models/res_users_verification.py`), but it does remove that path for *future*
   verifications going forward.

## Decision

**1. Data-source dependencies: ingest and persist, don't just proxy.** Any commercial API this
codebase queries for data (not merely for a one-time verification action) should have that data
synced into a real, owned Odoo model on a recurring basis -- not queried live, on-demand, with
nothing kept afterward. A live query against the current API remains fine for a real-time overlay
UI where freshness matters more than historical continuity (Greyline Fabric's own
`get_active_repeaters()` stays a legitimate live-query method for exactly that use), but the same
data flowing through that call should also land in a persisted table via a periodic sync job, so a
future access cutoff leaves this platform with whatever was captured up to that point, not nothing.
See `docs/proposals/GREYLINE_FABRIC_DEFENSIVE_INGESTION.md` for the first concrete implementation
of this pattern.

**2. Capability/verification dependencies: diversify, don't single-source.** A trust decision this
platform's own security model depends on (identity verification, callsign upgrade, license
proof) must never rest on exactly one third party's continued willingness to cooperate.
`ham_onboarding` already has a real, working instance of this pattern, discovered rather than
newly designed by this ADR: **LoTW mTLS**, **QRZ bio-edit challenge**, **official-email OTP**
(against the FCC/ISED's own on-file public email, a *government*-sourced fact, not a commercial
third party), **a Morse-code callsign challenge requiring no third party at all**, and **AI/admin
review of an uploaded license document** are five structurally independent verification paths,
none of which depends on any of the others staying available. A future ARRL or QRZ cutoff removes
one path, not the platform's entire ability to verify anyone -- this diversification must be
actively preserved as new verification work is added, not allowed to erode into an unstated
single-path dependency (e.g., a future feature that quietly assumes "every real user has done LoTW"
would be exactly the kind of regression this ADR exists to prevent). No specific new engineering
task follows from this half of the decision today -- the existing design already satisfies it; the
obligation is to keep satisfying it as the platform grows, not to build something new right now.

**3. New commercial third-party integrations must state, at design time, which category they are**
(data-source or capability), and must have their sovereignty story stated explicitly before being
built: a data-source integration names its sync model and cadence; a capability integration names
what other independent path already exists (or is planned) so it is never the sole verification
route for anything trust-sensitive.

**4. A follow-up inventory audit is owed, not yet performed as part of this ADR**: every current
commercial API integration should be checked against this decision and classified. Known
candidates, not yet individually audited: `web_map`'s APRS.fi/OpenWeatherMap/AISHub/OpenSky
integrations (believed to be proxied through a local daemon per `ham_map_settings.py`'s own
comment, but not confirmed to persist any of that data locally), and any future integration this
ADR doesn't yet know about. Track the audit's own findings in `night_shift_todo.md` as they're
confirmed, one integration at a time, rather than guessing at their current state here.

## Consequences

A commercial third party cutting off access becomes a degraded-but-survivable event -- stale but
present data for a data-source dependency, one fewer (but not the only) verification path for a
capability dependency -- instead of an unplanned, total loss of function or trust-model integrity.
This does add real, ongoing engineering cost: every new data-source integration now needs a sync
model and cron, not just a live-query wrapper, and every new trust-sensitive feature needs to be
checked against whether it's quietly narrowing the platform back down to a single verification
path. That cost is accepted deliberately, given the stated risk is not hypothetical -- it's Bruce's
own considered expectation for specific, currently-integrated named parties (Greyline, QRZ, possibly
ARRL), not a generic worst-case exercise.
