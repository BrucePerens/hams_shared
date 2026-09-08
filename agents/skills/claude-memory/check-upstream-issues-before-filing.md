---
name: check-upstream-issues-before-filing
description: "Before proposing to file a bug upstream on an open-source project, search that project's existing issues/branches first to confirm it isn't already known or fixed."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 58453db6-5ee8-4667-a749-b2d8dad1938a
  modified: 2026-08-24T20:55:03.142Z
---

While working on hams.com/hams_open, Claude found what looked like a novel build bug in a third-party dependency (`pflarue/ardop`'s `ardopcf`: GCC 14 turns a pointer-to-int argument mismatch in `lib/rawhid/rawhid.c` into a hard compile error) and proposed filing it upstream as a new GitHub issue. When the user was asked whether to go ahead, they said: "I am loath to give an Open Source project an AI-originated bug without getting deeper into it. Explain further," and then asked Claude to check whether it had already come up. A search of the repo's existing issues turned up two closed issues (#124 and #162) reporting the exact same error text — the fix had already landed on the project's `develop` branch over a year earlier, but had never been merged to `master`, which is what a plain `git clone` checks out by default. The "bug" was real, but it was neither new nor something that needed a fresh report — at most a courtesy comment noting `master` is stale relative to `develop`. The user's reaction: "Glad I didn't naively act like I discovered it. Let's look first next time."

The generalized lesson: before proposing to file a bug, issue, or PR against any upstream/third-party project, first search that project's own issue tracker (and check for relevant long-lived branches like a `develop` branch that may already contain a fix) to see whether the problem is already known, already reported, or already fixed. Do this before presenting the finding to the user as something to potentially file, not just before actually filing it — the user should never be put in the position of deciding whether to submit something that turns out to already be common knowledge to the maintainers. This matters especially for AI-originated findings: presenting an already-known issue as a fresh discovery risks looking uninformed or wasting a maintainer's time, and the user is rightly cautious about that risk before anything with their name or an AI's name goes out to a public project.
