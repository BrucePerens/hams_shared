# ADR 0095: Third-Party Credentials Stay on the User's Own System

## Status
Accepted

## Context
`ham_logbook/models/res_users.py` stores six Fernet-encrypted credential fields on `res.users` --
`lotw_password`, `eqsl_password`, `allstar_password`, `echolink_password`, `clublog_password`,
`clublog_api_key` -- third-party passwords/API keys a ham entrusts to this platform so it can sync
their logbook with LoTW, eQSL, AllStar, EchoLink, and ClubLog. A real bug-hunt pass found these
fields' compute-method guard trusted **any** service account platform-wide to decrypt and read every
user's credentials, rather than one specific, narrowly-scoped account -- and while investigating
whether narrowing the guard to "the one right account" was a sufficient fix, the deeper question of
whether central decryption is even the right design at all had never been explicitly settled.

Bruce's own words, verbatim, when asked which service account should be trusted: "Spend more time
establishing if the natural candidate really is it, this is a piece of data we have to handle
securely. It should not leave the user's system. It should only be decrypted and used when
appropriate. Make sure the entire design is self consistent."

This names a standing principle this codebase had no explicit, written answer for: a third-party
credential a user hands to this platform is not this platform's data to use freely -- it is held on
the user's behalf, and the platform's own custody of it should be as narrow as the real functional
need allows.

## Decision

**1. Prefer local use over central use.** Where a credential's actual purpose is to authenticate an
action that could equally be performed by something running on the user's own hardware (their own
`hams_local_relay` instance, their own client device) instead of by a central Odoo server acting on
their behalf, the local path is the correct default. Central decryption and direct use of a
third-party credential by a platform server is a deliberate exception, not the default shape, and
needs its own stated justification (e.g., a sync operation that only makes sense running on a
schedule independent of whether the user's own hardware is online).

**2. Where central decryption is genuinely necessary, scope it to exactly one named consumer.** A
credential compute-method guard must resolve a specific, real service account via
`zero_sudo.security.utils._get_service_uid(xml_id)` (this codebase's own established pattern,
already used elsewhere in `ham_logbook` itself for GDPR-export access) -- never a blanket
`is_service_account` check that trusts any service account on the platform. The specific account
must be the one, real, confirmed identity the actual consuming daemon authenticates as, verified
against the live `daemon.key.registry` (or equivalent) rather than assumed from a plausible-sounding
name.

**3. Decrypt only at the moment of legitimate use, never speculatively or ambient.** A credential
should not be decrypted as a side effect of an unrelated read, held decrypted longer than the single
operation that needs it, or decrypted on a schedule broader than the real user-facing need it serves
(e.g., a sync job should run because the user asked for a sync or a real schedule they control
requires it, not decrypt-and-poll indiscriminately).

**4. Never let a decrypted credential reach a log, error message, or any persisted store other than
its own encrypted field.** An exception path touching one of these fields must be reviewed for
whether the plaintext could leak into `_logger` output, a traceback, or any other side channel.

**5. The full data flow must be traced and stated, not assumed, before scoping a guard.** When
narrowing which account/process may decrypt a given credential, the actual end-to-end flow --
where it's entered, where it's decrypted, and exactly what happens to the plaintext afterward --
must be confirmed against the real running system (the live database's own registry rows, the real
consuming daemon's own code) before the guard is written. A guard scoped to a plausible-sounding
account name, never confirmed against the real system, is not a fix.

## Consequences
Every current and future third-party credential field on `res.users` (or any other model) is
measured against this ADR: is central decryption actually necessary for this credential's real
purpose, is the guard scoped to one confirmed real consumer, and does the plaintext ever travel
further than that one legitimate use requires. `ham_logbook`'s six fields are the first audit
subject under this ADR -- their own end-to-end trace and remediation is tracked in
`night_shift_todo.md`, not restated here, since this ADR states the standing policy independent of
any one field's current compliance status. A future credential-handling feature that centralizes
decryption/use without checking against principle 1 first, or that widens a guard back to a blanket
service-account check for convenience, is a regression against this ADR, not a acceptable shortcut.
