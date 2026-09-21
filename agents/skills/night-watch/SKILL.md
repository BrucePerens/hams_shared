---
name: night-watch
description: >-
  The one standing unattended agent for hams.com/hams_open/hams_shared. Each run watches the three
  GitHub repos (open Dependabot alerts, CI build failures, stale hand-tracked dependency pins) and
  fixes what is safely fixable, then works the shared structured to-do queue at
  hams_com/night_shift_todo/ (critical/high/medium/low, one file per item): unblocks items whose
  night_shift_questions/ question was answered, claims and finishes a bounded number of open items,
  and escalates real judgment calls to the questions queue. Records open work in
  night_shift_todo/, completed work in night_shift_history.md, and never a "nothing found" entry.
  Replaces the former dependabot-ci-watch and night-shift-todo skills. Triggers: night watch,
  night shift, dependabot, security alert, CI failure, build failure, to-do queue, claim a task,
  file a task, what's left, blocked on a question.
version: 1
---

# Night Watch

Bruce's instruction, 2026-09-16: "Merge the night-shift-todo skill ... with the dependabot-ci-watch
skill and call it night-watch. We'll run all of that work with one agent." This file is that merge.
Its two parts are the two former skills' content, kept close to verbatim because nearly every
paragraph records a real incident; only duplicated passages and cross-references between the two
old files were changed. Read the whole file before a run -- it runs unattended, with no memory of
any earlier run.

Companion skill, still separate: `night-shift-questions`
(`hams_shared/agents/skills/night-shift-questions/SKILL.md`), the queue of decisions only Bruce can
make. A `status: blocked` to-do points into it.

## A run, in order

1. **Orient.** Read `/home/bruce/workspace/hams_com/CLAUDE.md` and
   `hams_shared/docs/adrs/0099_shared_working_tree_commit_discipline.md` (how to commit in the
   shared working tree -- never a bare `git commit`). Run `ListAgents`: interactive sessions Bruce
   runs himself work the same backlog, and so can another run of this agent.
2. **Watch** (Part 1): Dependabot alerts, CI runs, and
   `python3 hams_shared/tools/check_dependency_releases.py` from hams_open. Fix what is safely
   fixable, including the whole implication chain; push it.
3. **Unblock** (Part 2): for every `night_shift_questions/answered/*.md` whose `blocks:` list names
   a to-do still `status: blocked`, flip it to `open`, clear `claimed_by:` unless that session is
   live, remove `blocked_on:`, and append a note naming the answered question and its decision.
   Commit and push each. Cheap; do it every run.
4. **Work the queue** (Part 2): `critical/`, then `high/`, `medium/`, `low/`. Claim, commit, and push
   the claim BEFORE investigating. Finish for real (tested, committed, pushed -- all three) or
   record exact progress, or file a real question and block the item. CI or security findings from
   step 2 that could not be fixed this run become queue items at an honest priority; an urgent one
   also gets a `SendMessage` to the session it affects.

   **No per-run item cap. Work the queue until it is genuinely empty, or every remaining item
   needs Bruce's own judgment call, or Bruce says to stop.** Bruce, 2026-09-16, correcting an
   earlier draft of this rule that capped a pass at three items: the hourly alarm's only job is to
   make sure step 2 (CI/dependabot) gets re-checked periodically, not to bound how much of the
   queue gets worked once the agent is up. So one pass is: Orient, Watch, Unblock, then keep
   claiming and finishing queue items back to back -- not stopping after a fixed count and waiting
   to be woken again -- until nothing open and unclaimed is left, or everything left is a real
   product-judgment call (a whole new feature, a design with open questions) rather than a bounded
   fix. Convert a genuine judgment call into a `night_shift_questions/open/` entry and move on to
   the next item, rather than skipping the whole queue over one large item. Only then record,
   improve this file if warranted, and heartbeat idle (see "The night-watch agent" below) -- an
   interactive session being actively told "keep working the queue" follows the exact same rule,
   there is no separate, more bounded mode for it. There is no special mechanism that makes
   continuing automatic (a plain instruction embedded in a skill is not a persistent background loop
   by itself) -- it works because the agent keeps reading and re-following this file's own procedure
   across many consecutive tool rounds in the same session, the same way it follows any other
   standing instruction here.
5. **Record** (see "Recording" below). A run that found nothing and worked nothing leaves no trace.
6. **Improve this file** with anything a future run would genuinely benefit from knowing (see
   "Self-improvement").

A run that has queued an Odoo test and is waiting on the lock or the load gate should use the wait
for steps 2 and 3 and for items that need no lock, not sit idle; and it must not end before that
test's real result has been read.

## The night-watch agent, and how the cron job wakes it

Bruce, 2026-09-16: "Set up the night-watch skill so that the night-watch agent does the actual
work, and the cron jobs just wake it up if it's not awake already." So there is one long-lived
agent session doing the work, and the scheduled task is only an alarm clock.

**Who the agent is** is recorded in a small state file outside every repository, maintained by
`hams_shared/tools/night_watch_agent.py` (`~/.local/state/night-watch/agent.json`; the
`NIGHT_WATCH_STATE` environment variable overrides the path). It holds the agent's `ListAgents`
name and its short `ref` id, `state` (`working` or `idle`) and `last_active`. Session names are
reused over time, so the **ref** is the identity; never match on the name alone. The tool cannot
tell whether a session is alive -- only `ListAgents` can -- so every liveness decision below is the
caller's, made against the live list.

```bash
python3 /home/bruce/workspace/hams_open/hams_shared/tools/night_watch_agent.py show
python3 .../night_watch_agent.py claim --name <name> --ref <ref> [--force]   # exit 3: someone else is recorded
python3 .../night_watch_agent.py heartbeat --ref <ref> --state working|idle  # exit 4: you were replaced
python3 .../night_watch_agent.py release --ref <ref>
```

**What the cron-fired session does, and nothing more:**

1. Run `ListAgents` (it prints your own name and ref first) and `night_watch_agent.py show`.
2. **A recorded agent whose ref is in the live list**: it exists. If its `state` is `working` and
   `minutes_since_last_active` is under 45, it is awake -- `SendMessage` it the one-line periodic
   reminder below, then stop. Otherwise `SendMessage` it the wake-up line instead, then stop.
   Either way leave no trace beyond the message itself: no commit, no file, no history entry.
   - **Awake (periodic reminder)**: `night-watch periodic check-in: if it has been a while since
     you last ran Part 1 (CI/dependabot), do that now, then continue where you were.` This exists
     because "awake and working" only proves the agent hasn't stalled -- it says nothing about
     whether Part 1 has been re-checked recently. Bruce, 2026-09-16: "the hourly alarm's only job
     is to make sure step 2 (CI/dependabot) gets re-checked periodically" -- a message that queues
     silently until the agent's next tool round (see below) is how that actually happens once the
     agent is mid-way through a long queue item, not just at the start of a pass. Don't turn this
     into a demand to drop what you're doing: the agent decides, on seeing it, whether a fresh
     Part 1 pass is due yet or it just ran one.
   - **Stale**: `night-watch wake-up: run a pass per the night-watch skill.`
3. **No agent recorded, or its ref is not in the live list**: there is no agent. Become it:
   `claim --name <your name> --ref <your ref> --force` (force only after `ListAgents` has shown the
   recorded ref is gone), then run a pass yourself as the agent (below). A scheduled session is
   still a real agent for as long as it lives; the next alarm checks again.
4. If `claim` exits 3 without `--force` because another cron run claimed a moment earlier, re-run
   step 1 rather than forcing: that session is the agent now.

**A cron-fired session cannot do steps 1-2 as written (found 2026-09-21).** In a scheduled-task run
there is no `ListAgents` tool (`ToolSearch` for it finds nothing). `SendMessage` to another session,
and the older `mcp__ccd_session_mgmt__send_message`, both refuse with "Messaging another session is
unavailable in unattended sessions". So the alarm can neither check the recorded ref's liveness nor
deliver the reminder or wake-up line. What that run could see: `list_sessions` and `get_session` (from
the `ccd_session_mgmt` tools) show sessions by title and `isRunning`, but they use a different id
space from the `ref`, so they are circumstantial, not identity. That run found the recorded agent idle for 128
minutes and claimed the role with `--force`, recording its own session id prefix (`local_...`) as
the ref, since no `ListAgents` ref was available. Until this is fixed, expect every alarm to fall
through to step 3, and check `show`'s `claimed_at` history to tell whether a live interactive agent
is being displaced each time. A displaced agent's next `heartbeat` exits 4, so it stands down rather
than both working. Fixing this needs a wake path that works from an unattended session (or an
interactive agent that polls on its own schedule). That is a design choice for Bruce, not something
to improvise in an alarm run.

**The hams_com shared `main` often lags `origin/main`** (it was 9 to 12 commits behind on
2026-09-21). Its reflog shows peers moving it with `reset: moving to origin/main` without updating
the working tree. `git status -sb` showing `[behind N]` therefore means a commit built from
`read-tree HEAD` is based on a stale tree. Build queue commits from `git read-tree origin/main`
(after `git fetch`) and push `<commit>:refs/heads/main` directly. If you have already committed on
the stale local `main`, rebuild the commits on `origin/main`, push those, and put the local ref back
with a guarded `git update-ref refs/heads/main <old> <yours>`. That restores exactly the state you
found, and needs no rebase or reset.

**What the agent does on a pass** (the start of its session, or a wake-up message):

1. `heartbeat --state working`. If it exits 4, another session has taken over: stop acting as the
   agent and say so in one line.
2. Run "A run, in order" above. Heartbeat `working` again whenever you start a new item or a long
   wait, so a slow Odoo run does not look like a dead agent (45 minutes is the wake threshold).
3. At the end of the pass, `heartbeat --state idle` and end the turn. An idle interactive agent is
   woken by the next alarm's message. Don't `release` at the end of a pass; release only when this
   session is deliberately handing the role off.

A wake-up message that arrives while the agent is mid-pass simply queues until the pass's next
tool round; answer it by continuing, not by starting a second pass.

Bruce can make any interactive session the agent by telling it to claim the role (`claim --force`
after checking `ListAgents`); the alarm will then wake that session instead of doing the work.

## Part 1: Dependabot, CI and dependency watch

**Old names in the notes below.** Most notes predate the merge and name the former skills and
tasks. Read `dependabot-ci-watch` (or "the hourly check") as Part 1, `night-shift-todo` as Part 2,
and `night-shift-todo-worker` or `dependabot-and-ci-watch` as an earlier run of this agent. The
incidents they describe are unchanged.

Bruce's own standing instruction (2026-09-14): "I would like you to autonomously track
dependabot and build failures. We have both." Then, once told this would run as a scheduled task:
"It can message other threads, add to the to-do, or fix things itself. It should check every
hour, for now." This skill is the repeatable procedure for that -- whether triggered by the hourly
scheduled task or invoked manually in a fresh session with no memory of any prior conversation, so
read this whole file rather than assuming context.

**This skill file is meant to improve itself.** Bruce's own instruction: "modify the skill itself
with context it needs to start work, so that it can start more efficiently, but if there is any sensitive data, it should store it in this publicly-exposed skill as a reference to a file in hams_com, which is not publicly exposed." If a run discovers a
new, reusable fact worth knowing on every future run -- a new token/permission-scope limitation (the
`workflow`-scope gap below was found and added exactly this way), a recurring CI failure's real root
cause, a dependency-ecosystem quirk in one of these three repos, a faster way to check something --
add it directly to THIS FILE as part of that run's own commit, not only to a durable-tracking file.
See "Step 3" below for the real distinction between the `night_shift_todo/` priority queue (open,
actionable to-dos only -- full convention in Part 2) and `night_shift_history.md`
(the record of what already happened) -- THIS file is neither of those: it's the durable OPERATING
KNOWLEDGE the next run starts with, a third, separate purpose,
edited directly, committed and pushed alongside whatever else that run did (matching the "keep this
and the scheduled task's own prompt in sync" note at the bottom of this file, now "Relationship to the scheduled task"). Don't let this file
grow unbounded with one-off trivia, though -- only add something here if a FUTURE run would
genuinely benefit from already knowing it, the same bar as any other durable-knowledge decision in
this codebase.

**Three ways to act, not just one**: fix things yourself and push, record durably (a new file in
`night_shift_todo/<priority>/` if it's still open, an appended entry in `night_shift_history.md` if
you finished it -- see "Step 3" below for the real distinction), or message another active Claude
session directly (`ListAgents` to see what's running, `SendMessage` to reach one) if something is
time-sensitive -- a real, currently-exploitable-shaped security exposure, or a build failure
actively blocking other in-progress work you can see evidence of (e.g. a very recent commit clearly
aimed at fixing something that then failed CI again). Don't message for routine/low-urgency
findings -- the durable record is the right channel for those; messaging is an ADDITION to the
durable record for urgent items, never a replacement for it (a chip or message alone is not durable
-- see the `hams-durable-tracking-over-chips` convention). This does NOT mean every run's own
findings get a to-do entry regardless of outcome -- a resolved item's durable record is
`night_shift_history.md`, and a "nothing found" run gets no durable-file entry at all.

**Check `ListAgents` before starting substantive work, not just when something urgent comes up.**
This skill runs both as a half-hourly (every-30-minutes) scheduled task and as manual, on-demand sessions Bruce starts
himself -- on a shared dev box, that means multiple sessions can genuinely be working the same
CI/dependency backlog at the same moment, all with standing authorization to act autonomously. Real
collision, 2026-09-14: two sessions independently started the identical rust-toolchain 1.98.1 bump
and clippy-fix task (same Bruce instruction, given separately to both), and it was only caught
because one session proactively messaged the other -- if neither had checked in, both would have
burned real time re-deriving the same 26 lint fixes. A second, smaller mishap the same day: a
session running `git commit` without reviewing `git diff --cached` first swept up another session's
already-staged, unrelated changes into its own commit (content wasn't lost, but the commit message
became misleading, and it took a cross-session exchange to sort out and record correctly).
Concretely: before committing to a specific fix (not before reading/investigating -- that's always
safe), a quick `ListAgents` costs nothing and tells you if another session is active on the same
codebase; if its name or a recent message suggests overlapping work, say so with `SendMessage`
before duplicating effort, not after. Before running `git add`/`git commit` on this shared working
tree, check `git diff --cached` (or use a scoped pathspec on `git add`, or a temporary
`GIT_INDEX_FILE` for the whole add-then-commit sequence) rather than trusting that everything
currently staged is your own -- another session's `git add`ed-but-not-yet-committed work can be
sitting in the same shared index. When you do find a real overlap: don't just pick one side
arbitrarily -- coordinate on who keeps working which piece (per file/module is usually a clean
split), reuse completed work across sessions rather than redoing it (a `git worktree diff` or
patch export both sessions can apply is often faster than re-deriving from scratch), and record the
coordination trail in the to-do's own file under `night_shift_todo/` (or `night_shift_history.md`
if it's already done) so it's not just two chat messages that vanish with the sessions.

**When you decline to do something and another session is running, talk to that session before
leaving it undone** (Bruce's instruction, 2026-09-14). Examples: holding off on a verification run,
leaving a fix for later, or skipping a step because it seemed like someone else's call. Find the peer
with `ListAgents` and say what you are declining and why. Agree that one of you will do it, if the
only alternative is that nobody does. It's a real failure when every session declines the same
thing. On 2026-09-14, this run and the `Dependabot/CI monitor` session each chose not to start a
workflow_dispatch to verify `65bc44e2`, so for a while nothing was going to verify it. This covers
declining by judgment only. If the permission system *denied* you an action, never ask a peer to do
it instead. That bypasses Bruce's permission decision, so record it for Bruce.

The three real repos: `/home/bruce/workspace/hams_com` (GitHub `BrucePerens/hams_com`),
`/home/bruce/workspace/hams_open` (`BrucePerens/hams_open`), and
`/home/bruce/workspace/hams_open/hams_shared` (a git submodule of `hams_open`, physically checked
out there, but its own separate GitHub repo, `BrucePerens/hams_shared`). `hams_shared` has no GitHub Actions
workflows (`gh api repos/BrucePerens/hams_shared/actions/workflows --jq .total_count` is 0, checked
2026-09-15), so an empty `gh run list` there is expected, not an access problem. Read
`/home/bruce/workspace/hams_com/CLAUDE.md` first for this project's standing engineering
conventions (env-var ordering for `sudo -u odoo` test runs, worktree symlink gotchas,
secret-handling rules) before doing anything else -- they apply to any fix you make here.

**On this box, plain `grep` is a shell function wrapping `ugrep` (via the Claude Code binary),
not GNU grep** -- `type grep` shows it. For a simple substring/basic-regex search this is
invisible, but a heavier extended-regex pattern (e.g. a bounded repetition like `.{0,80}` used to
grab surrounding context) can hit ugrep's own complexity limit and fail with `ugrep: error: ...
exceeds complexity limits`, printing nothing and, if stderr is redirected to `/dev/null` as usual,
looking exactly like "zero matches" instead of "the command errored." Found live, 2026-09-17/18: a
context-extraction grep across the bug-hunt claims corpus silently reported 0 results this way,
while a plain substring grep on the very same files found hundreds. If a `grep` command's result
looks suspiciously empty for a pattern that should obviously match, redo it once with `command grep`
(real GNU grep 3.11) before trusting the "no matches" conclusion, especially for anything beyond a
plain keyword or basic regex.

### Step 1: Check Dependabot alerts

For each of the three repos, run:
```bash
gh api repos/BrucePerens/<repo>/dependabot/alerts --jq \
  '.[] | select(.state=="open") | {number, severity: .security_advisory.severity, summary: .security_advisory.summary, package: .dependency.package.name, ecosystem: .dependency.package.ecosystem}'
```

For each open alert found:
1. Check whether `night_shift_todo/` already has an entry for this exact alert (`grep -rl` across
   the priority directories for the package name / alert number) -- if so and nothing has changed,
   skip it, don't re-report the same thing every run.
2. If new: investigate for real. Read the actual advisory
   (`gh api repos/BrucePerens/<repo>/dependabot/alerts/<number>`) for the real vulnerable version
   range and fixed version. Determine whether the vulnerable package is a direct or transitive
   dependency, and whether this codebase's own real usage of it actually reaches the vulnerable
   code path (per this project's own "re-derive from evidence, don't assume" discipline) -- a
   transitive dependency whose vulnerable function this codebase never calls the way the advisory
   describes is a real, honest "low real risk" finding, not grounds to ignore the alert entirely,
   but it changes how urgently it needs fixing.
3. If a safe, real fix exists (bump the dependency to the patched version in `Cargo.toml`/
   `Cargo.lock`, `package.json`/`package-lock.json`, `requirements.txt`, `go.mod`, etc. as
   appropriate), apply it, run the real affected test suite to confirm nothing broke, and commit
   it with a real, specific message ending `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
   If the fix requires a major version bump with real, non-trivial breaking-API changes, or
   removing/replacing a dependency entirely, do NOT force it unilaterally -- file it in
   `night_shift_todo/` as needing Bruce's own judgment call, with the specific tradeoff named
   (not just "needs a decision"). Before doing so, double-check the codebase's own actual usage
   against the advisory's flagged APIs, not just whether the crate builds -- a real miss on
   2026-09-15: an earlier run filed exactly this kind of "needs Bruce's call" note without checking
   that the one real call site didn't even touch the flagged API, leaving a safe fix undone for a
   day until another session checked more carefully and fixed it directly.
4. You have permission to push these commits directly (Bruce handed this over 2026-09-14). **How
   push access actually works, so you don't hit the same wall a prior session did**: Bruce's own
   git identity normally authenticates over SSH with a hardware YubiKey (`ED25519-SK`), which
   requires his own physical touch on the key for every signature -- something no autonomous
   session can ever provide. The real, working path instead: a `gh` CLI login already exists
   (`gh auth status` shows account `BrucePerens`, token scopes including `repo`, stored securely
   in the system keyring, not a plaintext secret anywhere), wired as git's own HTTPS credential
   helper via `gh auth setup-git` (check `git config --global --get "credential.https://github.com.helper"`
   -- should show `!/usr/bin/gh auth git-credential`), with all three repos' `origin` remotes
   pointed at `https://github.com/BrucePerens/<repo>.git` rather than the SSH `git@github.com:...`
   form. If `git push` ever fails with an SSH/publickey/YubiKey-signing error, check `git remote -v`
   first -- it likely means a remote reverted to the SSH form (re-run
   `git remote set-url origin https://github.com/BrucePerens/<repo>.git`) or `gh auth status` shows
   the login is gone (report that plainly rather than trying to create a brand-new token yourself;
   ask Bruce, per this project's own credential-handling caution -- see the "Secret Safety" section
   of `CLAUDE.md`). Push real, tested, low-risk fixes (dependency bumps that pass their real test
   suite) directly. For anything flagged as needing Bruce's own judgment, don't push code changes
   you're not confident in -- pushing your own uncertainty is different
   from pushing a verified fix.

   **Formerly a real gap, now resolved (2026-09-14)**: the `gh` token's scopes used to be
   `admin:public_key, gist, read:org, repo` -- notably missing `workflow`, which GitHub
   specifically requires to push ANY change to a file under `.github/workflows/`, even a plain
   `.sh` helper script that lives there but isn't itself a `.yml` workflow definition (confirmed
   directly the first time this was hit: a real push touching
   `.github/workflows/publish_relay_binary.sh` was rejected with `refusing to allow an OAuth App
   to create or update workflow ... without 'workflow' scope`). Bruce ran `gh auth refresh -s
   workflow` since then -- `gh auth status` now shows `workflow` in the token's scopes, and a
   real push touching `.github/workflows/build-relay.yml` succeeded cleanly the same night,
   confirming the fix actually stuck. **Don't assume this gap is still present** -- check `gh
   auth status`'s own scope list fresh each run rather than trusting this note's history; if
   `workflow` is ever missing again (e.g. a re-auth or a fresh token), the same rejection and the
   same fix (`gh auth refresh -s workflow`, an interactive device-code approval only Bruce can
   complete) apply.

### Step 2: Check CI build status

For each of the three repos, run:
```bash
gh run list --repo BrucePerens/<repo> --limit 15 --json databaseId,name,status,conclusion,createdAt,headBranch
```

For each run with `conclusion: "failure"` on the main/default branch that's NOT already recorded
in `night_shift_todo/` (grep the priority directories for the workflow name + rough date):
1. Get the real failure log. First map which jobs/steps failed:
   `gh run view <databaseId> --repo BrucePerens/<repo> --json jobs --jq '.jobs[] | select(.conclusion=="failure") | "\(.databaseId) \(.name) [" + ([.steps[]|select(.conclusion=="failure")|.name]|join(", ")) + "]"'`
   then fetch each failing job's own log: `gh api repos/BrucePerens/<repo>/actions/jobs/<job_id>/logs`.
   Don't rely on `gh run view --log-failed` alone -- it returns EMPTY output for jobs that fail in
   `actions/checkout`, in service-container startup, or before a runner was ever assigned (3 of 4
   failing runs on 2026-09-14 gave 0 lines that way).
2. Read enough of the actual log to find the real root cause -- don't guess from the workflow
   name alone. Read EVERY failing job's own failing step, not just the first one: one run of
   `Build Local Relay` can fail for several unrelated reasons at once (on 2026-09-14 it was
   eight legs with six distinct causes), and fixing one shared-looking cause does not turn the
   other legs green.
3. If it's a real, fixable bug in this codebase's own script/config/code (a real example already
   fixed once: a `jq --arg` call blowing past the kernel's `ARG_MAX` on a multi-MB base64 payload,
   fixed with `jq --rawfile` reading from a file instead -- see
   `.github/workflows/publish_relay_binary.sh` and `publish_relay_installer.sh`'s own git history
   for the exact pattern), fix it for real, verify locally as much as you can (a full CI re-run
   isn't available to you directly, but at minimum confirm the specific broken command/script now
   behaves correctly in isolation), commit, and push (same permission as above).
4. If it's an external/infrastructure issue (a flaky third-party service, a genuinely transient
   network failure, a runner capacity issue) rather than a real code bug, note that honestly
   rather than manufacturing a fix -- record it as "transient, not investigated further" with the
   real evidence for why you believe that, not just an assumption.
5. If the SAME workflow has failed repeatedly across multiple runs with the same root cause,
   that's a higher-priority, more consequential finding than a one-off -- say so explicitly.

**Fix the whole implication chain, not just the first-reported symptom (Bruce's own standing
instruction, 2026-09-14)**: "Fixing CI problems and dependency issues means fixing them and all of
their implications. You should be doing that autonomously." A dependency or toolchain bump is not
done when the bump itself lands -- if it changes what the codebase's own tooling flags (a newer
Rust's clippy surfacing lints an older one didn't, a newer linter/formatter version changing its
own defaults, a newer test framework deprecating an API this codebase used), fix those too, in the
same pass, without stopping to ask. Concretely: this project's own `rust-toolchain.toml` pins are
meant to track real current stable (see `daemons/hams_local_relay/rust-toolchain.toml`'s own doc
comment) -- when you bump one, immediately run `cargo clippy --release --all-targets -- -D warnings`
(matching CI's own "Clippy (warnings as errors)" step exactly -- see the `--all-targets` note below;
an earlier version of this line omitted that flag and was wrong) under the NEW toolchain and fix
every new finding it surfaces, the same run, not as a separately-deferred follow-up. The same principle
applies to a Dependabot dependency bump that changes a library's own deprecated-API surface, or any
other case where fixing the reported thing reveals more of the same category of problem one level
deeper -- keep going until the whole chain is actually clean, not just the one failure that was
originally reported.

**Known self-hosted-runner gotcha (fixed 2026-09-14, worth knowing if it ever resurfaces)**:
`hams_com`'s runner (`hams-devbox`) is persistent, not ephemeral -- the same `_work` tree is
reused across jobs. Any job whose `container:` is a plain OS image (ubuntu/fedora/rockylinux,
none of which set a non-root `USER`) runs as root, and root-owned files it leaves behind become
undeletable by the next job's `actions/checkout` step, which runs directly on the host as the
unprivileged `github-runner` account -- surfaces as `Deleting the contents of...` failing with
`EACCES`, blocking EVERY subsequent checkout regardless of which workflow runs next, not just the
one that caused it. Every container job in `build-relay.yml` now has a `Fix workspace ownership
for the next job` step (`if: always()`, `chmod -R a+rwX "$GITHUB_WORKSPACE"`, deliberately
chmod rather than chown to a hardcoded uid:gid, which would go stale if the service account is
ever recreated) as its last step. **Inside a job container, use `$GITHUB_WORKSPACE`, never
`${{ github.workspace }}`.** The expression expands to the host path, which doesn't exist in the
container, where the workspace is mounted at `/__w/<repo>/<repo>`. The step was first written with
the expression, and every container job's cleanup exited 1 with `chmod: cannot access ...: No such
file or directory`. It cleaned nothing and turned green legs red for hours before anyone read the
step's own log line (fixed 2026-09-14, fourth hourly run). The fix is confirmed in real CI: in run
34910889692, the rockylinux:9 and ubuntu:24.04 legs ran the cleanup with no error. The count of
runner workspace files that are neither owned by github-runner nor world-writable
(`sudo -n find <_work tree> -not -user github-runner -not -perm -o+w | wc -l`) climbed to ~10k
while a container leg was writing and dropped to 0 after each leg's cleanup. A nonzero count while
a container is running (`sudo -n docker ps`) is normal and does not mean the workspace is poisoned. A step that runs on the host (like the
`docker run -v` cleanup below) is correct with the expression. When verifying a cleanup fix,
read the cleanup step's own log output. The job going green or red doesn't tell you whether it worked. If a NEW container-based job is ever added to any of these three
repos' workflows, it needs this same step, or this exact failure mode will come back for that job.
If you ever see `Deleting the contents of...`/`EACCES`/`rmdir` in a checkout failure log again,
this is almost certainly the same root cause: check the runner host directly (`sudo find
<runner's _work tree> -not -user github-runner`) before assuming it's something new.

**The same gotcha also hits Docker container ACTIONS, not just job-level `container:` jobs
(found and fixed 2026-09-14, same day, after the fix above looked complete but wasn't)**: the
`security-audit` job in both `build-relay.yml` and `build-server-daemons.yml` has no job-level
`container:` at all, yet hit the identical `EACCES` checkout failure -- because
`EmbarkStudios/cargo-deny-action@v2` (one of its two steps) is itself a Docker container *action*
(`runs: using: docker` in its own `action.yml`), and its Dockerfile sets no `USER`, so it runs as
root with the workspace bind-mounted in, same as a job-level container would. The OTHER action in
that job, `rustsec/audit-check@v2.0.0`, is a plain `node20` action and is not a suspect -- check an
action's own `action.yml` `runs:` block before assuming which one is responsible. Because this job
has no container of its own, the job-level fix's "clean up as your own last step, from inside the
container" trick doesn't apply directly -- fixed instead with `docker run --rm -v "${{
github.workspace }}:/w" alpine chmod -R a+rwX /w` as the job's own last step (`github-runner` is
already in the `docker` group, so this needs no new host privilege). If a workflow ever adds a NEW
step using a Docker container action (check its `action.yml`'s `runs:` block, not just whether the
job itself declares `container:`), assume it can write root-owned files into the workspace and
needs this same `docker run ... chmod` step after it, not just after job-level `container:` usage.

**More operating knowledge (added 2026-09-14, second hourly run)**:
- **Billing-blocked GitHub-hosted jobs** (`build-macos`, the one leg still on `macos-latest`): the
  job shows `conclusion: failure` with empty `steps`, no `runner_name`, and a `BlobNotFound` 404
  for its log. The real reason is only in the check-run annotations:
  `gh api repos/BrucePerens/<repo>/check-runs/<job_id>/annotations --jq '.[].message'` (seen:
  "recent account payments have failed or your spending limit needs to be increased"). Only
  Bruce can fix billing. Every other hams_com job runs on the self-hosted `hams-devbox` runner
  and is unaffected.
- **Container jobs run as root, but `HOME=/github/home` is a bind mount owned by the host's
  `github-runner` account.** Tools that insist on owning their home refuse to run: Wine exits with
  `wine: '/github/home' is not owned by you` (fixed in `build-windows` with a job-level
  `WINEPREFIX: /tmp/wine-ci-prefix`), and rustup prints a harmless
  `error: $HOME differs from euid-obtained home directory` in every container leg. That rustup
  line is noise, not the failure -- keep reading.
  **Two more log lines read like failures and are not.** Both surface from the obvious first move of
  grepping a failing job's log for `error:`, and both cost run 31 (2026-09-16) real time.
  (1) `cargo:warning=Compiler family detection failed due to error: ToolNotFound: failed to find
  tool "cc"` is the `cc` crate's probe running before the toolchain is on PATH; it does not mean the
  build lost its compiler. The number that actually matters is the same build's own
  `baked N Hamlib rig model(s)` line -- build-windows baked 312/57 and ubuntu:20.04 baked 230/31 in
  the very logs carrying that warning. (2) `Error: Unable to find a match: wsjtx` in a
  `build-redhat` leg is deliberate: both the Debian and RPM `Install Test-Only Dependencies
  (sox, wsjtx)` steps end in `|| echo`, because the FT8 real-signal tests are `#[ignore]`d and gated
  on `binary_exists()`, so a missing wsjtx skips them rather than failing the leg. Read down to the
  real `##[error]` line before chasing either.
- **Reading a failed `Run Ignored FT8 Real-Signal Test` step** (`ft8_decode_fires_at_a_real_utc_slot_boundary`):
  in that step's log, each `Block size = 7680` line marks a new `Ft8Decoder`, which is a wall-clock
  15s slot swap. The numbered `state:` lines are RADE modem frames, 120ms of audio each, so the
  test's 15s FT8 signal is exactly states 1-125. **Don't read the first-to-last `state:` span as the
  feed time.** After the signal the test sends a 5ms silence chunk every 200ms while it waits for the
  boundary, so states 126 onward arrive 4.8s apart and come from silence. A run of this skill on
  2026-09-16 misread that span as a 13s feed on the Pi and filed a throughput problem that did not
  exist: states 1-125 took 3.3s in every Pi run. If a `Block size` line falls between states 1 and
  125, the feed crossed a slot boundary and the signal was split, so it could not decode. That is
  how run 35090450429 failed (its feed started 1.4s before a boundary). The test now waits until
  just after a boundary and asserts the feed stayed inside one slot, so that assertion firing
  really would mean the pipeline ran slower than real time. For real per-stage Pi throughput, run
  hams_com's `src/digital_decoder_timing_probe.rs` (see its module doc). The Pi's throttling check is
  `sudo -n vcgencmd get_throttled` over `ssh pi500-1` (plain `vcgencmd` cannot open `/dev/vcio`
  as `ai`); `0x0` means no throttling since boot.
- **The arm64 `build-linux` leg runs natively on the Raspberry Pi 500 runner `pi500-1`** (since
  2026-09-14; it used to run under QEMU emulation on the dev box, which broke whenever a reboot
  dropped the binfmt handlers). Facts: runner label `pi500-1`; Debian 12 bookworm, aarch64; jobs
  run as the `ai` account, which has passwordless sudo; rustup is installed; there is no Docker,
  so that leg has `container: ""` and runs on the host. The runner is ephemeral (one job per
  just-in-time registration), re-minted continuously by `pi500-runner-jit-loop.service` on the dev
  box. If the Pi picks up no jobs, check that service first (`systemctl --user status
  pi500-runner-jit-loop` -- it is a user unit of bruce's, so the plain system-level `systemctl`
  reports "could not be found", which is not the service being gone), then `gh api repos/BrucePerens/hams_com/actions/runners`. The Pi has an
  outbound firewall (`pi500-egress`) allowing only ports 80/443/22, DNS and NTP, so a build step
  that needs another port will be dropped (logged as `pi500-egress-drop:` in the Pi's dmesg).
  `pi500-1` showing `offline` in the runners API between jobs is normal for an ephemeral runner.
  `test-pi500-runner-smoke.yml` (workflow_dispatch) is a cheap check that the runner is alive.
  The runners API also lists `Bruce_Thinkpad` (labels `Bruce_Thinkpad`, `windows`), a Windows
  laptop runner set up the same just-in-time way (hams_com `docs/proposals/WINDOWS_QUALIFICATION.md`).
  It showing `offline` is normal, and as of 2026-09-16 no workflow under `.github/` targets it.
  Security note: the runner's just-in-time config is visible in its process arguments on the Pi,
  so never paste `ps` command lines from that box into a record.
  Two failure shapes the first native runs hit (2026-09-14), worth recognizing immediately:
  (1) **Non-root cleanup.** Unlike the root-in-container legs, the Pi leg runs as `ai`, so anything
  that creates read-only files (Go's module cache is the known case) breaks a plain `rm -rf`.
  `install_relay_runtime_deps.sh` now builds with `-modcacherw` and `chmod -R u+w`s before cleanup.
  (2) **The firewall drops, it doesn't reject**, so a connection to a blocked port hangs until the
  caller's timeout instead of failing fast. Relay tests must never dial live outside services on
  non-web ports. The winlink tests used pat's `telnet` alias, which dials the real Winlink CMS on
  port 8772; they now use a closed loopback URL. When a Pi-only test failure is a timeout, check
  `sudo journalctl -k --since today | grep pi500-egress-drop | grep -o 'DPT=[0-9]*' | sort | uniq -c`
  on the Pi first. Running the same tests in a root container is not an equivalent reproduction.
- **`hams_local_relay` pins its compiler in `rust-toolchain.toml`**, and CI's `cargo clippy` runs
  under that pin, not under whatever `dtolnay/rust-toolchain@stable` installed. To reproduce CI's
  clippy locally, run it inside `daemons/hams_local_relay` with no `+toolchain` override, using a
  scratch `CARGO_TARGET_DIR` so you don't disturb other sessions' builds.
  **CI's exact invocation is `cargo clippy --release --all-targets -- -D warnings`** -- read
  `build-relay.yml`'s "Clippy (warnings as errors)" step rather than trusting a remembered form.
  `--all-targets` matters: it lints `#[cfg(test)]` code too, so **a clippy error that exists only in
  test code IS a real CI failure**, not local hygiene. Plain `cargo clippy` (no `--all-targets`,
  no `--tests`) never compiles test targets and will report clean on exactly that failure. An
  earlier version of this file quoted CI's step without `--all-targets`, which would have led a run
  to write off a test-only lint as harmless. The step runs on the ubuntu-22.04 leg only, so it is
  often still queued hours after a relay push -- a committed test-only lint can sit unreported for
  a long time, which makes checking it locally worthwhile rather than waiting for the leg. The three server daemon
  crates (`hams_relay_bridge`, `hams_data_relay`, `hams_simulated_band`) have NO pin: their CI
  uses the newest stable directly, so a new Rust release can turn them red with new lints. Check
  them with `cargo +stable clippy --release --all-targets -- -D warnings`.
- **`build-relay.yml` only triggers on pushes under `daemons/hams_local_relay/**`.** A push that
  only touches the workflow file does not run CI. Verifying such a fix needs the next relay push
  or `gh workflow run build-relay.yml --ref main` (workflow_dispatch builds and tests but never
  publishes). That runs a full matrix on Bruce's laptop, so check `ListAgents` and coordinate
  first; another session may already have one in flight.
- **`hams_open`'s `Dependency Release Watch` shows red whenever a hand-tracked pin**
  (`hams_shared/tools/dependency_watch.json`) is behind upstream. It is a staleness alarm, not a
  broken script -- and per Bruce's standing decision below, a red run is work to do: bump the stale
  pins, following the verification procedure each entry's own `notes` field documents.
  **That workflow runs only on Mondays (cron `0 13 * * 1`), so a green run or a "clear" note goes
  stale within hours.** mercury's `mercuryv2` branch in particular moves several times a day. Each
  run of this skill should run the checker itself, from `hams_open`:
  `python3 hams_shared/tools/check_dependency_releases.py` (exit 1 means a pin is behind). It takes a
  few seconds. On 2026-09-15 (seventeenth check), a note saying "clear" had been copied forward for
  13 hours while mercury was behind again.
  **Quick mercury bump check**: `gh api repos/Rhizomatica/mercury/compare/<old>...<new>` for the
  file list, then clone at the new commit, `make`, run the binary with spawn_mercury()'s flags
  (`-x null -p <port> -L <log>`, probe the control port with bash's `/dev/tcp`), and when the diff
  touches `datalink_arq/` also run upstream's `make -C tests test` (Unity tests, about a minute;
  it prints `=== All Tests Passed ===`). The relay sends no CONNECT/DISCONNECT itself (Pat's
  `varahf://` client does), so ARQ changes matter through Pat, not mercury.rs.
- **The hams_com working tree is shared with other live Claude sessions.** Commits and line
  numbers can change under you mid-run. `git fetch` and re-read before editing, find edit sites
  by content rather than stale line numbers, and `git add` only the specific files you changed.
  If a peer session is working the same failures (`ListAgents`, then a short `SendMessage`
  naming who takes which item), split the work rather than both editing the same workflow file.
  **You cannot tell which session made a commit from its authorship.** Every session on this box
  commits as `BrucePerens <bruce@perens.com>`, so a commit's author, its subject line, and even its
  topical similarity to work you saw a session doing are all worthless as identification. On
  2026-09-16 a run guessed an owner that way and messaged the wrong session twice. If you need to
  know whose a commit is, ask on `ListAgents`'s live sessions rather than inferring it -- and expect
  that sometimes nobody claims it, because the session that made it has already exited.
  Relatedly, `git log --oneline origin/main..main` coming back empty is only true at the instant you
  run it: a peer can commit into the shared tree between that check and your `git push`, and your
  push then carries their commit too. It happened the same run. That is harmless for a to-do note,
  but it would publish a relay change someone was deliberately holding, so re-run the check
  immediately before pushing rather than once at the start, and tell the owner if you find you
  pushed something of theirs.
  **Check a claimed CI fix against `origin/main`, never the working tree.** A peer's fix often
  exists only as uncommitted edits to the shared tree, so `grep`ping the file on disk shows the
  clean, fixed form while CI is still failing on the pushed, unfixed one. Read the pushed content
  directly: `git show origin/main:<path> | grep -n <pattern>`. Found 2026-09-16, twenty-sixth
  hourly check, on the `offline_queue_test_lock` clippy failure -- the working tree had the async
  `tokio::sync::Mutex` fix and `origin/main` still had `std::sync::MutexGuard`.
  **Don't write an ordinal ("the Nth hourly check") you haven't actually counted.** Several notes
  in this file carry one, and it is easy to copy a plausible-looking number forward from whatever
  the last note said. The real count comes from `list_task_runs` on the `night-shift-todo-worker`
  task (`totalRuns`; before 2026-09-16 the watch ran as its own `dependabot-and-ci-watch` task); its newest entry also gives this run's true `started_at`, which is the date
  to write -- a run that starts at 00:2x UTC belongs to the next day from the one whose CI logs it
  is reading. Both were wrong in this note's first draft.
  **A red clippy on the ubuntu-22.04 leg is everyone's problem, not just the owning feature's.**
  It fails every later relay run regardless of that run's own content, and because clippy runs
  before Formatting, no rustfmt check runs either while it's red. So when a to-do says a fix is
  held pending something else, check whether unrelated relay commits have landed since
  (`git log --oneline <fix-was-diagnosed-at>..origin/main -- daemons/hams_local_relay/`) -- if
  they have, the hold is now costing other sessions, and that's worth a `SendMessage` to the
  owner suggesting it split the fix into its own commit. Don't edit files a peer has asked you
  not to touch; say what you found instead.
- **Before calling a failure a regression, check that the fix is actually in the run's head
  commit**: `git merge-base --is-ancestor <fix-commit> <run's headSha>`. The single `hams-devbox`
  runner often has several runs queued, so runs on commits from before a fix keep reporting the
  "fixed" failure for hours afterward. On 2026-09-14 a `jq: Argument list too long` and a burst of
  checkout `EACCES` both showed up after their fixes had been pushed, and both came from such runs.
  Every `Build Local Relay` run on a commit from before hams_com `65bc44e2` re-poisons the workspace
  with root-owned files, until they drain. Before that commit, the job-level container cleanup never
  worked (see the `$GITHUB_WORKSPACE` note above), so runs on commits after `9c77855d` but before
  `65bc44e2` (e.g. 34900430486, 34903239033) don't verify the container-leg cleanup either.
- **You may cancel superseded runs and clean the runner workspace yourself.** Bruce added these to
  `permissions.allow` on 2026-09-14 and asked that this skill say so: `Bash(gh run cancel:*)`,
  `Bash(gh run *)`, `Bash(sudo -n chmod *)`, and `Bash(sudo *)`. Before they were added, the
  permission classifier denied both actions. `gh run *` also covers `gh run watch`, `list`, `view`
  and `rerun`, so you can watch a verification run to completion instead of leaving it for the next
  hourly run.
  - Cancel a queued run when a later queued run of the same workflow on `main` makes it pointless.
  - Clean the runner workspace (`sudo -n find <_work tree> -not -user github-runner | wc -l`, then
    `sudo -n chmod -R a+rwX /home/github-runner/actions-runner-hams-com/_work/hams_com/hams_com`)
    when checkout fails with `EACCES`.
  - Run each `gh run cancel <id>` as its own simple command, not in a shell loop, so it matches the
    allow rule.
  - Never cancel a run that is already in progress on a publish step.
  - `build-relay.yml`'s `concurrency:` group (`cancel-in-progress: false`) holds at most one
    *pending* run. A newer push replaces the pending one, which then shows `cancelled` with 0
    jobs. That is GitHub's behavior, not another session cancelling it. The in-progress run is
    never touched.
  - So your own push under `daemons/hams_local_relay/**` (a pin bump, say) replaces the pending run.
    If a `night_shift_todo/` item names that pending run as its verification, send the item's
    `claimed_by` session the replacement run id and your commit hash. It may be following the
    cancelled run. This came up on 2026-09-15 with the mercury ed0681e7 bump.
  - **To verify a relay commit, follow a commit, not a run id.** On a busy day the pending run is
    replaced over and over: the mercury 7af6d188 check lost five pending runs in three hours before
    one ran. A `gh run watch <id>` just ends with `cancelled`. Instead, poll `gh run list --workflow
    build-relay.yml` every few minutes and stop at the first run that is `completed`, not
    `cancelled`, and whose head passes `git merge-base --is-ancestor <your-commit> <headSha>`. Run
    that in the background. Any later run carries your change, so whichever one runs verifies it.
    **Use the `Monitor` tool for that wait, not `Bash` with `run_in_background`.** The Bash tool's
    own `timeout` parameter is capped at 600000 ms -- ten minutes -- so "background it for a few
    hours" (what this note said until 2026-09-16) is not something that tool can do, and a watcher
    written that way dies long before the leg it is waiting for reaches the queue's front. `Monitor`
    takes `timeout_ms` up to 3600000 (one hour), or `persistent: true` for the rest of the session.
    Write the loop so it prints exactly one line and exits when it finds the verifying run, and so
    a failed `gh` call can't kill it (`|| true` on the poll). On 2026-09-16 the ubuntu-22.04 leg of
    the run verifying `42ce3373` was still queued forty minutes after the push, behind four other
    legs on the single `hams-devbox` runner -- an hour is the right order of magnitude for this
    wait, not ten minutes.
    Also, never stop your own watcher with `pkill -f <script name>`: the CLAUDE.md self-match trap
    applies to this exactly, because your `bash -c` command line contains the pattern, so the
    `pkill` kills the shell running it and whatever else was chained after it never executes. Give
    the watcher its own deadline, or use `TaskStop`.
  - Record what you cancelled in `night_shift_history.md` (it's a completed action) or file a
    `night_shift_todo/` entry if cancelling it leaves something still needing a real re-run.
- **A transport tool's version-probe flag is part of what a pin bump can break.** The relay finds
  each tool by running it and matching a banner (`mercury::probe_mercury_installed`,
  `probe_ardopcf_version`, `probe_direwolf_version`). mercury `a85ea0ba` (2026-09-17) added `-V` and
  in the same commit stopped printing its `Rhizomatica Mercury Version` banner for `-h`, so bumping
  the pin alone would have made every relay report a working mercury as not installed -- and
  `spawn_mercury_and_wait_for_mercury_ready_against_the_real_built_binary` SKIPS itself when no
  mercury is discoverable, so CI would have stayed green on it. The probe now runs `-V`, which older
  builds also answer (they print the banner before rejecting the unknown flag; the probe ignores the
  exit status). So when bumping any transport tool's pin, diff its CLI/usage handling as well as its
  protocol code, and grep the relay for the flag the probe uses. More generally: a test that skips
  itself when the tool is absent cannot report that the tool became undetectable -- the fake-binary
  test is what guards that, so make the fake mimic the new real binary's flag behaviour rather than
  answering every argument.
- **`$RUNNER_TEMP` is not unique per job on the persistent hams-devbox runner.** A fixed path under
  it (`$RUNNER_TEMP/pip-audit-venv`) was found half-removed by the second matrix leg two seconds
  after the first leg finished, which surfaced as `No module named pip.__main__` on one leg while the
  other passed (run 35248460489). Use `mktemp -d "$RUNNER_TEMP/<name>.XXXXXX"`, pass the path on
  through `$GITHUB_ENV`, and remove it in an `if: always()` step.
- **The runner's system Python is PEP 668 externally managed**, so `pip install --user <tool>` in a
  host-side job exits 1 with `error: externally-managed-environment` before the tool ever runs. That
  is what `Python Dependency Audit` did on every run from 2026-09-08 (when it moved to self-hosted)
  to 2026-09-17. Install such a tool into a virtual environment instead. hams_open's copy of that
  workflow runs on `ubuntu-latest` and is unaffected, so a green run there says nothing about the
  hams_com one.
- **The relay's transport-tool install scripts build pinned commits** (since 2026-09-15). Both
  `daemons/hams_local_relay/install_relay_runtime_deps.sh` and `install_relay_runtime_deps_rpm.sh`
  fetch `MERCURY_COMMIT`/`ARDOPCF_COMMIT` exactly. The Debian script also has
  `HAMLIB_VERSION`/`HAMLIB_SHA256`. Before that, both scripts cloned the moving branch HEAD and
  ignored `dependency_watch.json`, so CI built whatever upstream pushed that hour. That is how the
  ubuntu:20.04 leg broke. **When you bump any of these pins in `dependency_watch.json`, update the
  same constant in both scripts**, or CI keeps building the old commit. Hamlib has a second pin
  site: `build-relay.yml`'s build-windows "Cross-build hamlib for mingw-w64" step carries the same
  `HAMLIB_VERSION`/`HAMLIB_SHA256` and also builds that release natively, so build.rs's baked
  `rigctl --list`/`rotctl --list` tables match the Hamlib the .exe links (since 2026-09-15; before
  that it used unchecksummed 4.6.2 and Ubuntu's 4.5.5 `libhamlib-utils`). The Debian script
  compiles a probe against the distribution's hamlib and builds static Hamlib from source only when
  the probe fails. That happens on Ubuntu 20.04 (3.3) and 22.04 (4.3.1, no `rigerror2`). Debian 12
  (4.5.4, the pi500 leg) and 24.04 use the system package. A new mercury call into a newer Hamlib
  API belongs in the probe (`hamlib_probe.c` heredoc). On amd64 a second probe checks the default
  compiler for `_mm256_loadu2_m128i` (GCC >= 10, used by mercury's vendored RaptorQ) and builds
  mercury with `CC=gcc-10` on Ubuntu 20.04 (GCC 9.4). To reproduce a transport-tools CI leg, run
  `install_debian_deps.sh` then `install_relay_runtime_deps.sh` in a `docker run --rm
  --cpuset-cpus ... ubuntu:<version>` container (install `sudo git curl ca-certificates
  build-essential` first). That reproduced both 20.04 failures locally.
- **`Swatinem/rust-cache` doesn't include the job container in its cache key.** The automatic key is
  job id, OS family and architecture. Matrix legs of one job that differ only by `container:` used
  to share one `target/` cache: `build-linux`'s ubuntu 20.04/22.04/24.04 legs, and `build-redhat`'s
  fedora and rockylinux legs. Cargo's fingerprints don't track glibc, so an older-glibc leg reused
  build scripts from a newer one and failed with `version 'GLIBC_2.3x' not found (required by
  .../build-script-build)`. This first showed on 20.04 in run 34914239136. Fixed in hams_com
  `91755340` with `key: ${{ matrix.os }}` / `key: ${{ matrix.container }}`. A new matrix job that
  varies `container:` needs the same `key:`. To see which cache a leg restored, read the rust-cache
  step's `Restored from cache key` line. `gh api repos/BrucePerens/hams_com/actions/caches` lists
  the caches and when each was saved. The cache also restores `~/.cargo/bin`, without cargo's
  install metadata, so a plain `cargo install <tool>` step works once and then fails on every later
  run with "binary `<tool>` already exists in destination" (exit 101). The `.deb` job hit this with
  cargo-deb in run 34936877542. Guard every `cargo install` step with `command -v <tool> ||`
  (hams_com `22f07cd4`).
- **Check each packaging job's "baked N Hamlib rig model(s)" line, not just the build legs'.**
  build.rs needs `rigctl`/`rotctl` (`libhamlib-utils`) at build time. With no rigctl it still builds,
  but it bakes an empty table that refuses every named radio. `install_debian_deps.sh` now installs
  it (hams_com `22f07cd4`). Before that, the build-linux legs only had rigctl through their test-only
  wsjtx install, and the `.deb` job shipped "baked 0".
- **Reproduce a failing container leg locally before calling a CI fix done, and keep going past
  the first error.** `build-raspbian` and `build-windows` each hid several failures in a row,
  because each failure stops the job at its first broken step. Stage the committed source with
  `git archive <commit> daemons/hams_local_relay` (plus hams_open's `daemons/ham_digital_modes` in
  a sibling directory, so the symlink resolves). Then run the job's steps in a `docker run --rm
  --cpus N --memory Ng ubuntu:24.04` container and fix each new error in turn. Found this way on
  2026-09-15: `build-raspbian` doesn't run `install_debian_deps.sh`, so it needs the host tools
  (pkg-config, cmake, autotools) listed itself, plus host `libhamlib-utils`. Without that package,
  build.rs bakes an empty rig model table into the armv7 binary, and the build still succeeds.
- **The ubuntu:20.04 relay leg links Hamlib 3.3, not 4.x.** Ubuntu 20.04's libhamlib is 3.3.
  `install_relay_runtime_deps.sh` builds static 4.7.2 only for mercury, and the relay itself
  still links the distribution library. So relay code and tests must stick to what 3.3 has.
  Three differences surfaced on 2026-09-15, the first time that leg reached Run Tests:
  (1) `rig_get_conf2` doesn't exist, and not in Ubuntu 22.04's 4.3.1 either (the 22.04 leg hit
  the same link error once its Formatting step passed). The test binary fails to link with
  `undefined symbol`, while the release build still links, because only a test called it. Use
  `rig_get_conf` with a buffer of at least 128 bytes.
  (2) Rig model numbers are `backend*100+n` (IC-7300 = 373), not 4.x's `backend*1000+n` (3073).
  build.rs bakes the matching table, so tests take the number from
  `hamlib::linked_hamlib_model_id(backend, n)` (and its `..._ic7300_...`/`..._ft847_...` wrappers)
  instead of a literal. **Any bare model-id literal in relay test code is wrong by construction**
  -- it can only be right on one side of the 3.x/4.x split. A second instance surfaced on
  2026-09-16 (run 35034581568, job 104614049499) after the first was fixed: the FT-847 in
  `set_ptt_returns_promptly_when_the_serial_port_is_wedged` was written `1001`, which is 101 on
  3.3, so `rig_init()` returned NULL and the test panicked with "Failed to allocate Hamlib RIG
  memory context". When a 20.04 test failure mentions that message, check the model id first.
  A `rigctl --list | grep <model>` in a throwaway `ubuntu:20.04` container resolves the real
  number in under a minute, without building anything. Fixed in hams_com `339ab491`.
  (3) The dummy rotator's azimuth range is -180..180 (also in 4.3.1), not -180..450 (4.6.2+).
  Distribution Hamlib versions: Ubuntu 20.04 3.3, 22.04 4.3.1, 24.04 4.5.5, Debian 12 4.5.4.
  Each release's source tarball is on Hamlib's GitHub releases page, for checking API questions.
  (4) Hamlib 4.3.1's dummy rig stores `rig_set_split_freq()` in a side field that
  `rig_get_split_freq(rig, VFO_B)` never reads, because that call reads VFO B through `get_freq`.
  The split-TX safety test therefore sets VFO B's frequency too. Upstream fixed the dummy by
  4.5.5. The relay's `get_tx_freq()` is correct for real radios.
  (5) Hamlib's CW queue (`fifo_morse`, `resetFIFO`) and the dummy backend's `stop_morse` both
  first appear in **4.6**. On 3.3, 4.3.1, 4.5.4 and 4.5.5, `rig_send_morse` is synchronous, and
  `rig_stop_morse` against the dummy backend returns `-RIG_ENAVAIL` (-11). So a relay test written and
  passed on the dev box's 4.6.2 that expects a working `rig_stop_morse` fails on the 22.04, 24.04 and
  pi500 legs (run 35082588535, 2026-09-16). The dev box's own Hamlib is newer than every CI leg
  except build-windows, so any Hamlib behavior a new test relies on needs a check against the
  4.3.1/4.5.x tag sources (`raw.githubusercontent.com/Hamlib/Hamlib/<tag>/src/rig.c`, one `curl`).
  In a local container run, `callbook::tests::has_adequate_space_matches_a_real_independent_df_call`
  fails if the disk under `/var/lib/docker` changes free space mid-run. It compares a sysinfo
  reading against a later `df`. Check `df -h /var` before blaming code.
  To check a 3.3 question, the 3.3 source tarball is on the Hamlib GitHub releases page (tag
  `3.3`). A full `cargo test` for this leg runs in an `ubuntu:20.04` container: install
  `install_debian_deps.sh` and `install_relay_runtime_deps.sh`, then run cargo with a scratch
  `CARGO_TARGET_DIR`. The build takes about 7 minutes, the tests about 5.
- **Ubuntu 22.04's Hamlib 4.3.1 registers only the FIRST rig backend a process asks for**, unless
  `rig_load_all_backends()` is called first. This is a *different* bug from the model-id split
  above, and it looks exactly like it in a log -- both surface as `rig_init()` returning NULL and
  the relay's own "Failed to allocate Hamlib RIG memory context". Don't conflate them. Confirmed
  with a throwaway C probe linked against each distribution's own libhamlib, which is the fast way
  to settle any question like this (about two minutes per version, no Rust build): under 4.3.1,
  `rig_init(1)` (dummy) succeeds and a following `rig_init(1001)` (FT-847) returns NULL with
  `rig_check_backend: rig model 1001 not found and rig count=41`; after one
  `rig_load_all_backends()`, dummy, FT-847 and IC-7300 all succeed. Version-specific: 3.3 (20.04),
  4.5.4 (Debian 12, the pi500 leg), 4.5.5 (24.04) and the dev box's own 4.6.2 all load a second
  backend on demand, so **only the 22.04 leg can ever catch this, and a green local run proves
  nothing about it**. Both `rig_load_all_backends()` and `rot_load_all_backends()` exist and return
  `RIG_OK` on all four versions including 3.3, so calling them is safe everywhere -- check that
  before adding any Hamlib call, since 3.3 predates a lot of the 4.x API. Fixed in hams_com
  `5983fd93` (`hamlib::load_all_backends_once()`, a `std::sync::Once` called under
  `HAMLIB_CALL_LOCK` by both `RadioInterface::new` and `RotatorInterface::new`).
  **When a CI fix comes with a new regression test, prove the test is a guard**: run it once with
  the production fix removed and confirm it fails, in the same container, before committing. The
  first draft of this one called the loader itself instead of going through
  `RadioInterface::new()`, which would have passed with the fix deleted -- and, because the loader
  is a process-wide `Once` and the test sorted first under `--test-threads=1`, would also have
  pre-registered every backend and stopped the ORIGINAL failing test from catching the regression.
  A test that only passes after the fix is not the same thing as a test that fails without it.
  The general lesson, which is why this is worth keeping: the 22.04 leg spent a long time red at
  `Clippy`, so `Run Tests` never ran there. **The first time a CI leg gets past a long-standing
  earlier failure, treat its later steps as untested code paths, not as regressions** -- expect new
  failures and budget for them, rather than assuming a failure on a leg that "used to be fine" is a
  regression from the most recent commit.
- **`build-raspbian` was retired in hams_com `775892a3`** (2026-09-16): the 32-bit armv7
  cross-compile leg is gone, replaced by the native arm64 `build-linux` leg on `pi500-1`. Notes in
  this file that mention `build-raspbian` are history now; don't go looking for that job.
- **Wine GUI installers need a virtual display in CI.** Inno Setup's `innosetup-*.exe
  /VERYSILENT` still creates a window. With no X display it exits 1 without printing anything
  under `WINEDEBUG=-all`. Pass `/LOG="C:\x.log"` and read the log from the Wine prefix to see the
  real error ("Invalid window handle", code 1400). The fix is `xvfb-run -a wine ...`, with `xvfb
  xauth` installed. `ISCC.exe` itself runs headless without it. The dev box has a live desktop
  session, which is why local runs there don't hit this.
- **A cross target must be listed in `daemons/hams_local_relay/rust-toolchain.toml`.** That file
  pins the compiler, and `dtolnay/rust-toolchain`'s `targets:` only installs a target for *stable*.
  Cargo inside that directory uses the pinned toolchain, and fails with "can't find crate for
  `core`". armv7 and x86_64-pc-windows-gnu are listed. When bumping `channel`, keep the list.
  Also, unix-only APIs (`tokio::signal::unix`, `std::os::unix`) need `#[cfg(unix)]`; `build-windows`
  is the only CI leg that catches an ungated one.
- **`cargo fmt --check` runs only on the ubuntu-22.04 leg** (with clippy). Check that leg's
  Formatting step specifically. In the shared working tree, another session may have formatted a
  file and not committed it. Compare `git hash-object` of the working-tree file against your own
  formatted copy before committing, so you don't overwrite someone's unrelated edits.
- **Check relay formatting on every run; don't wait for CI.** Feature commits often land tested
  and clippy-clean but not rustfmt-clean. There were three rustfmt-only fix commits on 2026-09-15
  (`ff6bd117`, `984bf172`, `42e2833a`), and CI reports it only when the 22.04 leg reaches
  Formatting, which can be hours later. Takes seconds:
  `git archive origin/main daemons/hams_local_relay | tar -x -C <scratch>`, then
  `cargo fmt -- --check` inside the extracted crate. The archive carries `rust-toolchain.toml`, so
  the pinned rustfmt runs. Check the committed snapshot, not the shared working tree, which often
  holds a peer's unformatted work in progress. To pick a toolchain explicitly, prefer
  `rustup run <channel> cargo fmt -- --check`: a `cargo +$ch` built from a shell variable silently
  breaks when the variable holds more than one line, which is what a `grep` for `channel` in
  `rust-toolchain.toml` returns, and cargo then reports a confusing "no such command". To fix,
  format the snapshot and commit those blobs through a private index (see the shared-index note
  below).
  **On the ubuntu-22.04 leg, Clippy runs BEFORE Formatting, so a red clippy reports Formatting as
  `skipped`.** Never read a skipped Formatting step as a passing one: every step after the failure
  is skipped, so a rustfmt fix stays unverified until clippy is green on that leg. Found 2026-09-15,
  when run 35030820868 verified `42e2833a` this way and a watcher polling only for the Formatting
  step's conclusion got `skipped` and nearly recorded it as a result. Read the leg's failing step
  list, not one step's conclusion.
  `run_linters.py` step 52 (`check_cargo_fmt.py`) runs the same check across every crate in both
  repos. The relay's own `cargo fmt --check` doesn't cover `ham_digital_modes`, a path dependency
  rather than a workspace member.
  **This check also catches a missing-file build break, not just formatting, and far earlier than
  CI does.** On 2026-09-16 (run 30) it failed with ``failed to resolve mod `offline_records` `` --
  `main.rs` on `origin/main` declared a module whose file had been deleted, so the relay could not
  compile at all. Cause: a commit used `git add <paths>` and then a BARE `git commit`, which takes
  the whole shared index, and so carried another session's staged `git rm` of that file, unmentioned
  in its message. Two heads were pushed on top before anyone noticed. So when this check reports
  anything other than a formatting diff, read the actual message rather than assuming rustfmt: an
  unresolved `mod` means the pushed tree is uncompilable and every queued run on it is doomed.
  The right response is to cancel those queued runs (they can only burn a full matrix on Bruce's
  laptop ahead of a run that can pass) and to make sure nobody records a fix as verified by a run
  whose head cannot build -- `git merge-base --is-ancestor <fix> <headSha>` is necessary but not
  sufficient for that.
- **A step-less, runner-less `Security audit` job in `Build Server Daemons`** is the check-run
  `rustsec/audit-check` creates itself. It mirrors the matrix `security-audit` jobs' result and has
  no log of its own. Read the matrix job's `RustSec advisory check` step instead.
- **`EACCES: permission denied, stat '/home/bruce/.local/bin/git'` lines in hams-devbox job logs
  are noise, but the PATH that causes them isn't.** They are not the workspace-ownership `EACCES`
  described above. The runner's saved `PATH` (`/home/github-runner/actions-runner-hams-com/.path`)
  was captured from Bruce's shell, so it lists `/home/bruce/.local/bin`, `.cargo/bin`, `go/bin` and
  Claude plugin directories ahead of `/usr/bin`. Only `/home/bruce/.local` and `.config` are mode
  700, so the EACCES lines are just those entries, and `git` falls through to `/usr/bin/git`.
  **`/home/bruce/.cargo/bin` and `go/bin` are reachable**: both host-side `security-audit` jobs ran
  `/home/bruce/.cargo/bin/cargo audit`, meaning Bruce's own rustup and cargo-audit. The first fix
  (hams_com `e1934525`) still let `dtolnay/rust-toolchain` run Bruce's rustup. That action installs
  rustup only when `command -v rustup` finds none anywhere on PATH, so a stale entry is enough to
  skip it. The jobs now install the runner account's own rustup first (hams_com `f22e97c0`). Any
  new host-side Rust job needs that same pre-step, not just `dtolnay/rust-toolchain`. An earlier run
  of this skill called the whole PATH harmless after reading one EACCES line. Check reachability as
  the runner account instead: `sudo -n -u github-runner test -x <dir>`. A host-side job's
  `[command]` log lines show which binary actually ran. `runsvc.sh` exports `.path` once at service
  start, so editing it does nothing until `actions.runner.BrucePerens-hams_com.hams-devbox.service`
  restarts (only while no job runs). Re-running `config.sh` from Bruce's shell regenerates it with his
  PATH. Any new host-side job that needs cargo must provision it (`dtolnay/rust-toolchain@stable`)
  and not rely on `.path`. **`.path` was cleaned on 2026-09-15** to system directories only
  (backup `.path.bak-2026-09-15` beside it), and `build-server-daemons.yml`'s host-side
  `build-and-test` job got the same runner-rustup pre-step (it had been missed). If a job log
  shows `/home/bruce/...` again, `.path` was regenerated, most likely by re-running `config.sh`
  from Bruce's shell.
- **An offline `hams-devbox` runner is usually an OOM kill, and it looks like a code failure in the
  job log.** The dev box has 15.3 GB of RAM and 16 GB of swap, shared by a dozen concurrent Claude
  sessions, an Odoo test server, and whatever CI container is compiling at the time. On 2026-09-16
  (run 29 of the hourly task) the kernel's OOM killer killed the runner's own `docker` child while
  the ubuntu:20.04 leg built Hamlib 4.7.2 from source, and the whole runner service died with it;
  swap was 100% full (16,076 of 16,077 MB). **The failing job's log says nothing about memory** --
  it ends with `##[error]Process completed with exit code 137` (SIGKILL) and
  `##[error]The runner has received a shutdown signal`, which reads like someone stopped the runner
  or like a broken build step. So whenever a step dies with exit 137, or a run sits `queued` with no
  job starting, check the host before reading the step as a regression:
  `systemctl status actions.runner.BrucePerens-hams_com.hams-devbox.service` (look for
  `Result: oom-kill`), `sudo -n dmesg -T | grep -i oom-kill`, `free -m` (swap exhausted?), and
  `gh api repos/BrucePerens/hams_com/actions/runners --jq '.runners[] | "\(.name) \(.status)"'`.
  Note that `gh run list` reports such a run as `queued` at the run level while some of its jobs
  already show `completed failure`, so read the job list, not the run's own status.
  **Restarting the runner does not re-run the job the kill took down.** GitHub replays only the
  jobs still `queued`; one already recorded `completed failure` stays failed, so whatever that leg
  was the only leg able to verify is still unverified. Check which legs actually re-ran before
  telling anyone a fix is confirmed -- this exact mistake was made and corrected on 2026-09-16 about
  `339ab491`, whose 20.04 leg is the only one that can catch the Hamlib 3.x/4.x model-id split. The
  cheap close is to let the next push under `daemons/hams_local_relay/**` carry it, since that run's
  own legs verify the fix for free; `gh run rerun --job <id> --repo BrucePerens/hams_com` works too
  once the run reaches `completed`, but re-triggers whatever step caused the OOM, so check `free -m`
  for real swap headroom first.
  **The runner service now survives an OOM kill outright**, via a drop-in at
  `/etc/systemd/system/actions.runner.BrucePerens-hams_com.hams-devbox.service.d/restart-on-oom.conf`
  (its full text is also reproduced in hams_com's `docs/runbooks/06_ci_cd_pipeline.md`, since `/etc`
  is not in git). It carries two settings, added an hour apart on 2026-09-16 and both confirmed live:
  `Restart=always`/`RestartSec=15` (the unit GitHub's own `svc.sh install` generates has no
  `Restart=` line at all, which is why one killed job left CI dead for an hour; the journal later
  showed it restarting 15s after a second kill and draining the rest of the run green on its own),
  and `OOMPolicy=continue`.
  A drop-in file sitting on disk is not proof either setting is in force -- it needs a
  `systemctl daemon-reload`. Confirm the loaded values with `systemctl show
  actions.runner.BrucePerens-hams_com.hams-devbox.service -p OOMPolicy -p Restart -p NRestarts`.
  **`OOMPolicy` is the one that matters most, and the reasoning behind it
  is the durable fact**: both times, the process the kernel actually killed was the job's own
  `docker` CLI child holding **10 MB**, with `oom_score_adj:500` -- the Actions runner sets 500 on a
  job's children on purpose so a runaway job is sacrificed first, but 500 adds half of total RAM to
  the kernel's score, making that 10 MB process the preferred victim under *any* global memory
  pressure, whatever caused it (the other victim in the second report was a 5 GB Chrome renderer in
  the Claude desktop app's own cgroup). systemd then defaults services to `OOMPolicy=stop`, which
  stops the WHOLE unit when any process in its cgroup is OOM-killed -- so a job-scoped kill tore
  down the entire runner. So **the expected shape of this problem is now a job failing with exit 137
  while the runner stays online**; a runner that is genuinely *offline* means something neither
  setting covers, and is worth investigating rather than just starting. Neither setting prevents the
  OOM: that is a real capacity problem tracked in hams_com's `night_shift_todo/high/`. Its container
  side is handled since hams_com `19aa0611` -- every job-level `container:` in `build-relay.yml`
  carries `--memory=6g`, so an over-budget build is killed inside its own container instead of
  starving the host. Two cautions: only start the service when no job is running on it (an
  OOM-killed service is safely dead, so that case is fine), and don't reap the memory hogs you find
  -- stray `rust-analyzer` instances held ~2.9 GB that run, and they may belong to a live peer
  session's editor.
- **Installer publishes never sign, and that is by design, not a wiring bug.**
  `publish_relay_binary.sh` signs its zip with zipsign and logs
  `RELEASE_SIGNING_KEY is set -- signing ... with zipsign before publish`, and an unsigned binary
  publish is an error. `publish_relay_installer.sh` -- the `.deb`, `.rpm` and Windows `.exe` path to
  `/shack/download/<platform>` -- has no signing code at all, and `build-relay.yml` never passes it
  `RELEASE_SIGNING_KEY` (verified 2026-09-16 against `origin/main`: the secret appears only on the
  four `publish_relay_binary.sh` steps). Its own header comment explains why: an installer is
  already the single file an operator runs, so there is nothing to bundle and sign around. So the
  absence of a signing line in an installer publish step's log is expected -- don't read it as a
  regression against the binary-publish rule. OS-native installer signing (Authenticode, `.deb`/
  `.rpm` signatures on the direct-download route) is a real, separate, already-documented gap:
  hams_com `docs/proposals/blocked/RELAY_SUPPLY_CHAIN_SECURITY.md` section 1.5, "OS-native installer
  signing -- a real gap zipsign does not cover".
- **`hams_com` has no `ODOO_URL` secret yet** (no production Odoo exists; `https://hams.com` timed
  out on 2026-09-15). Every publish step on `main` (binary zips, .deb, .rpm, apt repo, Windows)
  fails at `curl "$ODOO_URL/..."` with an empty URL once its build is green. That is expected before
  deployment, not a new bug. Check `gh secret list` fresh. `HAMS_RELAY_SOURCE_PUBLISH_KEY` does
  exist (set 2026-09-15). It is a random shared key a session generated; its only copy outside
  GitHub is the file named after it in `~/.secrets/hams_com_ci/`, and the Odoo server's
  `ham_relay_bridge.source_publish_key` system parameter must be given the same value when the
  server exists. Sessions may generate and set such CI-only shared secrets themselves: `gh secret *`
  is in `permissions.allow`, and piping the key from a file keeps it out of the transcript. The
  release-signing keys (`RELAY_RELEASE_SIGNING_KEY`, `APT_REPO_SIGNING_KEY`) used to be excluded:
  no session was to generate them. **Bruce lifted that rule on 2026-09-15.** A session may generate
  both keypairs and pipe each private half straight into `gh secret set` without reading it. The
  full procedure and current state are in hams_com's
  `night_shift_todo/low/release-signing-keys-decision-*.md`, and Bruce's answer is under
  `night_shift_questions/answered/`. `RELAY_RELEASE_SIGNING_KEY` was generated and set on 2026-09-15
  (its public half is `updater.rs`'s `RELEASE_VERIFYING_KEYS` and `packaging/release-signing.pub`).
  The GPG key comes from `daemons/hams_local_relay/packaging/linux/generate_repo_signing_key.sh`,
  run once. The permission classifier denies raw `gpg` key generation, so that script needs its own
  allow rule from Bruce. Never ask a peer to run it. `publish_relay_binary.sh` base64-decodes
  `RELAY_RELEASE_SIGNING_KEY` and requires 64 bytes (hams_com `9c68a521`), because `printf '%s'`
  truncated a raw key at a NUL byte. So set that secret base64-encoded
  (`base64 -w0 release.key | gh secret set ...`). The GPG key is armored text and needs no encoding.
  **A job only sees a secret that existed when the job started.** `gh secret list` prints each
  secret's last-update time. Compare it with the job's start time before calling an empty secret a
  wiring bug. In run 35008066571, the windows and raspbian publish steps started at 19:06 and 19:15
  UTC and logged `RELEASE_SIGNING_KEY is not set`. The secret was set at 19:29, so that was expected.
  **With the secrets set, each publish step signs before it reaches the expected `curl: (3)`.** A
  publish step that fails earlier, in `cargo install zipsign`, `rpmsign`, or `gpg --import`, is a
  new failure, not the missing-`ODOO_URL` case. The binary publish log should show
  `RELEASE_SIGNING_KEY is set -- signing ... with zipsign before publish.` An unsigned binary publish
  is now an error, not a warning. The YUM script signs the `.rpm` itself with `rpmsign` (job needs
  `rpm-sign`), because `add_yum_repo.sh`'s `gpgcheck=1` rejects unsigned packages.
  **Read the publish step's last log line before calling it the
  expected failure.** The expected one is `curl: (3) URL rejected: No host part in the URL`
  (exit 3). The `.deb` job's `ubuntu:22.04` container prints the same empty-URL error differently,
  as `curl: (3) URL using bad/illegal format or missing URL`, because its curl is 7.81 and the
  other containers have curl 8.x. This was reproduced locally on 2026-09-15.
  **On a day when every build leg is green, exactly six jobs still show `failure`, and that is
  the whole expected picture**: `publish-source`, `publish-linux-binary`,
  `publish-linux-aarch64-binary`, `build-linux-deb-package`, `build-redhat-rpm-package`, and
  `build-windows`. The last is the trap -- build-windows publishes inside its own build job
  (failing step `Package and publish binary`, not `Publish binary`), so a red build-windows
  sitting among green build legs is not a build failure until you have read its last log line.
  Read all six, not a sample: they are the only jobs that can hide a real failure on such a day,
  and their publish paths genuinely differ: the binary jobs run `publish_relay_binary.sh` (which
  signs with zipsign), the `.deb`/`.rpm` jobs run `publish_relay_installer.sh` (which never signs,
  by design), and each container brings its own curl and its own prerequisites. Confirmed
  2026-09-16, run 33, across runs 35056345011 and 35057629639: all six reached a `curl: (3)`
  variant, the three binary ones after logging their zipsign signing line.
  An exit 127 (`zip: command not found`, `jq: command not found`) means the job's
  container lacks a tool the publish script needs, and the leg could not publish even with
  secrets. build-windows hit exactly that on 2026-09-15, after hours of being written off as the
  no-secrets case. Fixed in hams_com `a4252487`. A new container job that runs
  `publish_relay_*.sh` needs `zip` (binary script only) and `jq` (all four scripts) in its
  prerequisites. Two more non-`curl: (3)` publish failures, found the first time every build leg
  went green (run 34932262419, 2026-09-15, fixed in hams_com `ff3f02b4`). (1) Exit 126
  "/usr/bin/jq: Argument list too long": a base64 payload passed with `jq --arg`. The command line
  is capped by ARG_MAX (2 MB on the dev box), and a zip, .deb or .rpm is larger. Always stream
  base64 to a file and use `jq --rawfile`. This bug lived on in inline workflow steps and the
  APT/YUM repo scripts after `publish_relay_binary.sh` was fixed, so grep `.github` for
  `--arg .*base64` whenever you touch one. (2) `publish-linux-binary` refusing with "librade.so.0.1
  not found next to ...": the `linux-binary` artifact must carry `librade.so.0.1` along with the
  executable.
- **The hams_com git index is shared too, not just the working tree, and the rule for that now
  lives in an ADR.** Read `hams_shared/docs/adrs/0099_shared_working_tree_commit_discipline.md`
  before your first commit of a run: it is the authoritative statement of how to commit here (never
  a bare `git commit`/`-a`/`-A`; `git commit -- <path>` is NOT the safe form, because it commits
  that path's working-tree content including a peer's uncommitted edits; use a private
  `GIT_INDEX_FILE`, and do its mandatory resynchronisation step afterwards, which is the step people
  skip). It is deliberately not restated here -- two copies of a procedure are how the two copies
  drift apart. The ADR was written 2026-09-16 (run 30) precisely because this material had lived
  only in this skill's operating notes, so sessions running a different skill kept rediscovering the
  same failure; if you find a new instance, add it to the ADR, not here.
  What is specific to THIS skill, and worth knowing before you read a CI failure:
  **a shared-index accident can push an uncompilable tree, and it looks like a code regression.**
  On 2026-09-16 a peer's staged `git rm` of `daemons/hams_local_relay/src/offline_records.rs` rode
  out under an unrelated commit, leaving `main.rs` declaring a module with no file; two more heads
  were pushed on top, and both queued a full relay matrix that could only fail at compilation. So
  when several consecutive heads fail, check whether the pushed tree even builds before attributing
  it to any commit's own content -- the relay formatting check above is the fastest way.
- **Before pushing hams_com, run `git log --oneline origin/main..main`.** Another session may
  have committed on `main` and be holding the push on purpose. For example, a relay change can wait
  until a local test run finishes, because pushing anything under `daemons/hams_local_relay/**`
  queues a full relay CI matrix on the dev box. Pushing your commit would push theirs too. If
  the list shows commits that aren't yours, commit locally and don't push. Use `ListAgents` and
  `SendMessage` to find the owner and agree that it pushes your commit along with its own. Note
  that in your to-do's own file under `night_shift_todo/`. This happened on 2026-09-15, eighth hourly
  check (hams_com `afd538ff`).
- **Before recording a dependency bump as "breaking, needs Bruce", grep the codebase for its real
  call sites.** An advisory names the vulnerable APIs, and a changelog shows breaking changes, but
  neither tells you what this codebase calls. hams_com alerts #3/#4 (crossbeam in the vendored
  `reference/ambe/imbe.rs`) stayed filed as Bruce's call from 2026-09-14 until the sixteenth hourly check on 2026-09-15, because crossbeam's
  `MsQueue`/`SegQueue` API changed a lot. The crate never used those. Its only call was
  `crossbeam::scope`, and the 0.8 bump was a two-line change (hams_com `ed40b779`). Also re-check a
  standing "needs Bruce" item whenever a run has time, rather than copying it forward. That crate
  needs a nightly toolchain. `cargo +nightly test` passes in debug as well as `--release` since
  arrayvec 0.3 -> 0.7 (2026-09-15, seventeenth check). None of its tests call `decode`, and
  `decode` is seeded by the OS (`rand::weak_rng()`). To check a dependency bump there, compare
  decoder output from fixed-seed scratch copies. Its `VENDORED.md` gives the procedure.
- **A commit message saying "Fixed #1" closes GitHub issue or PR #1 when pushed.** GitHub reads
  `fix`/`fixes`/`fixed`/`close`/`closes`/`closed`/`resolve`/`resolves`/`resolved` followed by `#N`
  as a closing keyword, even when `#N` means an item in the commit's own list. Dependabot PR #1
  (hams_com) was closed this way by unrelated commit `98b4d84e`. Dependabot then commented that
  it would stop proposing that release. A closed Dependabot PR whose `closed` event has a
  `commit_id` was closed by a commit, not by anyone deciding
  (`gh api repos/BrucePerens/<repo>/issues/<n>/events`). In commit messages, number items as
  "item 1" or "(1)", not "#1".

### Standing decisions from Bruce -- act on these, don't ask

Bruce answered these on 2026-09-14 and asked that they be recorded here so no future session asks
him again. They override any older text in this file that says to defer such items or record them
as needing his judgment.

1. **Update dependencies when you can.** Stale pins flagged by `Dependency Release Watch` (ardopcf,
   mercury, cloudflared, kopia, etcd, pat, hamlib) and Dependabot alerts with an available fix are
   work for this run, not "for later". Verify each bump for real (build it, run the relay's tests
   against it, checksum release binaries, per the entry's `notes`), update `pinned` and `notes`,
   commit and push. A bump that genuinely breaks something you can't fix in the same run gets
   recorded with the exact error -- that's a real blocker, not a preference question.
2. **When upgrading Rust, fix the new clippy errors** the newer toolchain introduces, in the same
   change. That covers bumping `daemons/hams_local_relay/rust-toolchain.toml` to a new stable, and
   the unpinned server daemon crates that pick up new stable lints automatically. Fix the code the
   way clippy suggests (behavior-preserving); use a targeted, commented `#[allow]` only where the
   lint is genuinely wrong for that code. Never weaken or delete tests.
3. **When a distribution lacks a tool the relay needs, download and build it from source** in the
   install script, rather than dropping the platform or asking. Pin the exact version or commit,
   verify downloads by checksum or commit hash, and keep build toolchains inside the temporary
   build directory. Example: `install_relay_runtime_deps.sh` builds pat at the version
   `dependency_watch.json` pins, with a checksum-pinned official Go toolchain, because Ubuntu 20.04
   has no `pat` package and other distributions ship an outdated one. When pat's pin is bumped,
   update `PAT_VERSION`/`PAT_COMMIT` there too, and the Go version and checksums if pat needs a
   newer Go.
4. **arm64 CI builds run natively on `pi500-1`** (see the operating-knowledge note above), not under
   emulation.

Still Bruce's (these involve money, accounts, or product direction): re-scoping or handling his own
existing credentials, and dismissing Dependabot alerts on GitHub. CI-only secrets a session creates
and controls itself (random shared keys, and since 2026-09-15 the release-signing keypairs) are not
on this list. Re-check any "still Bruce's" label against a primary source every run, rather than
copying it forward. The macOS leg is no longer a billing item:
Bruce postponed it until the self-hosted Mac arrives, and hams_com `2520690c` sets `build-macos`
to `if: false`. Don't report its skipped job as a failure.

## Part 2: The structured to-do queue

Bruce's own instruction, 2026-09-15, after a session watched multiple independent hourly
`dependabot-and-ci-watch` runs coordinate real work all evening: `night_shift_todo.md` had grown
past 8,900 lines of free-form prose, genuinely hard for any session to scan for "what's actually
still open" without a full read-through -- exactly the failure mode that once caused a claimed
"done" backlog to turn out to have 20 unsorted files still sitting in it. His fix, given directly:
"put each to-do in a separate file and make it a directory... make priority directories: critical,
high, medium, low." This skill is the resulting convention.

### Where it lives

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

### File format

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

### Workflow

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
Every night-watch run (step 3 of "A run, in order") checks `night_shift_questions/answered/`
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

### Relationship to other systems -- don't conflate these

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

### Waiting for the shared Odoo test runner

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

### A load gate now runs BEFORE the lock, so a busy box delays you instead of failing you

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

### A third pre-flight check has NO skip flag, and it blocks every session, not just yours

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

### Ask the peer before picking an item in a module they have uncommitted work in

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

### Two linter/claims tools whose default invocation silently reports nonsense

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

**The module-local store has its own full-hash recipe.** Claims under a module's own `claims/`
directory (hams_com, e.g. `ham_club_management/claims/`) are checked by
`check_claims_freshness.py <module>`, which also prints only 12 characters. Its
`compute_function_hash(<path to the .py file>)` returns `{anchor_name: full sha256 hex}` for that
file, keyed by the anchor exactly as written (no prefix stripping), so paste
`sha256:<value>` straight into `code_hash:`. Run it only after confirming `git diff HEAD` on that
`.py` file is empty, or a peer's uncommitted edit gets hashed into your claim (2026-09-16,
workspace-98).

### Two claim files can share a basename across modules -- name the anchor, not the file

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

### "The module's suite is green" does not mean its tests ran

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

### A status outside the four defined values escapes every query, not just its own

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

### Gate the commit on the edit's exit status, not just on the edit asserting

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

### Say so BEFORE filing, when two sessions are reading the same checker's output

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

### A fix that changes behaviour under test will be refused by the burn list

Worth knowing before designing one. Working the `distributed_redis_cache` poll-gate item on
2026-09-15, the first draft kept the suite's existing behaviour by excluding test processes from the
new gate (`not tools.config.get("test_enable")`). `check_burn_list.py` rejected that outright, by
AST and by regex, as the test-evasion pattern it exists to forbid -- correctly: a gate that reads
`test_enable` leaves the production path untested by construction. The honest fix was to let the
suite take on the real behaviour, which here meant every HTTP request and cron dispatch polling
Redis exactly as a serving worker does. Run `check_burn_list.py <module>` early, while the design
is still cheap to change, rather than after the tests are written.

### A new anchor must satisfy two separate checks, and the obvious fix for one breaks the other

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

### Let clippy's dead-code pass tell you a refactor left a layer behind

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

### Raise a new guard in an `else:` clause, not inside a broad `try:`

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

### A correctly-written test can leave no evidence in the log

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

### `git add <paths>` then a BARE `git commit` takes the peer's staged work too

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

### A scoped pathspec protects what you commit, not what a peer sweeps

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

### After deleting a caller, ask what still calls the callee

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

### `blocked_on:` may point at a to-do, not only at a question

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

### Two scheduled workers on one schedule converge on the same item

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

### Re-check the to-do still exists before appending its history entry

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

### "Copy X exactly" is a snapshot of X, not a live reference

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

### A checker can report nothing because it never looked

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

### Writing a new linter rule: its own tests prove almost nothing

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

### Fixtures built from documented behaviour pass while the code is wrong

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

### Two scripted-edit and commit traps that each passed their own check

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

### Three traps from one worker run's verification (2026-09-16, workspace-f9)

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

### Formatting only your own relay files while peers have uncommitted edits in the same crate

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

### A peer's run "loaded your code" is not a run of your tests

On a shared tree, a peer's test run often starts after your edits land and imports them. That is
worth knowing, and it gets offered as evidence. On 2026-09-16 workspace-f9 reported that its
`-u ham_onboarding,ham_testing` run had picked up workspace-fc's LoTW controller change and that
`test_lotw_consume` had "no failures". True, and it verified nothing new. The run had collected the
test module a minute before the new `test_07*` tests were written, so the log named every old test
in that file and none of the new ones. A green result for a test file is a claim about the tests
that were in it when the run collected it.

Before counting someone else's run as your verification, grep its log for the NAMES of the tests
your change added (`grep -o "Starting .*test_07[a-z_]*"`), not just for the file or the module.
If they are absent, the run has confirmed only that the old tests survive your change, which is
still useful. Say which of the two it was when you record it.

### The load gate can outlast its whole timeout behind a CI matrix

A relay push queues a build matrix on the dev box, and the load gate waits on a live
`Runner.Worker` whether or not that job is using much processor time. On 2026-09-16 a
`-u ics_forms` run sat for the full 3600 seconds of `HAMS_CI_LOAD_GATE_TIMEOUT` while one matrix
job after another started (the pid changed from 43674 to 271432 during the wait). It then exited 1
without starting Odoo. The one-minute load average was around 3 on 16 processors for part of that
hour, and at other times six mingw `cc1` processes were running.

Three consequences for a worker run:
- An exit 1 whose log's last line is `the box is still too busy to test on after N seconds` is not
  a test result. Re-queue it. Don't record the item as failing, and don't bypass the gate just
  because the load average looks low at the moment you check: the matrix alternates idle steps
  with heavy compiles.
- A relay push early in a run makes that run's own Odoo verification slower. Where a run has both
  kinds of item, queue the Odoo run before pushing relay code, not after.
- Write each claimed item's state into its body while the run is still waiting, not only once it
  finishes: exactly what is written, whether it is committed, and which test is expected to fail.
  If the session ends mid-wait, that note is the only record of uncommitted work sitting on a
  shared tree.

### A lint scan from hams_com and one from hams_open are not the same scan

`hams_shared` is a real directory inside hams_open but a symlink inside hams_com, and `os.walk`
does not follow symlinks. So `check_burn_list.py . --scan-daemons-and-tools` from hams_com never
reads `hams_shared/tools/`, while the same command from hams_open does -- and that is exactly the
scan `test.py`'s multi-module pre-flight runs. On 2026-09-16 (workspace-4a) this was why a
"whole-repo scan was clean" note sat next to a hams_open pre-flight that halted on 37 errors, six of
them added that morning by a worker's own new tests (imports inside test methods). **Before pushing
anything under `hams_shared/tools/`, run the whole-repo scan from hams_open**, not only pytest and
not only from hams_com.

Related trap from the same run: a checker given a path that does not resolve may report success.
`check_claims_freshness.py` did (fixed in hams_shared `84ab746`; it now exits 2), after a `cd`
earlier in the same command line had moved the working directory. Read the checker's own output for
the count of things it looked at, or run it from an absolute path.

### A peer's failure in an unrelated module can be YOUR uncommitted change

Every test run loads the shared working tree, so a peer's run executes your uncommitted edits too,
and a failure they cause shows up in a module the peer believes has nothing to do with you. On
2026-09-16 workspace-bb had an uncommitted `ham_onboarding` `_get_gdpr_export_data` override that
read `res.users.elmer_topics`. workspace-57's `-u ham_shack` run then failed
`test_gdpr_export_service_account` with `AccessError ... ham.elmer.topic`. It looked like an existing
access-rights gap on main, and workspace-57 was about to file a to-do for it. It asked first only
because workspace-bb had announced its claim, and the error turned out to be workspace-bb's missing
access grant.

Two habits follow:

- **When you claim an item, name the files you will leave uncommitted** in the message to peers, not
  just the item. A peer who sees a failure can then check its traceback against that list before
  filing anything.
- **When a peer reports a failure, grep its traceback for your own uncommitted paths and fields**
  (`git diff --name-only HEAD`) before saying it isn't yours. Here the traceback named neither
  `ham_onboarding` nor the override, only the relational field the override read. The check that
  settles it is whether your diff touches that field or model, not whether your module appears in
  the stack.

A peer's run that trips over your change is also free verification. This one proved a missing
access grant before the verification run for that item had even started.

### Changing an existing `ir.rule` or other security record: it is noupdate

Found 2026-09-16 (workspace-9b), narrowing `ham_events`' issue-report rule.

`check_burn_list.py` requires every `<record>` in a security file to sit inside a
`<data noupdate="1">` block. An upgrade (`-u`) never rewrites a noupdate record that already exists
in the database: `_load_records` skips any row whose stored `noupdate` flag is set. So editing that
rule's domain or permission flags in place changes a fresh install and nothing else. The shared
test database, and any upgraded database, keep the old rule. When the old rule granted more access,
Odoo ORs it with whatever you add for the same groups, and the narrowing silently does nothing.

What worked:
- Give the changed rule a NEW xml id, still inside the noupdate block.
- Remove the old record with `<delete model="ir.rule" search="[('name', '=', '<old name>'), ...]"/>`.
  Use `search=`, not `id=`: a missing id logs a WARNING with a traceback on every fresh install.
  `unlink()` also removes the old `ir.model.data` row, so the old id can't come back.
- Test the narrowed access as the restricted user. Know what that test can prove, though:
  `test.py` drops and recreates its database on every run (`DROP DATABASE IF EXISTS` in
  `test.py`), so a green run proves the fresh-install path only. It says nothing about whether the
  `<delete>` and the new ids behave on an upgraded database. The 2026-09-16 change was not verified
  on an upgrade. To verify one, install the module at the old commit and then `-u` it at the new one.

Two related traps from the same item:
- A rule with `(1, '=', 1)` for `base.group_user` covers administrators too, because they are in
  that group. Narrow it and administrators lose access, unless a separate rule for
  `base.group_system` (and each service group that reads the model) grants it back.
- Fields declared `groups="base.group_system"` (for example `res.users.is_service_account`) can't
  be read by any service account through the ORM, whatever its ACL. The record read fails with a
  403 in HTTP tests. Read them with parameterized SQL, as `event.event.action_handoff_ncs()` does.

And one about waiting: `pkill -f "<your waiter script name>"` matches the shell running `pkill` itself,
the same trap this file documents for `pgrep -f`. It killed its own shell with exit 144. Stop a
waiter by its PID, or use `pkill -f` with a pattern anchored the way the `pgrep` section describes.

## Recording


`night_shift_history.md`'s role is unchanged from before: the durable record of what already
happened -- completed fixes, resolved alerts, closed-out investigations. Bruce's own words on the
underlying distinction, which still holds: "the to-do list is for real to-dos, night_shift_history.md
is more appropriate for completed actions... if it's an 'I woke up and found no problems', just
delete those and tell the skill they are not worth archiving." The rules below apply to everything a run does, the queue work included.

Concretely, at the end of every run:

1. **Nothing new, nothing changed, nothing to fix, nothing open** (the routine "checked, all
   clear" case -- this is most hourly runs on a healthy day): **write nothing anywhere.** No new
   file in `night_shift_todo/`, no entry in `night_shift_history.md`. (If you want a record that
   the check happened at all, that's what the scheduled task's own run log/notification already
   provides.)
2. **Something was found AND fully resolved this run** (a real alert or CI failure, investigated,
   fixed, tested, committed, and pushed -- all three, per Part 2's "what done
   means" -- with nothing left open): append that summary -- what was wrong, what you changed, the
   real commit hash(es), and how you verified it -- directly to
   `/home/bruce/workspace/hams_com/night_shift_history.md` (re-read fresh immediately before
   editing, append at the end, purely additive). This is a completed action, which is exactly what
   that file is for.
3. **Something genuinely still needs attention** -- a real alert or failure you couldn't safely fix
   yourself, something that needs Bruce's own input (named specifically: which credential, which
   decision, which tradeoff -- never vaguely), or a fix you started but couldn't finish this run:
   file it as its own entry in `night_shift_todo/<priority>/`, per Part 2's
   file format. Pick the priority honestly -- a real, currently-exploitable-shaped security gap
   is `critical`, a routine "needs Bruce's answer on X" is usually `low` or `medium`.
4. **Close the loop on OPEN ITEMS THIS RUN CONFIRMS ARE RESOLVED.** Before writing anything new,
   check `night_shift_todo/critical/`, `high/`, `medium/`, `low/` (a cheap `ls`, not a full
   read-through) for items this run has now confirmed are actually done (a CI run you watched go
   green, an alert that's gone from `gh`'s own listing, a "still Bruce's" item he's since handled).
   Don't leave a stale claimed-but-actually-done item sitting there -- close it yourself: fold a
   summary into `night_shift_history.md` and `git rm` the to-do file, in the same commit as this
   run's other changes. This is the same failure mode the old single-file system had (entries
   sitting open long after being done, because writing and closing were treated as separate jobs) --
   the structured queue doesn't fix that on its own, the discipline still has to be applied.

Do NOT use the `spawn_task` chip mechanism for anything found here -- Bruce has explicitly asked
that this kind of tracking go directly into the appropriate file (per the three cases above)
instead.

## Constraints

- Never delete or weaken a test to make something pass.
- Never force-push, never rewrite published history.
- If `gh` itself is not authenticated or a repo API call fails for a real access reason (not just
  "no alerts"), report that plainly rather than silently treating it as "no findings."
- If this is a repeat run (via the hourly schedule) rather than a fresh manual invocation, don't
  re-litigate an alert or failure you already investigated and recorded unless its real state has
  changed (newly reappeared, got worse, or a fix you applied didn't actually stick).
- Never make a product, security or business decision yourself when a real, non-defaultable choice
  exists; file it in `night_shift_questions/open/`. When a proposal, ADR or the code already states
  a reasonable default, that default is the answer -- build against it.
- Never pick an item another active session is meaningfully working, and never batch unrelated
  to-dos into one commit.

## Self-improvement

This file is meant to improve itself. Bruce's instruction for the watch half: "modify the skill
itself with context it needs to start work, so that it can start more efficiently, but if there is
any sensitive data, it should store it in this publicly-exposed skill as a reference to a file in
hams_com, which is not publicly exposed." The same applies to the queue half. When a run discovers a
new, reusable fact about doing this work -- a permission-scope limitation, a recurring failure's
root cause, a coordination collision, a judgment-call boundary that was hard to draw -- add it to
the relevant part of this file in that run's own commit in hams_shared. Only add what a future run
would genuinely benefit from knowing. Never write a real secret here: hams_shared is public.

## Relationship to the scheduled task

One scheduled task serves this skill: `night-shift-todo-worker`
(`/home/bruce/.claude/scheduled-tasks/night-shift-todo-worker/SKILL.md`, titled "Night watch alarm").
Its prompt only performs "What the cron-fired session does" above and points here; this file is
authoritative. Keep the two consistent when the wake-up protocol changes. The former
`dependabot-and-ci-watch` task's prompt file may still sit on disk under
`~/.claude/scheduled-tasks/`, but that task is no longer scheduled -- don't treat its stale text as
current.

**Cadence changed to every 30 minutes (from hourly), Bruce's instruction, 2026-09-17**, given during
a live night-watch run when `list_scheduled_tasks` showed no task actually registered despite the
`night-shift-todo-worker` prompt file existing on disk -- the file and the live registration had
drifted apart. Recreated via `create_scheduled_task` with `taskId: night-shift-todo-worker` (same id,
so it lands back at the same path) and `cronExpression: */30 * * * *`. The wake threshold above (was
90 minutes) is now 45, keeping the same ~1.5x-the-cadence margin rather than diluting the
responsiveness gain by leaving it fixed while the alarm fires twice as often. If `list_scheduled_tasks`
ever again shows nothing where this section says something should be running, that is the signal to
recreate it, not to assume the file on disk means it is live.

Same conversation, Bruce's own follow-up: the awake case used to do nothing at all, which meant the
alarm's own stated job ("make sure step 2 gets re-checked periodically") only actually happened
while the agent was stalled -- a healthy, busy agent deep in one to-do item for hours would never be
reminded to re-check CI/dependabot until it happened to finish that item on its own. Fixed by having
the awake case send a periodic reminder too, not just the stale case a wake-up.

This skill lives in `hams_shared` (public) because it holds no proprietary content; it applies to
all three repositories. The queue and history files it writes live in private `hams_com`.
