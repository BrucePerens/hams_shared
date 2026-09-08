---
name: prefer-routing-over-exemption
description: Bruce prefers routing a requirement to the audience-appropriate place over exempting a category from it outright.
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-04T17:11:30.327Z
---

While designing how hams.com/hams_open's `verify_anchors.py` should handle a "test every function"
mandate that would otherwise force documenting purely internal/infrastructure functions the same
way as user-facing features, Claude built a new `INFRA_` anchor-name prefix that *exempted*
infrastructure functions from the documentation requirement entirely (still requiring a test link,
but waiving the doc-coverage check outright). Bruce corrected this directly: "I think the
difference is where the documentation is located. If we document one function that is user visible
in a user-visible document, and we document an infrastructure function in the README.md for the
module, we've still documented both." His preference was for infrastructure functions to be
documented in the module's own `README.md` and user-visible functions in user-facing docs --
*both* categories stay fully documented, just in different places suited to their different
audiences, rather than one category being waived from the requirement.

Checking directly (not assuming) confirmed the existing tool already supported this: citing an
anchor in a module's `README.md` already satisfied its documentation-coverage check via an
existing "contract" location mechanism, so no new exemption class was actually needed once the
right routing was used -- the `INFRA_` exemption was reverted.

The generalizable lesson for future sessions: when a category of code/content seems like it
shouldn't have to meet the same bar as another category (e.g., "infrastructure code doesn't need
user-level docs," or a similar-shaped argument elsewhere), the first design instinct should be to
ask whether the requirement can be *routed* to an audience-appropriate destination rather than
*waived* for that category -- and to check whether the existing system already has a place for
that routing before inventing a new exemption mechanism. Reach for an outright exemption only when
routing genuinely isn't possible, not as the default first move.
