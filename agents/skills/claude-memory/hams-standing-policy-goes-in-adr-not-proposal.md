---
name: hams-standing-policy-goes-in-adr-not-proposal
description: "On hams.com/hams_open, a cross-cutting standing policy document belongs in hams_shared/docs/adrs/ as a new ADR, not in a hams_com/docs/proposals/*.md file."
metadata: 
  node_type: memory
  originSessionId: 58453db6-5ee8-4667-a749-b2d8dad1938a
  modified: 2026-08-24T01:39:26.714Z
---

On hams.com/hams_open/hams_shared, this codebase already has two distinct documentation conventions for different kinds of open work, and the user has corrected Claude for reaching for the wrong one. `hams_com/docs/proposals/*.md` is for a specific, scoped feature or fix that is not yet built -- each file tracks its own "Still open" section and gets updated as that particular piece of work lands. `hams_shared/docs/adrs/` (Architecture Decision Records, numbered sequentially, e.g. `0086_own_model_extension_consolidation.md`, `0087_third_party_dependency_version_tracking.md`) is for a standing, cross-cutting policy that governs how the codebase handles a whole category of situation going forward -- not a single feature, but a durable rule with a Status/Context/Decision/Consequences structure, indexed in `hams_shared/docs/adrs/README.md`.

The distinguishing signal from the user: when Claude found a real bug caused by an untracked third-party dependency version (a FontAwesome version mismatch) and filed it as a `docs/proposals/ODOO_VENDORED_DEPENDENCY_VERSION_CHECK.md` proposal, the user asked for something broader -- "We need a standing document on this topic: programs with no upgrade path to check, things vendored by external parties that we should track, what to do (just test with the new version?)." That phrasing -- "standing document," asking for a general policy and process, not just a fix for the one bug found -- is the tell that the right home is a new ADR in `hams_shared/docs/adrs/`, not another proposal doc. The proposal doc remains useful as the specific implementation-tracking piece the ADR's policy points to, but the policy itself (what categories of un-owned dependency exist, what to do about each) belongs in the ADR.

Since `hams_shared` is a git submodule shared by both `hams_com` and `hams_open`, a new ADR file needs its own commit in the submodule (plus a `README.md` index-entry addition) and then a separate submodule-pointer-bump commit in whichever parent repo(s) reference it -- the same two-step submodule commit pattern used for any other `hams_shared` change.
