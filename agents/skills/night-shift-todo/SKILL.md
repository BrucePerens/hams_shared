---
name: night-shift-todo
description: >-
  Read and write the shared, structured to-do queue at hams_com/night_shift_todo/ -- one file per
  item, organized into critical/high/medium/low priority directories. Use this whenever checking
  for open work across hams_com/hams_open/hams_shared, filing a new to-do another session should
  pick up, claiming an item before starting on it, reprioritizing one, or marking one done. Also
  covers how this relates to night_shift_todo.md (now historical) and night_shift_history.md
  (where completed items get summarized). Triggers: to-do, night shift todo, open items, what's
  left, claim a task, file a task, priority queue.
version: 1
---

# Night-Shift To-Do Queue

Bruce's own instruction, 2026-09-15, after a session watched multiple independent hourly
`dependabot-and-ci-watch` runs coordinate real work all evening: `night_shift_todo.md` had grown
past 8,900 lines of free-form prose, genuinely hard for any session to scan for "what's actually
still open" without a full read-through -- exactly the failure mode that once caused a claimed
"done" backlog to turn out to have 20 unsorted files still sitting in it. His fix, given directly:
"put each to-do in a separate file and make it a directory... make priority directories: critical,
high, medium, low." This skill is the resulting convention.

## Where it lives

`/home/bruce/workspace/hams_com/night_shift_todo/`, with four subdirectories:

```
night_shift_todo/
  critical/
  high/
  medium/
  low/
```

A to-do's priority is which directory its file sits in -- not a field you have to parse. `ls
night_shift_todo/critical/` alone tells you the most urgent open work, no file contents needed.
Reprioritizing an item is a `git mv` between directories, which keeps the change visible in git
history the same way a normal code change would be, rather than buried in a diff.

A short `README.md` lives in `night_shift_todo/` itself for anyone who lands there directly without
having loaded this skill first -- keep it in sync with this file if the convention changes, but this
skill is the authoritative, fuller version.

## File format

One file per to-do, named `<slug>-<8-hex-chars>.md` -- e.g. `redis-6379-port-conflict-a3f19c02.md`.
The random 8-hex suffix (`printf '%08x' $RANDOM$RANDOM` or similar; doesn't need to be
cryptographically meaningful, just short and unlikely to collide) exists because this is a shared
working tree multiple sessions write to concurrently -- two sessions picking a similar slug at the
same moment must not silently overwrite each other's new file.

Frontmatter, then free-form body:

```markdown
---
status: open        # open | claimed | done
claimed_by:          # session name (e.g. "workspace-87"), blank until claimed
repo: hams_com       # hams_com | hams_open | hams_shared | cross-repo
category: ci         # ci, dependency, security, feature, patent, infra, etc. -- free text, pick
                     # something a future `grep -l "category: ci"` would find useful
created_at: 2026-09-15
---

Free-form body: what the to-do actually is, why it matters, any investigation already done,
relevant commit hashes, links to proposals. Write it the same way you'd write a
night_shift_todo.md entry -- this replaces that habit, not the writing quality.
```

## Workflow

**Checking for open work**: read `critical/` first, then `high/`, `medium/`, `low/` -- `ls` each
directory, then read files with `status: open` (skip ones already `status: claimed` by someone
else, unless investigating whether a claim is stale -- see below).

**Filing a new to-do** (something you found but aren't fixing yourself, or a real follow-up too
large for the current session): create the file directly under the right priority directory with
`status: open`, commit with a pathspec scoped to just that new file, and push. Don't batch multiple
new to-dos into one commit unless they're genuinely part of the same finding.

**Claiming one before you start work**: edit `status: open` -> `status: claimed` and fill in
`claimed_by:` with your own session name (from `ListAgents`'s own "This session is ... [the name
other sessions use to message it]" line). Commit and push that claim BEFORE doing the actual work,
not after -- the whole point is that another session checking the queue a minute later sees the
claim and doesn't duplicate your effort. This is the same principle `dependabot-ci-watch` already
documents for avoiding duplicate work (real collision, 2026-09-14: two sessions independently
started an identical rust-toolchain bump because neither checked first) -- applied here as a
structural guarantee instead of relying on everyone remembering to message first.

**A claim looking stale**: if you find an item `status: claimed` by a session that (per `ListAgents`)
no longer appears to be running, or the `claimed_by` session hasn't touched it in a clearly long
time, it's fine to pick it up -- but say so in the file's own body (append a line noting the prior
claim looked abandoned) rather than silently overwriting the claim history.

**What "done" means**: an item is only complete once the fix is tested, committed, and pushed --
all three, not just one or two. "I fixed it locally" isn't done. "I committed it" isn't done. Even
"I pushed it" isn't done without a real test run backing the claim (per this project's own "verify,
don't assume" discipline throughout `dependabot-ci-watch` and elsewhere). If a fix is committed but
genuinely can't be pushed yet (a real, named blocker -- not just "I'll get to it"), or is pushed but
CI hasn't confirmed it yet, the item stays `status: claimed`, not `status: done`, and its body says
exactly what's still missing (e.g. "committed as `<hash>`, not yet pushed: needs `workflow` scope"
or "pushed as `<hash>`, CI run `<id>` still queued"). Only move it to `night_shift_history.md` and
remove the file once all three are real.

**Completing one**: fold a real summary (what was found, what was fixed, commit hashes, test
results -- the same standard `night_shift_todo.md` entries already held) into
`night_shift_history.md` (append, matching that file's own existing convention), then remove the
to-do's own file from `night_shift_todo/` (`git rm` it). The queue directory should always reflect
only genuinely open work -- a completed item lingering there as `status: done` defeats the point of
being able to trust a bare `ls`.

**Coordination discipline**: this is the same shared working tree `dependabot-ci-watch` already
documents real collisions on (a duplicate-effort rust bump, a shared-index commit mishap). Before
committing to claim or complete an item, a quick `ListAgents` costs nothing. Before `git add`/`git
commit`, check `git diff --cached` or use a scoped pathspec -- don't trust that everything currently
staged is yours on a box other sessions are actively writing to.

## Relationship to other systems -- don't conflate these

- **`night_shift_todo.md` is now historical.** It holds the full record up to 2026-09-15 and stays
  as reference, but new to-dos go into this directory, not appended to that file. It carries a note
  at its own top saying so.
- **`night_shift_history.md`** is unchanged -- still the destination for completed-work summaries,
  from this queue same as before.
- **The `todo-list` skill** (`hams_shared/agents/skills/todo-list/SKILL.md`) is a different,
  smaller thing: a curated, checkbox-based list of project-level feature priorities, rewritten in
  place as a single file. It is not this queue and this skill doesn't change it.
- **`bug-hunt`'s own claims files** (`docs/bug_hunt_claims/`) are a separate, specialized system for
  that skill's own security-finding lifecycle (freshness tracking, second-round review, etc.) with
  requirements this general queue doesn't need to replicate. Leave them separate unless Bruce asks
  for them to be merged.
- **`night-shift`** (`hams_shared/agents/skills/night-shift/SKILL.md`) is an autonomy-MODE skill
  (aggressive unattended execution directives), unrelated to to-do storage mechanics despite the
  similar name.

## Self-improvement

Same convention as `dependabot-ci-watch`: if a run discovers a new, reusable fact about actually
using this queue (a naming collision that happened despite the hash suffix, a claim-staleness
judgment call that came up, a category taxonomy that turned out to need a new value), add it
directly to this file as part of that run's own commit.
