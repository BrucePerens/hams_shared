---
name: night-shift-todo
description: >-
  Read and write the shared, structured to-do queue at hams_com/night_shift_todo/ -- one file per
  item, organized into critical/high/medium/low priority directories. Use this whenever checking
  for open work across hams_com/hams_open/hams_shared, filing a new to-do another session should
  pick up, claiming an item before starting on it, reprioritizing one, blocking one on a real
  question for Bruce (see night-shift-questions), or marking one done. Also covers how this
  relates to night_shift_todo.md (now historical) and night_shift_history.md (where completed
  items get summarized). Triggers: to-do, night shift todo, open items, what's left, claim a task,
  file a task, priority queue, blocked on a question.
version: 2
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

Each priority directory holds an empty `.gitkeep`. Git doesn't track empty directories, so without
it, removing the last to-do in a priority deleted the directory itself. That happened to `critical/`
on 2026-09-16, and `ls night_shift_todo/critical/` then errored for every session. Don't remove
the `.gitkeep` files, and don't count them as to-dos.

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
status: open        # open | claimed | blocked | done
claimed_by:          # session name (e.g. "workspace-87"), blank until claimed
blocked_on:          # relative path to a night_shift_questions/open/*.md file, only when
                     # status: blocked -- see "Blocked on a real question" below
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
else, unless investigating whether a claim is stale -- see below; skip `status: blocked` items too
unless you're specifically checking whether their blocking question was just answered).

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

**`claimed_by:` is not unique over time -- session names get REUSED.** On 2026-09-16 a
`night-shift-todo-worker` run came up as `workspace-05` and found that name already written into
this very skill file and into an open to-do's body, describing a *different*, long-ended session
from the previous day. So "claimed by workspace-NN, and `ListAgents` shows a live workspace-NN"
does not establish that the live session is the one that made the claim -- it may be a new session
that happened to be handed the same name. `ListAgents` prints a short id alongside each name
(`workspace-05 [ebda75]`); record that id in `claimed_by:` as well when claiming, and when judging
whether a claim is stale, compare the id rather than the name. Where only a bare name is recorded
(every claim written before this convention), treat a name match as suggestive, not conclusive, and
send one `SendMessage` to ask rather than either taking the item or leaving it indefinitely.

**A claim looking stale**: if you find an item `status: claimed` by a session that (per `ListAgents`)
no longer appears to be running, or the `claimed_by` session hasn't touched it in a clearly long
time, it's fine to pick it up -- but say so in the file's own body (append a line noting the prior
claim looked abandoned) rather than silently overwriting the claim history.

**Blocked on a real question**: if working an item (claimed or not yet claimed) surfaces a genuine
judgment call only Bruce can make -- not something you should default on your own, see
`night-shift-questions`'s own "what belongs here" section for the bar -- don't stall silently and
don't guess. File the question in `night_shift_questions/open/` (that skill has the full format),
set this item's own `status: blocked` and `blocked_on:` to the new question file's path, and move
on to other unblocked work. Do NOT set `status: done` or delete the to-do while it's blocked, even
if you've made real partial progress -- record the progress in the body and leave it blocked.
`night-shift-todo-worker` (the scheduled task) checks `night_shift_questions/answered/` every run
and automatically flips a blocked item back to `status: open` once its question is answered,
appending a note on what was decided -- but any session noticing an answered question first should
do the same unblocking immediately rather than waiting for the next scheduled run.

**What "done" means**: an item is only complete once the fix is tested, committed, and pushed --
all three, not just one or two. "I fixed it locally" isn't done. "I committed it" isn't done. Even
"I pushed it" isn't done without a real test run backing the claim (per this project's own "verify,
don't assume" discipline throughout `dependabot-ci-watch` and elsewhere). If a fix is committed but
genuinely can't be pushed yet (a real, named blocker -- not just "I'll get to it"), or is pushed but
CI hasn't confirmed it yet, the item stays `status: claimed`, not `status: done`, and its body says
exactly what's still missing (e.g. "committed as `<hash>`, not yet pushed: needs `workflow` scope"
or "pushed as `<hash>`, CI run `<id>` still queued"). Only move it to `night_shift_history.md` and
remove the file once all three are real.

**A verification run shows the fix depends on another to-do**: this happens (2026-09-15: the GDPR
export's ADIF-queue grant couldn't pass until every other installed module in the same `super()`
chain had its grant too, because `hams_test` has every module installed). Claim the blocking to-do,
fold it into the same work, and note in both files why. Don't close the first item as done on its
own narrow test, and don't leave it waiting on an unclaimed sibling. A peer's claimed item is the
exception: coordinate with that session instead.

**A verification run fails in tests you didn't touch**: before committing, confirm each failure is
unrelated (read its traceback) and already tracked (`grep -rl <test name> night_shift_todo`). File a
to-do for any that isn't, after checking whether its owning session already filed one. Name the
tracking to-dos in your commit or history entry so a later reader doesn't re-investigate them.

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
- **`night-shift-questions`** (`hams_shared/agents/skills/night-shift-questions/SKILL.md`) is the
  paired queue for questions only Bruce can answer -- a `status: blocked` item here points at a
  file there via `blocked_on:`. Read that skill for the question-file format and the unblocking
  workflow; this file only documents the to-do side of the link.
- **`night-shift-todo-worker`** (a scheduled task, `~/.claude/scheduled-tasks/night-shift-todo-worker/`)
  is the hourly automation that reads both queues: unblocks `night_shift_todo/` items whose
  question just got answered, and works through open, unblocked items itself. See its own SKILL.md
  for what it will and won't do unattended.

## Waiting for the shared Odoo test runner

The dev box runs one `hams_shared/tools/test.py` at a time, and other sessions hold that lock for
long stretches. On 2026-09-16 `night-shift-todo-worker` queued a run behind them with
`while pgrep -f "tools/test.py"; do sleep 3; done; test.py ...`. That loop can never exit:
`pgrep -f` matches the loop's own `bash -c` command line, which contains the same string. It sat
for over an hour without launching, until a peer session noticed. Anchor the pattern to the real
process instead: `pgrep -u odoo -f "^python3 hams_shared/tools/test.py"`. The same trap applies to
any `pgrep -f` that waits on a command whose name also appears in the waiting script. To check
that a queued run actually started, look for its log file, not for the waiter process.

**A waiter can lose the race.** Several sessions often queue behind the same run, and whichever
polls first after it ends gets the lock. On 2026-09-15 a 20-second `sleep` lost to another
session's run and would have waited out a second full run. Poll every few seconds, and wrap the
launch in a retry: if the console shows `Another instance of test.py is already running`, go back
to waiting instead of treating that as your test result.

**Use the wait.** Another session's run can hold that lock for half an hour or more. Queue your own
Odoo run in the background, then work items that never need the lock. On 2026-09-15 two of
`night-shift-todo-worker`'s three items were verified without it while the third's run waited:
- `hams_shared/tools/`: `python3 -m pytest test_<name>.py` from that directory.
- `hams_com/daemons/<name>/`: `pytest` from the daemon's own directory with
  `PYTHONPATH=/home/bruce/workspace/hams_com/daemons`, because `hams_config.py` lives there.
  `pdns_sync` also needs dummy `PDNS_API_URL`/`PDNS_API_KEY`, which it reads at import.
- Lint: `check_burn_list.py --scan-daemons-and-tools <dir>`.

**Before queueing the Odoo run, expect two pre-flight halts that have nothing to do with your
change** (2026-09-15, workspace-dc). The Semantic Anchor scan fails every run on repo-wide
violations: about 170 "stacked anchors", plus missing targets under `docs/patent_disclosures/`.
The burn list fails on hams_open's backlog. Either one aborts the run before a single test starts,
after you've waited out the lock. Pass `HAMS_SKIP_ANCHOR_SCAN=1 HAMS_SKIP_BURN_LIST=1`, and cover
what they skip yourself: `check_burn_list.py <module dir>` on each touched module, and a grep of the
aborted log for your own anchor names. The log does list violations per file, so a new stacked
anchor you added (for example, an `[@ANCHOR:]` line directly followed by `Verified by`) shows up
there.

## A load gate now runs BEFORE the lock, so a busy box delays you instead of failing you

Added 2026-09-16 by `night-shift-todo-worker` (workspace-cb), closing
`night_shift_todo/medium/odoo-tours-vs-relay-ci-load-contention-4f9f03c6.md` option (a).

`test.py` now refuses to start Odoo while the box is already loaded, because a starved run does not
merely go slowly -- it produces failures that look exactly like code regressions. The recorded
evidence: three `test.py -u ham_shack` runs of identical code each failed a DIFFERENT
`TestShackSwBehaviorTour` test at the same ten-second wait step while `rustc` held about 750 percent
of the processor, and a later run near load 23 additionally failed four plain `url_open` tests on
the test client's own ten-second read timeout although the server answered 200.

Two signals, both read locally (no `gh run list`, so no network on the front of every invocation):
a live `Runner.Worker` process, which exists only while a continuous-integration job is actually
executing -- `Runner.Listener` runs permanently and means nothing -- and the one-minute load average
above the processor count. The second is what catches a peer running `cargo build` by hand, which
the runner check cannot see at all.

What this changes for a session queueing a run:

- **It waits up to 900 seconds and then exits 1**, naming what was busy (a pid, or the load figure).
  That is a different failure from the lock's "Another instance of test.py is already running", and
  it is not a reason to retry in a tight loop -- it means the box is genuinely loaded.
- **Three environment overrides**: `HAMS_SKIP_CI_LOAD_GATE=1` bypasses it entirely,
  `HAMS_CI_LOAD_GATE_TIMEOUT=<seconds>` changes the wait, and `HAMS_CI_LOAD_GATE_RATIO=<float>`
  scales the load threshold (default 1.0, i.e. the processor count). Unlike the Init Imports Linter
  below, this one deliberately HAS an escape hatch, for exactly the reason that section gives.
- **The gate runs strictly before the systemwide lock is taken**, and that ordering is load-bearing
  rather than incidental: waiting out somebody else's build matrix while holding the box-wide lock
  would stop every other session from testing for the whole wait. A source-order assertion in
  `test_test.py` fails if anyone moves the call. Process C (`HAMS_TEST_LOCK_HELD=1`) is exempt.
- `test.py --help` is exempt, so reading the usage text never waits.

So the count of pre-flight halts is now four, and this is the only one that can be a DELAY rather
than an immediate abort. If a queued run seems to be doing nothing, read its first line before
assuming it hung: `[*] Waiting for the box to quieten before starting Odoo` is this gate working.

## A third pre-flight check has NO skip flag, and it blocks every session, not just yours

The section above names two pre-flight halts to expect (the Semantic Anchor scan and the burn list)
and gives the environment variables that skip them. There is a third, and it behaves differently in
the way that matters most on a shared box: **the Init Imports Linter has no skip flag at all.**

It fails if any `.py` file in a module's `models/` (or `tests/`) directory is not imported by that
directory's `__init__.py`:

```
File 'qsl_confirmation_apply.py' in '.../ham_relay_bridge/models' is never imported in its __init__.py
```

That is one error and a hard halt, before any test runs. Two consequences follow, and the second is
the expensive one:

- It is a **repo-wide** check, so a half-written file in one module aborts a run targeting a
  completely different module. On 2026-09-16 a new, not-yet-wired helper file in `ham_relay_bridge`
  aborted a peer session's `-u ham_shack` run, and would have aborted every other session's run too
  for as long as it sat there.
- Unlike the anchor scan and the burn list, there is no `HAMS_SKIP_*` escape, so a peer cannot work
  around it. Their only options are to wait for you or to work out whose file it is.

So: **wire a new `.py` file into its `__init__.py` in the same edit that creates it**, not later at
commit time. The window between creating the file and wiring it is a window in which nobody on the
box can run a test. If the file defines no Odoo model (a plain helper module imported by the models
beside it), it still has to be listed -- say so in a comment above the import, since `models/` is
otherwise read as a list of model files.

The generalisation worth carrying: before assuming a pre-flight check is skippable because the two
documented ones are, check. And when a peer reports that their unrelated run is failing on your
file, treat it as the higher-priority interrupt -- it is costing every concurrent session, not just
the one that told you.

**Relay commits: check formatting with the PINNED toolchain, not whatever `rustfmt` is on PATH.**
An earlier version of this section said to run `rustfmt --edition 2021 --check` on the exact files
touched. That is wrong in a way that passes silently: a bare `rustfmt` is whatever comes first on
PATH, which on this box is `1.8.0-beta`, while `daemons/hams_local_relay/rust-toolchain.toml` pins
`1.98.1`, whose rustfmt is `1.9.0-stable`. rustfmt's output genuinely differs between versions, so
that check can pass locally and CI's still fail. Found 2026-09-15, after a session had already
reported "rustfmt clean" on that basis (the result happened to hold; the method did not).

`build-relay.yml` runs `cargo fmt -- --check` from inside `daemons/hams_local_relay`, where rustup
applies the directory-scoped `rust-toolchain.toml` override. Run it the same way -- from that
directory, as `cargo fmt`, so the pin takes effect. `cargo fmt --version` there should say
`1.9.0-stable`, not the PATH one; if it doesn't, the override isn't applying and nothing else you
check is meaningful.

**And clippy: CI runs `cargo clippy --release --all-targets -- -D warnings`.** `--all-targets` is a
superset of `--tests` (it lints benches and examples too), and `--release` changes which lints fire
at all, since some are opt-level-dependent. `--bin hams_local_relay --tests` is not the same check.

**The clean way to run both against what CI will actually see**, without the shared tree's
uncommitted peer edits compiling in and without fighting the shared `target/`:

```bash
mkdir -p /tmp/ci/hams_com /tmp/ci/hams_open
git -C ~/workspace/hams_com  archive origin/main daemons/hams_local_relay  | tar -x -C /tmp/ci/hams_com
git -C ~/workspace/hams_open archive origin/main daemons/ham_digital_modes | tar -x -C /tmp/ci/hams_open
cd /tmp/ci/hams_com/daemons/hams_local_relay
export CARGO_TARGET_DIR=/tmp/ci/target
cargo fmt -- --check
cargo clippy --release --all-targets -- -D warnings
```

The sibling `hams_open` extraction is what makes the checked-in relative `ham_digital_modes`
symlink resolve. The archive carries `rust-toolchain.toml`, so the pinned toolchain applies and
auto-installs if absent. This checks the committed snapshot rather than the shared working tree,
which is the thing CI will build. Budget a few minutes: the release clippy build took 3m40s on this
box even with a warm registry.

**Reading the result: relay CI runs report `cancelled` routinely, and it is not a failure.**
`build-relay.yml`'s concurrency group holds at most one PENDING run, so every new push under
`daemons/hams_local_relay/**` replaces the queued one. Following a run id therefore shows
`cancelled` whenever anyone pushes relay code before yours starts. Follow the COMMIT instead: re-resolve
the newest run whose head contains it, with `git merge-base --is-ancestor <your sha> <headSha>`.
(Mechanism established by the `dependabot-ci-watch` run, 2026-09-16.)

One trap when scripting that: `gh run list --json conclusion` returns `""`, not `null`, for a
pending run, so jq's `//` does not substitute a placeholder. A space-separated line then collapses
two spaces into one, `read` shifts every field left, and the pending run is silently skipped --
which makes a watcher lock onto an older completed run and report a stale `cancelled` forever. Emit
`@tsv` and test the conclusion for empty as well as null.

**Also don't run `cargo fmt` on the whole crate in the shared tree** (as opposed to the extracted
snapshot above): other sessions' uncommitted relay edits sit there and you would reformat them.

**Relay clippy: include test targets.** `build-relay.yml` runs `cargo clippy --release --all-targets
-- -D warnings`. Plain `cargo clippy --release -- -D warnings` skips `#[cfg(test)]` code. On
2026-09-15 that let five `await_holding_lock` errors in new `#[tokio::test]` bodies reach a
commit. Run `cargo clippy --bin hams_local_relay --tests -- -D warnings`, and read only the
findings in your own files, since peers' uncommitted edits compile in too.

**A `.py` syntax check as bruce: use `ast.parse`, not `py_compile`.** Some modules'
`__pycache__` directories are owned by `odoo` (`distributed_redis_cache/tests/` on 2026-09-15). There,
`python3 -m py_compile` fails with `Permission denied` while writing the `.pyc` and never reports
on the syntax.

Choosing items this way also avoids collisions: `git status` in each repo shows which module
directories other sessions have uncommitted work in.

## Ask the peer before picking an item in a module they have uncommitted work in

`git status` showing another session's edits in a module is not by itself a reason to skip an
item there, and it is not a reason to proceed either -- ask. On 2026-09-15 `night-shift-todo-worker`
(workspace-80) wanted `high/user-id-by-slug-live-stale-cache-and-missing-filters-384ddb2f.md`, which
touches `user_websites/models/res_users.py` and `sql_views.py`, while workspace-cc had uncommitted
work in that module's data XML, `blog_post.py` and security XML for its own claimed item. No file
overlap at all -- but `test.py -u user_websites` installs the whole module, so each session's
verification run would have been testing the other's in-flight edits, and a failure would have been
unattributable. One `SendMessage` settled it in a single round trip: workspace-cc confirmed its
edits were coherent rather than half-finished, said its own run had been queued for the lock since
14:58, and asked that the item be left until its commit landed, with an explicit "if my run doesn't
get the lock within the hour, go ahead without me" fallback so the asking session could not end up
blocked indefinitely. Ask that question, and ask for that fallback, rather than guessing either way.

**Queue behind the peer you just agreed with, don't race them.** Waiting for the lock is a race --
whoever polls first after a run ends wins it (this file's own "A waiter can lose the race"). Having
agreed to let workspace-cc go first, workspace-80's waiter had to actually implement that: wait out
whatever run is live, then watch for a run whose command line contains the peer's modules, wait that
one out too, and only then take the lock -- with a deadline so an agreement never becomes an
indefinite stall. Put that waiter in a script FILE rather than an inline `bash -c` loop, which also
sidesteps the self-matching `pgrep` trap this file documents above: a file's own command line is
just `bash <path>` and cannot contain the pattern.

## Two linter/claims tools whose default invocation silently reports nonsense

Both cost a run several tool calls on 2026-09-15 before the output was recognised as an artifact of
how they were called rather than a real finding.

**`check_function_test_anchors.py <module dir>` reports every function in the module as new.** Its
`--baseline` default is a filename that does not exist (`function_test_anchor_baseline.json`, with
no repo suffix), so it loads an empty baseline and nothing is grandfathered; and its keys are
relative to the directory passed, so `user_websites/models/res_users.py::X` in the real baseline
never matches `models/res_users.py::X` from a module-scoped run. Run it from the repo root with the
real baseline:
`python3 hams_shared/tools/check_function_test_anchors.py . --baseline hams_shared/tools/function_test_anchor_baseline_hams_open.json`
(or `_hams_com.json`). Then read only the findings in files you touched -- peers' uncommitted work
shows up too.

**`check_claims_freshness.py` can never check a centralized claim**, and does not pretend to: it
skips everything under `docs/bug_hunt_claims/` by design, because its freshness model assumes the
claim and the anchored code share one git repo. Pointed at `hams_com` it reported over 1100
false "orphaned claim" lines the first time anyone tried. The tool for the centralized store is
`python3 agents/skills/bug-hunt/check_claims.py --claims-dir docs/bug_hunt_claims/hams_open
--source-root /home/bruce/workspace/hams_open`, run from the `hams_com` root. Its console output
truncates hashes to 12 characters; to get a full `code_hash` to paste into a claim's frontmatter,
import that module and call `_build_cross_repo_anchor_hash_index(source_root)` -- note its keys
strip the `COMM_` prefix, so the anchor `user_websites:COMM_res_users_write` is stored as
`user_websites:res_users_write`.

## Two claim files can share a basename across modules -- name the anchor, not the file

`check_claims.py`'s output lists claims by path, and the eye reads the basename. On 2026-09-15
workspace-05 and workspace-80 were each updating a file called `res_users_write.md` and each
assumed the other's heads-up covered its own: they were
`hams_com/docs/bug_hunt_claims/hams_open/user_websites/claims/res_users_write.md` (anchor
`user_websites:COMM_res_users_write`) and
`hams_com/docs/bug_hunt_claims/hams_open/zero_sudo/models/claims/res_users_write.md` (anchor
`zero_sudo:res_users_write`) -- different modules, different anchors, different stale hashes. One
message caught it; without it, the zero_sudo one would have stayed stale while both sessions
believed it was handled, and a later `check_claims.py` run would have re-filed it as a fresh
finding.

The claims store centralizes one module's claims per directory, and module-level method names
repeat (`write`, `create`, `res_users_write`), so basename collisions are normal, not a fluke.
When telling a peer which claims you are touching -- or reading a peer's list -- identify each by
its **anchor name**, which is unique by construction, not by its file's basename. The same applies
to your own commit message and to any to-do that names one.

## "The module's suite is green" does not mean its tests ran

A real test run backing a claim is this queue's own bar for done, and a module-level green result
quietly fails to meet it in one specific way worth naming, because it is invisible from the result
itself.

On 2026-09-15 workspace-05 ran `test.py -u user_websites` and honestly reported 246 tests, 1 failed.
In that same module, `static/tests/violation_report.test.js` held three tests of `_onModalShow()`
that were not among the 246 and never had been: the file is bundled and declares a hoot tag, but no
`browser_js()` wrapper anywhere asks `/web/tests` for that tag, so nothing triggers it. Nothing
reported was inflated -- the three tests were simply never in the count. But "the module's suite is
green" would have been offered as evidence the module was well covered, and it cannot carry that
weight. A green module-level run and never-executed tests inside that same module are perfectly
compatible.

The general shape, which is worth checking before writing a verification sentence: a test count
measures what the runner was asked to run, not what exists. Anything the runner is never asked for
is absent from both the numerator and the denominator, so it can never show up as a gap. That is
true of an unbundled `.test.js`, of a bundled suite whose tag no wrapper requests, and of a suite
whose `describe` block contains no `test()` call -- hoot reports all three as `Passed 0 tests`
followed by `Test suite succeeded`, which is exactly what `browser_js()` waits for.

`check_hoot_runner_coverage.py` now checks all three mechanically: a `*.test.js` on disk that no
bundle lists, a bundled suite whose tag no wrapper ever requests, and a `describe` block with no
`test()` in it. So the short version is: cite the checker, not just the run.

That sentence was wrong when this section was first pushed, which is worth keeping rather than
tidying away. It claimed all three checks existed while two of them sat uncommitted, held back with
the `user_websites` wrapper one of them required -- a skill file asserting a guard that was not in
the tree, committed inside the section about documentation nobody re-derives. workspace-05 caught it
within the hour by reading the commit before acting on it, it was softened to the tense that was
actually true, and this commit is the one that finally earns the original wording.

When writing up any verified result, prefer "these N tests ran and passed" over "the suite is green"
-- the first is a claim about what executed, the second quietly implies coverage the run never
measured.

## A status outside the four defined values escapes every query, not just its own

`status:` is `open`, `claimed`, `blocked` or `done`. Inventing a fifth does not merely fail to match
a query for its own name -- it falls out of the complete set of them, which is a different and worse
failure.

Found 2026-09-15: one item sat at `status: in_progress`. A session grepping `status: open` for
available work does not see it. A session grepping `status: blocked` to check whether a blocking
question was answered does not see it either. `night-shift-todo-worker`'s two passes are exactly
those two queries, so the scheduled automation cannot reach such an item at all, in either
direction, and nothing reports that it was skipped. It is not visibly stuck; it is invisible.

That item also held a real blocker -- an unapproved design doc -- recorded in `blocked_on:` as
prose rather than as a path into `night_shift_questions/open/`. Both halves matter and they compound:
even with the status corrected to `blocked`, the answered-question sweep walks each answered
question's own `blocks:` list, so an item no question file names can never be unblocked by it. A
`blocked_on:` that does not point at a question file is a note to a human, not a link the automation
can follow, and should be paired with a real filed question if the blocker is a decision.

So when a state genuinely isn't one of the four -- work started but not finished is the usual case --
use `claimed` and say what is done and what is left in the body, which is what `claimed` is for.
Reserve `blocked` for a real filed question, and point `blocked_on:` at its path.

## Gate the commit on the edit's exit status, not just on the edit asserting

A guarded edit is only half of it, and the missing half produced a commit message asserting a change
that was never made -- on 2026-09-15, in the middle of a run whose whole subject was artifacts whose
text nobody re-derives.

The edit itself was correct practice: a Python block that asserts the old text is present before
writing, so a line-wrap mismatch cannot silently write nothing. The assertion fired exactly as
designed, and no bad write happened. The shell around it was the gap -- the `git add`/`git commit`
line was a SEPARATE statement rather than part of the same `&&` chain, so the block's non-zero exit
did not gate it. One of the two files had already been written before the assertion aborted the
block, so `git add` staged real content, the commit succeeded, and its message described both
changes. A true-looking commit that overclaimed by one file.

Put the edit and the commit in one `&&` chain, so a failed or partial edit cannot be committed.

Three forms of the same hazard, worth recognising together:

- **A tool that reports success for doing nothing.** `sed -i` with a pattern matching nothing exits
  0. So does a hoot run with no tests (`Passed 0 tests`, then `Test suite succeeded`). Assert on the
  thing actually changing, not on the command returning.
- **A guard that fires but does not stop what follows.** The case above. An assertion protects the
  file; only chaining protects the commit.
- **Checking the diff you got rather than the diff you claimed.** `git diff --cached --name-only`
  before committing should match what the message says. That one line would have caught it.

A related shell trap, hit while writing this very section: a heredoc ends at its delimiter wherever
that appears at the start of a line, INCLUDING inside content that quotes a heredoc as an example.
Writing `PYEOF` inside a `<<'PYEOF'` block closes it early and hands the remainder to bash. Use a
distinctive delimiter, or write the script to a file first.

## Say so BEFORE filing, when two sessions are reading the same checker's output

`grep -rl` across the queue before filing a new to-do does not prevent a duplicate; it only catches
one that already landed. On 2026-09-15 workspace-80 and workspace-cc each filed a to-do for the same
four stale claim `code_hash` values within a few minutes of each other, and both had grepped first
and both had come up empty, because neither file existed yet when the other looked. The trigger is
specific and recognisable: both sessions were reading the same tool's output (`check_claims.py`)
right after a change that made that output meaningful again, so both saw the same residue at the
same moment.

So when you are about to file off a shared checker's output, and `ListAgents` shows a peer who has
been working the same area, message them before filing rather than after. Reconciling afterwards is
cheap and worked fine here -- the peer deleted its own file, kept the better record, and appended a
line to the survivor saying what it superseded, so a later reader isn't left hunting a path that
only exists in git history -- but the message costs less than the reconciliation.

## A fix that changes behaviour under test will be refused by the burn list

Worth knowing before designing one. Working the `distributed_redis_cache` poll-gate item on
2026-09-15, the first draft kept the suite's existing behaviour by excluding test processes from the
new gate (`not tools.config.get("test_enable")`). `check_burn_list.py` rejected that outright, by
AST and by regex, as the test-evasion pattern it exists to forbid -- correctly: a gate that reads
`test_enable` leaves the production path untested by construction. The honest fix was to let the
suite take on the real behaviour, which here meant every HTTP request and cron dispatch polling
Redis exactly as a serving worker does. Run `check_burn_list.py <module>` early, while the design
is still cheap to change, rather than after the tests are written.

## A new anchor must satisfy two separate checks, and the obvious fix for one breaks the other

`verify_anchors.py` reports on a new `[@ANCHOR:]` in two different sections of one long run, and a
session that fixes the first finding and stops has not finished:

- **Test linkage**: some test must carry `// Tests [@ANCHOR: name]`.
- **Documentation coverage**: some Markdown under `docs/stories/` or `docs/journeys/` must reference
  it — and there, the reference MUST carry the module prefix, `[@ANCHOR: hams_local_relay:name]`.
  An unprefixed reference in a manual is read as `global:name` and reported separately as "missing
  from operational source code," which looks like a completely different problem from the one you
  were fixing. The tool's own diagnostic says so, several hundred lines away from the finding.

The trap is in fixing the first one. The cheapest way to give an unlinked function test linkage is
to add a second `// Tests [@ANCHOR:]` line above a test that already exercises it — and two anchor
lines adjacent in a file is exactly what the **stacked anchors** check forbids. So the fix converts
a "no test linkage" finding into a "stacked anchors" finding, in a different section of the report,
and a session that re-greps only for its own anchor NAMES will not see it: stacked anchors are
reported by file and line number, never by name. Grep the report for your FILE, not just your
anchor names.

The real fix is one anchor per test. If a function has no test of its own, that is usually the
report telling you something true — write the test. On 2026-09-15 this produced a genuinely better
suite: the function in question (a direct UDP DNS query) had only ever been exercised through a
higher-level decision, and giving it its own test covered the socket path on its own for the first
time.

## Let clippy's dead-code pass tell you a refactor left a layer behind

`cargo clippy --tests -- -D warnings` treats `dead_code` as an error, and on a refactor that is a
feature rather than an obstacle. Splitting a function so a caller could do part of its work once
instead of per-iteration (2026-09-15, the ACME propagation wait) left two production wrappers that
nothing called any more — invisible in a passing test suite, because the TESTS still called them.

Reach for deletion, not `#[allow(dead_code)]`. Wrappers kept alive only by their own tests are worse
than useless: the suite goes green while exercising a path production no longer takes. Deleting them
and moving the tests onto the function production actually calls took one pass and made every
remaining test a statement about real behaviour. The same applies to a `#[cfg(test)]`-only builder —
that one is legitimate, which is why the distinction is worth making deliberately rather than
silencing the lint across the board.

## Raise a new guard in an `else:` clause, not inside a broad `try:`

Adding a check right after an existing call is the obvious placement and can be quietly wrong. Found
2026-09-15 while landing the hoot empty-run guard in `HamsHttpCase.browser_js`.

The `super()` call sat inside a `try:` whose `except Exception` was the TOUR-failure handler. An
`AssertionError` raised in that body does reach the caller -- but only after being misclassified on
the way out: the handler set `_hams_tour_failed` (which suppresses `tearDown`'s V8-log truncation),
walked the `__context__` chain looking for a severed-websocket signature, screenshotted a browser
that had completed perfectly normally, and only then re-raised. Every side effect wrong, final
exception right. That combination is precisely what survives review, because the test outcome looks
correct.

`try/except/else` is the fix, and the reason is a language guarantee rather than a convention: the
`else` block runs only when the body raised nothing, and **an exception raised inside `else` is not
caught by that statement's own `except` handlers**. So a new guard surfaces as the clean failure it
is. Before adding a check next to an existing call, read what the surrounding `except` actually
DOES with an exception -- not merely whether it re-raises one.

## A correctly-written test can leave no evidence in the log

Decide what a PASSING run should look like before reading the log, or a correct result gets
mistaken for a broken one.

Landing the guard above, the run's log was grepped for the new assertion's own message. Zero hits,
which read immediately as "the guard never fired" -- and it was wrong. The count was zero precisely
BECAUSE the test wrapped the call in `assertRaises`, so the exception was caught and never logged.
The better the test, the less residue it leaves. What actually settled it was the `odoo.tests.result`
line plus the absence of those tests from the failure list.

This is the same family as `test.py`'s headline counting ERROR-level log blocks rather than test
outcomes: in both cases the log was read for a signal it was never going to carry. The habit that
prevents it is cheap -- write down what a pass and a failure will each look like in the log, then
read for that, rather than grepping for the string that happens to be on your mind.

## `git add <paths>` then a BARE `git commit` takes the peer's staged work too

The section below records a peer's broad `git add` carrying off one session's staged change. This
is the same collision from the other side, and it is the more dangerous direction because the
damage is not confined to a misleading commit message.

2026-09-16: a run staged its own files with `git add <explicit paths>`, wrote a careful message,
and ran a bare `git commit`. A bare commit takes the WHOLE INDEX. The shared index already held a
peer's `git rm` of two files (a revert Bruce had asked it for), so those deletions went out inside
a commit whose message never mentions them -- **and the tree that was pushed could not import**,
because the peer had not yet made the matching edits to `models/__init__.py` and
`ir.model.access.csv`. It was holding off on those four files precisely because the committing
session had uncommitted work in them.

Staging explicit paths feels like scoping, and it is not: it controls what you ADD, never what the
commit TAKES.

**A pathspec on the commit is the minimum, and it is NOT sufficient.** This section first said
`git commit -F - -- <paths>` was "the habit that actually holds". That is wrong, and correcting it
here rather than quietly is the point: `git commit -- <path>` commits that path's WORKING-TREE
content, so a peer's uncommitted edit to a file you also touched still rides along under your
message. This had already been recorded twice before this run rediscovered half of it -- which makes
this the fourth instance of one hazard, and the second time a session has written a partial fix for
it into a skill.

**The authoritative rule is now `hams_shared/docs/adrs/0099_shared_working_tree_commit_discipline.md`.**
Read it before your first commit in a session; `hams_com/CLAUDE.md` points there too, so every
session gets it regardless of which skill it is running. It carries all five recorded incidents as
evidence and the six decisions that follow from them -- including the private `GIT_INDEX_FILE`
mechanism in both forms and its **resynchronisation step**, which is mandatory and is the step
that gets skipped, because the commit already looks finished by then.

This paragraph is deliberately a pointer and not a summary. An earlier version of it pointed at
`dependabot-ci-watch`'s operating notes, which have since become a pointer themselves -- a
pointer to a pointer is the same drift this is trying to avoid, so it now names the ADR directly.

The narrower rule that survives all of this, and the one to reach for first: **a commit touching
only files no other session shares** -- your own new to-do file, your own new module -- has none of
this exposure, which is exactly why `night_shift_todo/` was restructured into one file per item.

`git diff --cached --name-only` does not save you here either, and it is worth understanding why.
Run immediately after your own `git add`, it shows a correct picture; the peer's `git rm` can land
in the seconds between that and the commit, exactly as a careful commit message is being written.
The check is a snapshot of a shared resource, and the interval is the hazard -- the same reasoning
the section below already gives for putting the edit, add and commit in one `&&` chain.

**Recovering, when it has already been pushed.** Do not rewrite published history for this. Ask
first whether the tree you just pushed is CONSISTENT: a swept deletion usually is not, because its
owner had not yet landed the references to it. Forward-fix the inconsistency (remove the dangling
import, the orphaned ACL rows) rather than restoring the files, if the deletion was the outcome the
peer actually wanted -- restoring them to tidy up your own commit fights a decision someone else
made deliberately. Then tell the peer immediately, precisely: which of their changes you carried,
what you repaired, and what is still theirs to land. A peer's revert that is half-committed by
someone else's accident is a broken build for every session until its other half arrives, so that
message is urgent rather than courteous.

## A scoped pathspec protects what you commit, not what a peer sweeps

`git commit -- <pathspec>` is this file's standing advice for a shared tree, and it is necessary
but not sufficient. A pathspec constrains what YOUR commit takes. It has no bearing at all on what
someone ELSE's broad `git add` takes, and anything you have left staged is fair game for it.

Real collision, 2026-09-15: one session had `git add`-ed a to-do file and was composing a scoped
commit when a peer committed with a broad add. The peer's commit -- titled for an entirely unrelated
claim -- carried the first session's 37-line change to a different file, and the first session's own
pathspec commit then reported `nothing to commit`, which is the only reason anyone noticed. Nothing
was lost: the content was correct and it was pushed. But the commit message now describes about a
third of its own diff, and a later reader looking for what that commit changed will be misled.

The exposure is purely the interval between `git add` and `git commit`, and the interval is not
short -- writing a careful commit message is exactly the kind of pause that loses this race. So put
the edit, the `git add` and the `git commit` in ONE `&&` chain. This file already recommends a single
chain for a different reason (gating the commit on the edit's exit status, so a partial edit cannot
be committed); the two reasons compound and one chain satisfies both.

Distinct from the private-index hazard `dependabot-ci-watch` documents (hams_shared `133d8ae`):
there, a private `GIT_INDEX_FILE` left the real index holding a stale view. Here both sessions used
the real index entirely correctly. The shared resource that collided was time.

## After deleting a caller, ask what still calls the callee

A deletion that "has a replacement" needs one more question than it looks like, and the cost of
skipping it is silent. Found 2026-09-15, while scoping the eQSL half of the central-credential
retirement -- the item was about to repeat a mistake already shipped for LoTW two weeks earlier.

`ham.qso.sync_qsl_batch()` is the only code in hams_com that marks a QSO confirmed. hams_com
`8fb785bc` retired the LoTW sync daemon, which contained its only `"lotw"` caller. The replacement
was real and was verified: `hams_local_relay`'s `lotw.rs` genuinely does download confirmations
locally, without sending the password to hams.com, which is the whole point of ADR-0095. But the
relay's Odoo-side receiver records the confirmation into a log table and stops -- its own comment
says it exists to record the report, "not to interpret or correlate it against a logbook QSO" -- and
nothing reads those rows. So confirmations have not reached any logbook since. Nothing errors, and
every `test_qsl_sync*.py` still passes, because those tests call `sync_qsl_batch` directly. **They
verify the function works. Nothing verified that anything calls it.** That distinction is the whole
bug, and it is invisible from a green suite.

Parity was measured against the component that had been built (the download layer) rather than
against the specific call the deletion removed (the application layer). The question that catches it
is narrow, mechanical, and answers itself in one grep: **after this deletion, what still calls the
thing I am deleting a caller of?** Ask it of every deletion whose justification is "the replacement
exists," and ask it about the callee, not about the feature.

**The document trap underneath it is worth recognising separately, because the document was not
stale.** `LOTW_RELAY_INTEGRATION.md` §3 has exactly two bullets, adjacent lines in one list: "Done,
2026-09-14: retire the daemon" and "Add ingestion for `lotw_confirmation_received` into
`ham_logbook`'s QSL/QSO sync surface. **Still open.**" Both bullets were accurate. Only their
ordering was load-bearing, and nothing in the list encoded that one was a prerequisite for the
other. A checklist where the items are true but the sequence matters looks exactly like a checklist
where it does not. When marking one bullet done, read its siblings and ask whether any of them had
to come first.

## `blocked_on:` may point at a to-do, not only at a question

The `night-shift-questions` pairing assumes `blocked_on:` names a `night_shift_questions/open/*.md`
file, and the answered-question sweep walks each answered question's `blocks:` list to find items to
reopen. That covers an item waiting on a DECISION. It does not cover an item waiting on a piece of
ENGINEERING that has to exist first, which is a real and different state -- found 2026-09-15, when
the eQSL retirement turned out to be gated on building the confirmation-ingestion leg above.

Filing a question there would have been wrong: the question queue's own bar is a genuine judgment
call, and "someone must build the thing this deletion depends on" is not one. Leaving it `open` with
a prose blocker is the shape this skill's companion already forbids, because it reads as available
work and gets claimed before its body reveals the gate. `claimed` would have parked it behind a
session that was about to end.

So point `blocked_on:` at the blocking **to-do's** path and say plainly in both files that this is
what has been done. The trade is explicit and must be written down: no sweep will ever reopen such
an item, because no question governs it -- **whoever closes the blocking to-do is responsible for
flipping it back to `open`**, and the blocker file should carry a reciprocal section saying so by
path. State that in the blocked item too, so a reader who arrives at it first is not left waiting
for automation that is never coming.

## Two scheduled workers on one schedule converge on the same item

`night-shift-todo-worker` can be running more than once concurrently -- two of its runs were live
together on 2026-09-15, both hourly, both having just read the same `ls` of `high/`. Both had
independently picked the same top-of-queue item, and both were minutes from claiming it. One
`SendMessage` caught it.

This is not the same hazard as a human-driven session colliding with a worker: two workers execute
the SAME selection procedure against the SAME queue, so they do not merely sometimes collide, they
converge by construction, and the priority ordering that makes the queue useful is exactly what
guarantees it. `ListAgents` does not separate them -- both appear as ordinary `workspace-NN` rows.

Two consequences for how a run should behave. First, survey and claim must be as close to one step
as possible: read the directory, pick, claim, commit, push, and only THEN start investigating. Every
minute spent reading an item's body before claiming it is a minute another worker can spend claiming
it. Second, when `ListAgents` shows a peer whose name follows the same `workspace-NN` pattern and
the queue is the shared one, assume a same-schedule sibling until told otherwise and say what you are
about to take before you take it -- the message costs less than the reconciliation, which is the
same conclusion this file already reaches for two sessions reading one checker's output.

## Re-check the to-do still exists before appending its history entry

Closing an item is two writes to two shared files: append to `night_shift_history.md`, `git rm` the
to-do. Between deciding to close and actually closing, another session may have closed it already --
and the two writes fail differently. The `git rm` fails loudly, because the file is gone. The append
succeeds silently, because appending never conflicts with anything.

Hit 2026-09-15. workspace-41 verified a fix, appended a closing entry, and its `git rm` failed:
workspace-cb had closed the same item minutes earlier, and a `git pull` in the same command had
brought that removal in. Only the failing `rm` revealed it. Had the two writes run in the other
order, or the append been committed on its own, `night_shift_history.md` would carry two entries for
one fix under different headings with different wording -- which reads as two separate incidents to
anyone scanning it later, and history is exactly the artifact nobody re-derives.

So: `git pull`, then confirm the to-do file is still on disk, and only then append. Better, put the
append and the `git rm` in ONE `&&` chain so a vanished to-do aborts before the append -- the same
gating this file already argues for between an edit and its commit. And if you find your append
already written when the `rm` fails, discard it rather than committing it; assert the working tree
is HEAD plus exactly your own block before truncating, since a peer may have appended concurrently
and a blind `git checkout --` would take their entry with it.

The general shape, which recurs: when closing something means writing to a shared append-only file
AND removing a shared marker, the removal is the operation that can detect a race and the append is
the one that cannot. Order and gate accordingly.

## "Copy X exactly" is a snapshot of X, not a live reference

A to-do that says to mirror an existing implementation was written when that implementation looked
a particular way. On a tree this many sessions write to, that can stop being true within the hour.

2026-09-16: `high/clublog-event-odoo-receiver-missing` said to build a ClubLog receiver "copying the
eQSL/LoTW pair exactly". About thirty minutes before it was claimed, `7417a92f` added a whole
confirmation-APPLICATION leg to both of those models -- `time_on`, an `apply_state` selection,
`applied_at`, `apply_error`, a `_cron_apply_confirmations()`, two cron records and a
`ham_logbook.group_logbook_sync_service` ACL. Following the instruction literally would have copied
every bit of it into a model that can never use any of it, because ClubLog's real API is push-only
and emits no confirmation event at all: a sweep, a cron and four fields with no caller. That is the
same infrastructure-without-a-caller shape the item two entries above it in the same queue existed
to FIX, which is the part worth noticing -- the bug would have been introduced by obeying the to-do.

So before mirroring anything, read the template's own recent history (`git log -p --since=1.week
<path>`), not just its current state, and ask which parts of it are load-bearing for YOUR case.
A peer's one-line heads-up settled this one in the same minute the divergence was found
independently, which is the other lesson: when a to-do names a template, the session that most
recently changed that template is worth a message before you copy it.

## A checker can report nothing because it never looked

`check_claims_freshness.py` builds its anchor index from **git-tracked `.py` files only**. A claim
whose anchored function lives in a file you have created but not yet staged produces no output at
all -- not "validated", and not "orphaned claim" either. Both the reassuring result and the alarming
one are absent, and absence reads exactly like success.

Found 2026-09-16 while adding two new claims files alongside a new model: the tool was run, printed
nothing about them, and only a second run AFTER the commit actually exercised them. Had the
`code_hash` values been wrong, the pre-commit run would have said nothing either.

So run claim-freshness checks **after** the files are tracked, and treat "the checker said nothing
about my new thing" as a question rather than an answer -- confirm the tool can see the file at all
before reading its silence as a pass. This is the same family as the entry above on a hoot run
reporting `Passed 0 tests` then `Test suite succeeded`: a tool that was never asked about your code
cannot report a problem with it.

## Writing a new linter rule: its own tests prove almost nothing

Bruce asks for bug patterns found while debugging to become linter rules rather than one-off fixes,
so this comes up regularly. Two failures hit in one sitting, 2026-09-16, adding an SSRF
outbound-fetch rule to `check_burn_list.py`. Both were invisible from a green test suite.

**A rule's own passing tests say nothing about its false-positive rate.** The new rule shipped with
14 tests, all passing, and reported 17 findings across the two repos. Reading those 17 sites found
three defects in the rule, and twelve of the seventeen were its own noise:
`requests.request(method, url, ...)` puts the URL in `args[1]` and the rule read `args[0]`, so it
was examining the HTTP METHOD string (`"GET"` has no `://`, so every such call was "unverifiable"
however carefully its URL was pinned); an f-string or concatenation led by an ALL_CAPS constant
(`f"{CLOUDFLARE_API_BASE}{path}"`) fixes the host as firmly as a literal prefix; and a URL built on
its own line and passed as a variable -- the commonest real spelling of a fixed URL in this
codebase -- was judged on the bare `Name` alone. After fixing those: 5 findings, not 17.

The tests could not have caught any of it, because they were written from the same mental model as
the rule. **Scan both real repos and READ the sites before believing a new rule is ready**, and
treat a large finding count as a hypothesis about the rule rather than about the code. A rule whose
findings get resolved by suppression tags instead of by reading has become noise.

**A new ignore tag means editing TWO places, and only one of them is where the rule lives.** The
tag went into `add_warning`'s suppression list, which is the obvious half. It was never added to
the sanctioned-tag allow-list (`valid_audits`, near the UNAUTHORIZED BYPASS check), so using it
raised UNAUTHORIZED BYPASS -- an **error**, which halts the whole scan. Following the rule's own
printed advice therefore turned a warning into a hard build break, on the first real use. Grep for
an existing tag name (`audit-ignore-path`, say) and make sure your own appears everywhere it does.

**Related, and worth deciding deliberately: severity.** This scan halts a run on its first error, so
an error-level rule landing on existing call sites breaks every concurrent session's pre-flight
until all of them are triaged. Land a new rule as an `[%AUDIT]` warning, triage the findings, then
promote. And when triaging, remember a suppression can be the RIGHT answer rather than a deferred
fix: `zero_sudo`'s `_poll_health_check` was tagged, not converted, because `urlopen_ssrf_safe`
rejects loopback and private addresses -- exactly what a local daemon health check polls -- so the
rule's own recommended fix would have broken it.

## Fixtures built from documented behaviour pass while the code is wrong

A unit-test suite written from the same mental model as the code under test cannot contradict that
model. This file already says as much about a new linter rule's own tests; here is the same failure
in a much narrower place, found 2026-09-16 while replacing `test.py`'s browser-leak counter.

`/proc/<pid>/cmdline` is NUL-separated. That is true, documented, and what every fixture in the new
tests was built from. **Chromium does not write it that way**: it rewrites its own argv in place to
set the process title, so every one of its processes holds the entire command line in a SINGLE
NUL-separated element, several hundred characters long. A counter that split on NUL and took element
zero as the executable therefore read the tail of a `--user-data-dir` path as the program name, and
counted a live headless browser as **zero** -- while nine unit tests passed.

Only launching a real `chromium --headless --remote-debugging-port=...` and counting it found this.
That took one command and eight seconds. The numbers, worth keeping because they show the bug in
both directions at once: with a real headless browser running the broken counter said 0 and the
box-wide `pgrep -c -f chrom` it replaced said 41; after the fix, 7 and 41; with no test browser at
all, 0 and 29 -- those 29 being the developer's own desktop Chrome and the Claude desktop
application's Electron shell, which is what the old alert had been firing on.

Two habits follow, and the second is the one that costs nothing:

- **When a fixture encodes an external format, build at least one case from a real observation**, not
  from the specification. Paste an actual excerpt in, and say in the test where it came from.
- **Exercise a process-inspection or environment-probing helper against the real thing once**, even
  when its logic is fully unit-tested. This is the same shape as this file's own "a checker can
  report nothing because it never looked" and "a green module-level run and never-executed tests are
  perfectly compatible": the tool did run, and its silence was not evidence.

Related and worth stating plainly, since a monitor is easy to leave alone: **an alert that fires
when nothing is wrong is worse than no alert.** The old counter shouted `POSSIBLE BROWSER LEAK: 42
active Chromium processes` three times during one run about a browser nobody had leaked, which is
exactly how every session learns to scroll past it -- and a genuine leak of twenty headless browsers
would have hidden inside the same number on a quieter desktop day.

## Two scripted-edit and commit traps that each passed their own check

Both hit one `night-shift-todo-worker` run on 2026-09-16 (workspace-fc).

**`str.replace` with an empty search string rewrites the whole file.** A Python edit computed the
text to replace as a slice between two `s.index(...)` results. The slice came out empty, and
`s.replace("", new)` inserts `new` between every character: a 1,189-line model file became 306,341
lines. A syntax check caught it within a minute, before any test run imported it, and the file was
restored from `git show HEAD:<path>`. That restore was safe only because the file had been confirmed
free of peer edits before editing began. The guard this file already recommends -- assert the old
text occurs exactly once -- would have caught it too, but only if it runs on the COMPUTED string:
`assert old and s.count(old) == 1`. Prefer literal old text over index arithmetic.

**A private-index `git add` cannot stage a deletion you have not made yet.** Closing an item is
"append history, remove the to-do". Passing the to-do's path to the ADR-0099 private-index
recipe (`GIT_INDEX_FILE=idx git add -A -- <paths>`) while the file is still on disk stages nothing
for it, and the commit goes out with the history entry but without the removal -- while its message
says the item is closed. Delete the file from the working tree first (or run
`GIT_INDEX_FILE=idx git rm --cached <path>` and delete it afterwards), and read the commit's `--stat`
for the deletion line before pushing.

## Three traps from one worker run's verification (2026-09-16, workspace-f9)

**The load gate's 900-second timeout ends a queued run, and a waiter loop that retries only on the
lock message treats that as the result.** Continuous-integration jobs ran back to back for over 30
minutes, with the load average near 1.3 on 16 processors, so the gate kept seeing a live
`Runner.Worker`. Two queued runs each exited 1 after 900 seconds without starting Odoo. A waiter
that retries on `Another instance of test.py is already running` does not retry on
`still too busy to test on`, so it reports exit 1 as though the tests had failed. Pass
`HAMS_CI_LOAD_GATE_TIMEOUT=3600` and retry on that message too. Don't reach for
`HAMS_SKIP_CI_LOAD_GATE=1` just because the load looks low: the gate is checking for the build
matrix, not only for load.

**`cron.method_direct_trigger()` inside a `TransactionCase` cannot see the test's own rows.** It runs
the job on a separate cursor, so a fixture row that was created but not committed is invisible.
The log says `fully done (#loop 1; done 0; remaining 0)` and the assertion that the cron removed the
row fails, though the code is correct. To test a cron's wiring in a transaction case, assert on
`cron.code` and `cron.user_id`, then call the configured method as that user,
`env[cron.model_id.model].with_user(cron.user_id)._method()`. Use `RealTransactionCase` if you need
the real runner.

**A run that started after a peer's edit has not necessarily loaded all of that edit.** A peer's
controller change landed before Odoo started and was loaded, but the peer's new test methods were
added a minute later and were absent from the run. Before telling a peer that your run covered their
change, grep the log for their test names' `Starting` lines, not just for an absence of failures.

## Formatting only your own relay files while peers have uncommitted edits in the same crate

This file says not to run `cargo fmt` on the whole crate in the shared tree, and to check formatting
on an extracted committed snapshot. That covers checking. It leaves no way to FIX a finding in
your own files before committing, and the obvious per-file tools don't provide one. `rustfmt
src/main.rs` formats every module that file declares, including the peer's `clublog.rs` and
`lotw.rs`. `skip_children` is nightly-only. Hand-applying each diff hunk works, but it is slow and
error-prone. Found 2026-09-16 (workspace-57), with a peer's uncommitted ClubLog work in the crate.

What worked: copy the crate's `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml`, `rustfmt.toml`
and `src/` to a scratch directory. Run `cargo fmt` there, where the pinned toolchain still applies
(check `cargo fmt --version`). Then copy back ONLY the files you changed, and re-run
`cargo fmt -- --check` in the shared tree, where the only remaining diffs should be the peer's
files. `diff` each file before copying it back, because a peer may have edited the same file
since you last looked.

Related: a parallel `cargo test -- digital_decoder::` run starts two dozen real decoder pipelines
at once. At load average 7.8 the two RTTY pipeline tests timed out, and both passed in 15 s run
alone. Before calling such a failure yours, rerun the failing tests on their own.

## Self-improvement

Same convention as `dependabot-ci-watch`: if a run discovers a new, reusable fact about actually
using this queue (a naming collision that happened despite the hash suffix, a claim-staleness
judgment call that came up, a category taxonomy that turned out to need a new value), add it
directly to this file as part of that run's own commit.
