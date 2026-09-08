---
name: hams-gap-tracking-and-autonomous-closure
description: "On hams.com/hams_open, when Claude finds or is asked to survey for unfinished features/gaps, keep a durable written record of them and work through closing them autonomously, in whatever order Claude judges best, rather than asking the user to prioritize via multiple choice."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 8634d779-d53d-4761-a994-a128e6aa0c52
  modified: 2026-08-23T14:18:13.652Z
---

When Claude surveys the hams.com codebase (hams_com, hams_open, hams_shared) for unfinished
features, stubs, mocks, or other gaps -- whether asked to do so directly or discovering them
incidentally during other work -- the user wants two standing behaviors, stated explicitly after
Claude tried to route a batch of "which of these should I do, and in what order" decisions back to
the user via a multi-question prompt:

1. **Keep a written record.** Maintain a durable, continuing record of gaps found (this
   session's practice has been `/home/bruce/workspace/tmp/night_shift_todo.md`, which already
   serves this role for in-progress/resume state -- the gap survey and its findings belong in the
   same durable-record tradition, not just in a one-off chat reply or artifact that isn't tracked
   going forward).
2. **Pursue them autonomously.** Work through closing the gaps in whatever order Claude judges
   best, without stopping to ask the user to rank or choose among them first, with the goal of
   eventually closing all of them. The user's own words: "pursue them in the order you please,
   eventually closing all of them."

This does not override the standing "flag real judgment calls for the user" principle for gaps
that are genuinely blocked on a resource only the user has (credentials, a primary source to
verify against, a hardware decision) or a product/architecture call with no clear default -- those
still get flagged precisely, per this session's own established practice. What this changes is the
*scheduling* question: don't ask the user to sequence or triage a list of already-identified gaps
via a multiple-choice prompt. Decide the order and proceed.
