# ADR 0087: Third-Party Dependency Version Tracking

## Status
Accepted

## Scope
This ADR governs how this codebase (`hams_com`, `hams_open`, `hams_shared`) tracks its exposure
to code it does not control the version of: anything vendored by a framework we depend on
(Odoo), anything copied directly into our own repo from an external project, and any external OS
binary a daemon shells out to but doesn't build. It does not govern dependencies we pin and
update ourselves on our own schedule (`Cargo.lock`, `requirements.txt`) -- those already go
through ordinary code review every time they change, because updating them is our own deliberate
action, not something that happens to us.

## Context
`fa-sign-in-alt` (FontAwesome 5+) was shipped in a login banner against an Odoo install that
vendors FontAwesome 4.7.0 -- the icon silently rendered as nothing, and nothing in the test suite
caught it, because no tour asserts a specific icon glyph is present. The mismatch was only found
by grepping the vendored CSS file directly during an unrelated review. Investigating it surfaced
that this is not a one-off risk but a whole category this codebase had no standing answer for:
code we build against, but whose version is decided by someone else's release schedule, with no
mechanism to tell us when it moves.

That category splits into three distinct shapes, each needing a different answer:

1. **Vendored by a framework we depend on, version-bumped silently on every framework upgrade.**
   Example: Odoo's own `web/static/lib/` (FontAwesome, Bootstrap, OWL, jQuery, Luxon, Chart.js,
   PDF.js, and others) and the Python libraries pulled in as `Depends:` of the `odoo` Debian
   package itself (`lxml`, `psycopg2`, `cryptography`, `reportlab`, and others). An
   `apt-get upgrade odoo` can change any of these without touching a single file in our own repo.
   The risk here is **drift**: our code silently starts assuming a version that's no longer true.

2. **Vendored by copy directly into our own repo, with no update mechanism at all.**
   Example: `ham_satellite/static/src/lib/{three.min.js, OrbitControls.js, satellite.min.js}`,
   `ham_shack/static/src/lib/{countries-110m.json, us-states.json}`. These don't drift underneath
   us -- the opposite risk: they're frozen forever, silently, until a human remembers to
   re-vendor them. There is no version for anything to check against; the risk is **staleness**
   (missing security fixes, missing data updates) with nothing prompting anyone to notice.

3. **External OS binaries a daemon shells out to but doesn't build.** Example: `tqsl`,
   `pat-winlink`, `direwolf`, `hamlib`/`rigctld`, Playwright's bundled Chromium, `socat`. An apt
   upgrade of any of these can change CLI flags, output format, or binary name/path out from under
   the code that shells out to it. This already happened for real, independent of this ADR:
   `find_pat_binary()` only ever searched for a binary literally named `pat`, never Debian's
   actual `pat-winlink` name, so it could never have found a real Debian install of `pat`, ever
   (see `WINLINK_RELAY_DAEMON_INTEGRATION.md`).

## Decision

1. **Category 1 (framework-vendored, silent drift): a narrow, incrementally-grown startup
   version-check that warns on mismatch, never hard-fails.** Only version-check something once a
   real dependency on its exact version/behavior has actually been confirmed by name in our own
   code -- not a blanket audit of everything Odoo happens to vendor. Each entry records what we
   depend on, the version last verified against, and the file/line in our own code that depends
   on it. On mismatch at startup, log a `WARNING` naming exactly what drifted and pointing at
   this ADR; per this codebase's no-hard-bricking philosophy, a version check must never be the
   reason a working install stops working. Tracking table and implementation plan:
   `hams_com/docs/proposals/ODOO_VENDORED_DEPENDENCY_VERSION_CHECK.md`.

2. **When a category-1 warning fires: test the specific feature against the new version before
   doing anything else.** Do not pin the framework backward -- we don't control that lever, and
   trying to fight it is how vendored files silently diverge from what the framework actually
   ships. If the feature still works, update the tracking table's "last verified" entry and move
   on. If it broke, fix our own code to match the new reality (the same posture as any other
   upstream break), and only then update the table.

3. **Category 2 (vendored-by-copy, silent staleness): a "last vendored" marker, checked on a
   calendar, not a version.** Since there is nothing to diff against automatically, the check is
   a periodic manual one: each vendored-by-copy asset gets a recorded vendor date (and upstream
   source URL) in a checked-in manifest, and re-vendoring is scheduled the same way other
   recurring maintenance in this codebase is -- not built as part of this ADR, but the tracking
   table format belongs in the same proposal doc referenced above so both categories are visible
   in one place.

4. **Category 3 (external OS binaries): a startup capability probe per binary, following the
   pattern this codebase already uses.** `probe_pat_version()` and the corrected
   `find_pat_binary()` are the existing model -- confirm the binary is actually found under every
   name/path a real target distro might install it as, and confirm its documented flags/output
   still match what our code assumes, logging a warning (never a hard failure) on mismatch. New
   daemon code that shells out to an external binary for the first time should follow this same
   pattern rather than assuming a single hardcoded binary name.

5. **Any new code that takes a hard dependency on a vendored library's specific version or exact
   behavior (an icon class name, a CLI flag, a JS API surface) must add an entry to the relevant
   tracking table as part of that change**, the same discipline `docs/proposals/*.md` already
   applies to open work -- this is what keeps the inventory from going stale itself.

## Consequences
A version mismatch in something we don't control becomes a logged warning pointing at a known
tracking table, instead of a silently broken feature discovered by manual inspection (as
happened here). The three categories stay explicitly distinct rather than collapsed into one
"dependencies" bucket, because "warn on drift" only makes sense for category 1 -- categories 2
and 3 need a calendar-based check and a capability probe, respectively, not a version comparison
against something that was never pinned in the first place. This does not replace ordinary code
review of `Cargo.lock`/`requirements.txt` changes, which already goes through review because we
control when those change.
