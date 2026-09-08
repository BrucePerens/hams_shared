---
name: hams-autonomous-session-conventions
description: "How the user wants long, unsupervised work sessions on hams.com/hams_open structured — commit cadence and what to do once the known task list runs out."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-21T14:45:18.864Z
---

When the user hands off a long, unsupervised session on the hams.com/hams_open codebase (e.g. leaving for the day or going to bed with several hours before they're back), two standing preferences apply, confirmed across multiple such sessions:

Commit locally as verified, coherent batches of work complete, rather than leaving everything uncommitted for a single end-of-session review. Never push to a remote or take any other destructive/high-blast-radius action without asking first — that boundary doesn't change. The point of committing along the way is just to checkpoint real, test-verified progress rather than to seek any additional autonomy.

Work through the known task list (open items from prior review artifacts, previously-identified bugs, pending fixes) roughly in easiest-to-hardest order. If the known list is exhausted before the session ends, the user's explicit preference is to continue productively rather than stop: run another round of code review and test-coverage enhancement across the codebase, since that reliably exposes further real bugs (confirmed by this pattern already surfacing several distinct, previously-unknown bug classes in past sessions — service-account parameter whitelist gaps, manifest dependency cycles, hollow self-write tests, ir.rule OR-vs-AND multi-tenancy bugs). The loop is: review, fix, add coverage, review again — not a one-shot pass.

Confirmed explicitly (2026-08-21): this applies to idle time *within* a session, not just to what happens once a task list runs dry at the very end. During one overnight session the user checked in periodically with questions, small side-tasks, and design discussion; between those exchanges, whenever a known punch list of scoped, decision-made "ready to start" items existed, time spent not actively pulling the next item from that list and starting it — waiting to be re-prompted rather than picking up the next ready item on its own — was, in the user's own words, "despite instructions," not merely a missed opportunity. The autonomy grant means: the moment there is nothing actively in flight and a ready item exists, start it immediately without waiting for the user to say so again, even if the user is mid-conversation about something else entirely.

This whole protocol is codified as the `night-shift` skill at `hams_shared/agents/skills/night-shift/SKILL.md` (shared into both hams_com and hams_open via a symlink) — check there first, since it's the canonical, more durable copy of these conventions and gets updated as the process evolves. It now also includes a "Status Artifact Requirement": when the user asks for a status/open-issues artifact partway through or after such a session, producing it is itself part of the same work, not a passive report — re-verify each claimed finding against current code/tests rather than trusting prior artifacts or earlier summaries, fix whatever the re-verification turns up (including newly-exposed pre-existing bugs) under the session's normal conventions instead of just listing it as still-open, and have the new artifact supersede/consolidate prior ones from the same investigation rather than leaving several stale artifacts for the user to reconcile.
