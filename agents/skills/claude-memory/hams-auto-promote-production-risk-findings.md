---
name: hams-auto-promote-production-risk-findings
description: "On hams.com/hams_open, any finding that might affect real production behavior gets promoted to high priority in the to-do list automatically -- Claude doesn't need to ask, and shouldn't drop the current task to chase it unless that task is trivial."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-08T17:38:36.000Z
---

During a 2026-09-08 night-shift session, Claude found a real bug (the `shack_sw.js` service worker
losing the controller race to the sitewide `sw.js` on repeat visits to `/shack`) and, after the
user asked once to raise it to high priority, the user generalized this into a standing rule,
verbatim:

> "Let's make that a rule. Whenever you run into an issue that might manifest in production, it
> gets promoted to high-priority and you continue with the present task unless the present task is
> trivial."

The rule has two parts, both standing:

1. **Auto-promote, don't ask.** Any finding discovered during other work that could plausibly
   affect real users in production -- not just a test artifact, not just a linter nit, not
   something confirmed test-only/environment-only -- gets written into the relevant to-do list
   (currently `night_shift_todo.md`) as explicitly HIGH PRIORITY the moment it's found, without
   waiting for the user to ask. This mirrors how the `shack_sw.js` finding was framed once flagged:
   a short "why this is high priority, not just a test nit" explanation of the concrete production
   impact, not just a severity label.
2. **Don't context-switch onto it automatically.** Discovering and logging the finding as high
   priority is not the same as dropping the current task to go fix it immediately. Keep working the
   task already in progress. The one exception: if the current task is itself trivial (near
   finished, or a small enough remaining step that finishing it costs almost nothing), it's fine to
   wrap that up first and then pivot to the newly-promoted high-priority item sooner rather than
   letting it sit.

This complements, rather than duplicates, two existing memories: `hams-gap-tracking-and-autonomous-closure`
(keep a durable record of gaps and work through them autonomously) and
`keep-working-until-actually-done` (don't stop turns while open work remains). This one is
specifically about triage priority and focus discipline the moment a NEW production-risk finding
surfaces mid-task, not about the overall work loop.
