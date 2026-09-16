---
name: night-shift-questions
description: >-
  Read and write the shared queue of open questions for Bruce at hams_com/night_shift_questions/
  -- one file per question, in open/ or answered/. Use this whenever a to-do needs a real decision
  only Bruce can make (before blocking it), when checking what's waiting for his answer, when
  recording his answer, or when unblocking a to-do whose question just got answered. Pairs with
  the night-shift-todo skill; read that one too if you haven't already. Triggers: question queue,
  ask Bruce, blocked on a decision, needs Bruce's input, unblock a to-do.
version: 1
---

# Night-Shift Questions Queue

Bruce's own instruction, 2026-09-15, describing what he wanted after `night-shift-todo` shipped:
"I'd like to automate the pipeline more. Something queues questions for me and has them always
ready. Something handles to-dos, including ones unblocked by the resolved questions." This skill is
the questions half of that; `night-shift-todo` is the to-dos half; the `night-shift-todo-worker`
scheduled task (see its own SKILL.md) is the automation that ties them together.

**The problem this solves**: a session hits something only Bruce can decide -- a security-design
tradeoff (self-service revoke vs. admin-only), a product direction, a credential/business step,
picking between two viable architectures. Historically this went one of three ways, all lossy: (1)
`AskUserQuestion` right then, which only works if Bruce is actually at the terminal that moment;
(2) writing a paragraph into `night_shift_todo.md` and hoping a future session or Bruce notices it
buried in prose; (3) the session just picks an answer itself and moves on, which is exactly what
`hams-dont-be-too-cautious-about-originating-work` says NOT to do for a genuine judgment call. This
queue is the fourth, durable option: file the question, keep working everything that ISN'T blocked
on it, and let Bruce answer on his own schedule from a queue that's always current.

## Where it lives

`/home/bruce/workspace/hams_com/night_shift_questions/`, with two subdirectories:

```
night_shift_questions/
  open/       # not yet answered
  answered/   # answered -- kept for the record and for automatic to-do unblocking
```

A question's state is which directory it's in -- not a field to parse. `ls
night_shift_questions/open/` alone tells Bruce (or any session) exactly what's waiting, the same
directory-encodes-state discipline `night-shift-todo` already uses for priority.

## File format

One file per question, named `<slug>-<8-hex-chars>.md` (same random-suffix convention as
`night-shift-todo`, for the same reason -- a shared working tree, collision avoidance). Frontmatter,
then free-form body:

```markdown
---
status: open              # open | answered
asked_by:                 # session name (ListAgents' own "this session is..." line)
blocks:                   # relative paths of night_shift_todo/ files waiting on this, if any --
                           # empty list is fine for a standalone question nothing is blocked on
  - night_shift_todo/medium/some-item-a3f19c02.md
created_at: 2026-09-15
answered_at:               # filled in when moved to answered/
---

## Question

State the actual question so a cold reader (Bruce, days later, no memory of the session that
asked) can answer it without re-reading anything else. Include enough context to be self-contained:
what triggered this, what's already been investigated, why it's a real decision and not something
the session could reasonably default on its own (see night-shift-todo's own escalation criteria --
this is the same bar).

## Options considered

(if there are genuinely distinct options, lay them out with their real tradeoffs -- the same
quality bar as an AskUserQuestion prompt. Skip this section for an open-ended question with no
natural option set.)

## Answer

(blank until Bruce answers -- filled in with his actual words/decision when the question moves to
answered/)
```

Write the question with the same care as an `AskUserQuestion` call -- this is functionally an
async version of that tool. A vague "what should we do about X?" with no context is not a filed
question, it's a stalled to-do wearing a question's clothes.

## Workflow

**Filing a question** (a to-do or an in-progress task hits a real judgment call): create the file
under `open/` with `status: open`. If a specific to-do is what's actually blocked, add its path to
`blocks:` AND edit that to-do file itself: `status: blocked`, add a `blocked_on: <this question's
path>` field. If nothing is blocked yet (a standalone observation worth Bruce's eventual input,
not currently stopping any specific piece of work), leave `blocks:` empty -- filing it here still
beats letting it evaporate at the end of a session. Commit and push (scoped pathspec, per the
shared-working-tree discipline both skills document) as its own commit, not folded into unrelated
work.

**Bruce answering** (from any device, any time): edit the question file's `## Answer` section with
the real decision, set `status: answered` and `answered_at:`, then `git mv` it from `open/` to
`answered/`. If Bruce answers in chat instead of editing the file directly, the session he's
talking to does this on his behalf immediately -- his answer isn't durably recorded until it's in
the file, the same "chat alone is not durable" principle `hams-durable-tracking-over-chips` already
establishes for to-dos.

**Unblocking to-dos once a question is answered**: read the answered question's own `blocks:` list.
For each path listed, open that `night_shift_todo/` file and: change `status: blocked` back to
`status: open`, clear `claimed_by:` unless that session is still running (see below), remove (or
comment out, keeping history) the `blocked_on:` field, and append a note
to the to-do's own body pointing at the answered question's path and summarizing the decision, so
whoever next works the to-do doesn't have to open a second file to know what changed. This is
exactly what `night-shift-todo-worker` does automatically every run (see its own SKILL.md) -- but
any session noticing an answered question with a live `blocks:` entry should do this immediately
rather than waiting for the next scheduled run, the same "don't leave your own past entries stale"
principle `dependabot-ci-watch` was corrected to follow for `night_shift_todo.md`.


**Clear `claimed_by:` when you unblock, or you hand the queue an ambiguous item.** An item usually
reaches `status: blocked` from `status: claimed` -- a session started it, hit a real decision, and
filed a question -- so its `claimed_by:` is still populated while it waits. Flipping only the status
back to `open` leaves `status: open` with `claimed_by: <a session that ended weeks ago>`, and
`night-shift-todo-worker`'s Step 2 selects "open, unclaimed" items: a worker reading `status` takes
it, a worker reading `claimed_by` skips it forever, and neither is wrong about what it read. Found
2026-09-16 while verifying the fix to a related gap (see `night-shift-todo`'s own "A status outside
the four defined values escapes every query"); same shape, one step further along the same path.

So clear the field as part of the same edit. The exception is a live one: if the `claimed_by` session
is still in `ListAgents`, leave it and message that session instead -- its answer just arrived and it
may well want to resume its own item, which is the whole point of having filed the question.

**A question with no answer for a long time**: that's fine and expected -- this queue exists
precisely so nothing is lost or re-asked while Bruce is away. Don't re-file a duplicate of an
already-open question; a quick `grep -rl` across `open/` for the same topic before filing a new one
costs nothing.

## What belongs here vs. what doesn't

- **A real judgment call**: a design tradeoff with no clearly-correct default, a product/business
  decision, something needing a credential or account only Bruce has, a security posture choice
  with real consequences either way. This is the same bar `night-shift-todo`'s own "claim before
  you start" workflow and this codebase's standing `hams-dont-be-too-cautious-about-originating-
  work` memory already draw between "originate it yourself" and "this needs his input."
- **NOT here**: anything with a reasonable default already stated somewhere (a proposal doc's own
  "smaller, safer first build," an existing ADR, a precedent elsewhere in the codebase) -- build
  against the default per that same standing memory, don't manufacture a question to avoid
  deciding. NOT a routine to-do with no open question at all (that's `night-shift-todo`, not this).
  NOT something `docs/BRUCE_ACTION_ITEMS.md` already covers well (a pure external action -- create
  an account, click a button, log into a website -- with no engineering decision attached); this
  queue is for decisions, that file is for outside-the-codebase actions Bruce alone can perform.

## An action-shaped blocker still belongs here when its result selects a design

The "what belongs here vs. what doesn't" section sends a pure external action to
`docs/BRUCE_ACTION_ITEMS.md`, and that is right for "create an account" or "click a button in a
vendor's console." But it is easy to over-apply, and the cost of getting it wrong is silent.

The case, 2026-09-15: a to-do was blocked on measuring whether a browser certificate exception
granted in one tab applies to a different origin's later `fetch()`. That is an action — someone has
to open a browser — so it looks like it belongs in the action-items file. It does not, because the
*result* selects between two very different builds (a modest `localStorage`/`window.open` mechanism
versus a whole local certificate authority), and building the smaller one speculatively would waste
the entire effort if the answer came back the other way. That is a decision with real tradeoffs,
reached through a measurement.

The mechanical reason this matters more than the taxonomy: `docs/BRUCE_ACTION_ITEMS.md` has no
`blocks:` list. Nothing sweeps it, so nothing it records can ever flip a to-do back to `open`. Route
a blocker there and the to-do waits forever even after Bruce does the thing. **Ask which file can
unblock the to-do, not which file the blocker superficially resembles.** When an action's output is
an input to a decision, file the question here and put the recipe for performing the action inside
it — Bruce gets both in one place, and the sweep can reach the to-do afterwards.

## A prose `blocked_on:` is unreachable, and `status: open` with one is worse

Two real items found in the same pass, 2026-09-15, both from sessions that recorded a genuine
blocker in the most natural way — as a sentence.

`blocked_on:` is read by the answered-question sweep, which walks each answered question's own
`blocks:` list. A blocker that no question file names can never unblock anything, so the item stays
blocked no matter what Bruce decides. `night-shift-todo`'s own "A status outside the four defined
values escapes every query" names the same shape one step earlier in the path.

The second item was the more dangerous of the two: `status: open` carrying a prose `blocked_on:`.
That reads as available work to every session scanning the queue, so it gets claimed, and only then
does its body reveal the real fix is gated on someone else's unanswered question — wasting a claim
and, on a shared tree, possibly a verification run too. `open` and `blocked_on:` contradict each
other; if a `blocked_on:` is real, the status is `blocked`.

When fixing one of these, keep the prose. It is usually the most precise statement of the blocker
anyone has written, and it is what the question file should be written from. Move it into the item's
body as a quote rather than deleting it with the frontmatter field.

One question can name several to-dos in its `blocks:` list, and should when they share a fix — the
two above were two halves of one unanswered fact, and they now come back to `open` together.

## Self-improvement

Same convention as `night-shift-todo`/`dependabot-ci-watch`: if a run discovers a new, reusable
fact about actually using this queue, add it directly to this file as part of that run's own
commit.
