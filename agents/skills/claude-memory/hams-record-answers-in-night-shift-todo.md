---
name: hams-record-answers-in-night-shift-todo
description: "On hams.com/hams_open, record the user's answers to proposal-blocker questions in night_shift_todo.md immediately, not only in the individual proposal doc."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 5ef88eba-a768-4870-9ddb-1f49c09220f5
  modified: 2026-09-01T17:42:13.895Z
---

On hams.com/hams_open, `hams_com/night_shift_todo.md` is the durable, always-consulted index for
this project across sessions -- individual `docs/proposals/*.md` files are not reliably re-read or
cross-referenced by a future session picking up the backlog. During a 2026-09-01 session, Claude
asked the user (Bruce) several proposal-blocking questions via `AskUserQuestion`, updated only the
relevant proposal docs with the answers, and moved on. Bruce then pointed out that some of those
same questions had already been asked and answered earlier in the same session (before a context
compaction), but the earlier answers had been lost -- never written anywhere durable -- so Claude
re-asked them from scratch. His exact instruction: "Save the answers in night_shift_todo.md,
because you actually asked some of these questions last night, and then forgot them."

The lesson: on this project, whenever the user answers a question that unblocks a proposal or
other backlog item -- whether asked via `AskUserQuestion` or in plain conversation -- write the
question and the user's answer into `night_shift_todo.md` immediately, as its own durably-dated
entry, in addition to (not instead of) updating the specific proposal doc the decision belongs to.
Do this before moving on to further work, not as a later cleanup pass. The individual proposal doc
is the right place for the decision's technical detail and its effect on that proposal's own scope,
but `night_shift_todo.md` is the project's actual memory across session boundaries and context
compactions -- a decision that lives only in a proposal doc, or only in conversation, has already
been shown to get asked twice and lost once on this project.

**Second half of the lesson, found the same session, immediately after the first correction was
made:** the fix above is necessary but not sufficient -- Claude went on to ask a *second* round of
proposal-blocker questions and duplicated two more that had already been answered (and one of them,
a public member profile page, had already been fully built and committed) purely because a survey
of `docs/proposals/*.md` trusted each file's own status header instead of checking ground truth.
Before asking the user ANY proposal-blocker question, or presenting a proposal survey as current
status: (1) grep `night_shift_todo.md` itself for the topic/filename -- its "OPEN QUESTIONS FOR
BRUCE" / "Live Q&A" / handoff-index sections are the real record of what's already been asked and
answered, and a proposal doc's own header can go stale even after `night_shift_todo.md` recorded
the true outcome; (2) check the actual code or `git log -- <relevant path>` for whether the feature
already exists, since a doc can simply never get updated when the work it describes lands. A
proposal doc's title and header are a claim to verify, not a source of truth, exactly the same
discipline this project already applies to code claims.
