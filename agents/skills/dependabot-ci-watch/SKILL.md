---
name: dependabot-ci-watch
description: >-
  Check all three real hams.com/hams_open/hams_shared GitHub repos for open Dependabot
  security alerts and CI build failures, investigate each for real, fix what's safely
  fixable (and push it), message another active session if something is urgent, and record
  everything durably in night_shift_todo.md. Also runs automatically once an hour (for now) as
  a scheduled task (dependabot-and-ci-watch); this skill is the same work, invokable on demand
  in a fresh session. Triggers: dependabot, security alert, CI failure, build failure, check for
  vulnerabilities, check the build.
version: 11
---

# Dependabot & CI Build Watch

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
add it directly to THIS FILE as part of that run's own commit, not only to `night_shift_todo.md`.
`night_shift_todo.md` is the durable historical record of what happened; this file is the durable
OPERATING KNOWLEDGE the next run starts with, and the two serve different purposes -- a fact that
would help every future run start faster or avoid rediscovering the same wall belongs here, edited
directly, committed and pushed alongside whatever else that run did (matching the "keep this and the
scheduled task's own prompt in sync" note at the bottom of this file). Don't let this file grow
unbounded with one-off trivia, though -- only add something here if a FUTURE run would genuinely
benefit from already knowing it, the same bar as any other durable-knowledge decision in this
codebase.

**Three ways to act, not just one**: fix things yourself and push, record durably in
`night_shift_todo.md`, or message another active Claude session directly (`ListAgents` to see
what's running, `SendMessage` to reach one) if something is time-sensitive -- a real, currently-
exploitable-shaped security exposure, or a build failure actively blocking other in-progress work
you can see evidence of (e.g. a very recent commit clearly aimed at fixing something that then
failed CI again). Don't message for routine/low-urgency findings -- the durable record is the
right channel for those; messaging is an ADDITION to the durable record for urgent items, never a
replacement for it (a chip or message alone is not durable -- see the
`hams-durable-tracking-over-chips` convention, which is exactly why this all also goes into
`night_shift_todo.md` regardless of whether you also messaged someone).

**Check `ListAgents` before starting substantive work, not just when something urgent comes up.**
This skill runs both as an hourly scheduled task and as manual, on-demand sessions Bruce starts
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
coordination trail in `night_shift_todo.md` so it's not just two chat messages that vanish with the
sessions.

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
out there, but its own separate GitHub repo, `BrucePerens/hams_shared`). Read
`/home/bruce/workspace/hams_com/CLAUDE.md` first for this project's standing engineering
conventions (env-var ordering for `sudo -u odoo` test runs, worktree symlink gotchas,
secret-handling rules) before doing anything else -- they apply to any fix you make here.

## Step 1: Check Dependabot alerts

For each of the three repos, run:
```bash
gh api repos/BrucePerens/<repo>/dependabot/alerts --jq \
  '.[] | select(.state=="open") | {number, severity: .security_advisory.severity, summary: .security_advisory.summary, package: .dependency.package.name, ecosystem: .dependency.package.ecosystem}'
```

For each open alert found:
1. Check whether `night_shift_todo.md` already has an entry for this exact alert (grep for the
   package name / alert number) -- if so and nothing has changed, skip it, don't re-report the
   same thing every run.
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
   removing/replacing a dependency entirely, do NOT force it unilaterally -- record it clearly in
   `night_shift_todo.md` as needing Bruce's own judgment call, with the specific tradeoff named
   (not just "needs a decision").
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

## Step 2: Check CI build status

For each of the three repos, run:
```bash
gh run list --repo BrucePerens/<repo> --limit 15 --json databaseId,name,status,conclusion,createdAt,headBranch
```

For each run with `conclusion: "failure"` on the main/default branch that's NOT already recorded
in `night_shift_todo.md` (grep for the workflow name + rough date):
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
comment) -- when you bump one, immediately run `cargo clippy --release -- -D warnings` (matching
CI's own "Clippy (warnings as errors)" step exactly) under the NEW toolchain and fix every new
finding it surfaces, the same run, not as a separately-deferred follow-up. The same principle
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
- **The arm64 `build-linux` leg runs natively on the Raspberry Pi 500 runner `pi500-1`** (since
  2026-09-14; it used to run under QEMU emulation on the dev box, which broke whenever a reboot
  dropped the binfmt handlers). Facts: runner label `pi500-1`; Debian 12 bookworm, aarch64; jobs
  run as the `ai` account, which has passwordless sudo; rustup is installed; there is no Docker,
  so that leg has `container: ""` and runs on the host. The runner is ephemeral (one job per
  just-in-time registration), re-minted continuously by `pi500-runner-jit-loop.service` on the dev
  box. If the Pi picks up no jobs, check that service first (`systemctl status
  pi500-runner-jit-loop`), then `gh api repos/BrucePerens/hams_com/actions/runners`. The Pi has an
  outbound firewall (`pi500-egress`) allowing only ports 80/443/22, DNS and NTP, so a build step
  that needs another port will be dropped (logged as `pi500-egress-drop:` in the Pi's dmesg).
  `test-pi500-runner-smoke.yml` (workflow_dispatch) is a cheap check that the runner is alive.
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
  scratch `CARGO_TARGET_DIR` so you don't disturb other sessions' builds. The three server daemon
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
- **The hams_com working tree is shared with other live Claude sessions.** Commits and line
  numbers can change under you mid-run. `git fetch` and re-read before editing, find edit sites
  by content rather than stale line numbers, and `git add` only the specific files you changed.
  If a peer session is working the same failures (`ListAgents`, then a short `SendMessage`
  naming who takes which item), split the work rather than both editing the same workflow file.
- The scheduled task's own prompt (`~/.claude/scheduled-tasks/dependabot-and-ci-watch/SKILL.md`)
  still says hams_com pushes are blocked by the missing `workflow` scope and unpushed commit
  `a723508b`. That is stale as of 2026-09-14: the scope was granted and `a723508b` is pushed.
  This file is authoritative; check `gh auth status` fresh.
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
  - Record what you cancelled in `night_shift_todo.md`.
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
  `hamlib::linked_hamlib_ic7300_model_id()` instead of a literal.
  (3) The dummy rotator's azimuth range is -180..180 (also in 4.3.1), not -180..450 (4.6.2+).
  Distribution Hamlib versions: Ubuntu 20.04 3.3, 22.04 4.3.1, 24.04 4.5.5, Debian 12 4.5.4.
  Each release's source tarball is on Hamlib's GitHub releases page, for checking API questions.
  (4) Hamlib 4.3.1's dummy rig stores `rig_set_split_freq()` in a side field that
  `rig_get_split_freq(rig, VFO_B)` never reads, because that call reads VFO B through `get_freq`.
  The split-TX safety test therefore sets VFO B's frequency too. Upstream fixed the dummy by
  4.5.5. The relay's `get_tx_freq()` is correct for real radios.
  In a local container run, `callbook::tests::has_adequate_space_matches_a_real_independent_df_call`
  fails if the disk under `/var/lib/docker` changes free space mid-run. It compares a sysinfo
  reading against a later `df`. Check `df -h /var` before blaming code.
  To check a 3.3 question, the 3.3 source tarball is on the Hamlib GitHub releases page (tag
  `3.3`). A full `cargo test` for this leg runs in an `ubuntu:20.04` container: install
  `install_debian_deps.sh` and `install_relay_runtime_deps.sh`, then run cargo with a scratch
  `CARGO_TARGET_DIR`. The build takes about 7 minutes, the tests about 5.
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
  and not rely on `.path`. See `night_shift_todo.md` for whether `.path` has been cleaned yet.
- **`hams_com` has no repository secrets** (`gh secret list` is empty, as of 2026-09-14; no
  production Odoo exists yet). Every publish step on `main` (binary zips, .deb, .rpm, apt repo,
  Windows) will fail at `curl "$ODOO_URL/..."` with an empty URL once its build is green. That is
  expected before deployment, not a new bug. Check `gh secret list` fresh, because it stops being
  true once Bruce creates them. **Read the publish step's last log line before calling it the
  expected failure.** The expected one is `curl: (3) URL rejected: No host part in the URL`
  (exit 3). The `.deb` job's `ubuntu:22.04` container prints the same empty-URL error differently,
  as `curl: (3) URL using bad/illegal format or missing URL`, because its curl is 7.81 and the
  other containers have curl 8.x. This was reproduced locally on 2026-09-15. An exit 127 (`zip: command not found`, `jq: command not found`) means the job's
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
- **The hams_com git index is shared too, not just the working tree.** On 2026-09-14 this run
  committed `night_shift_todo.md` with a plain `git commit`, and another session's already-staged
  Rust changes (199 of 201 lines) went in under a todo-only commit message and were pushed.
  `git commit -- <path>` doesn't fully protect you either: it commits the path's *working-tree*
  content, including any other session's uncommitted edits to the same file. The safe pattern for
  appending only your own text to a shared file is a private index:
  `git show HEAD:<file> > tmp; cat my_entry >> tmp; GIT_INDEX_FILE=idx git read-tree HEAD;
  GIT_INDEX_FILE=idx git update-index --add --cacheinfo 100644,$(git hash-object -w tmp),<file>;
  c=$(git commit-tree $(GIT_INDEX_FILE=idx git write-tree) -p HEAD -m "...")`. Then check
  `git diff --stat HEAD $c`, run `git update-ref refs/heads/main $c HEAD`, and put the new blob into
  the real index for that one path, so the shared index doesn't show your commit staged in reverse.
  Also append the same entry to the working-tree file.
- **Before pushing hams_com, run `git log --oneline origin/main..main`.** Another session may
  have committed on `main` and be holding the push on purpose. For example, a relay change can wait
  until a local test run finishes, because pushing anything under `daemons/hams_local_relay/**`
  queues a full relay CI matrix on the dev box. Pushing your commit would push theirs too. If
  the list shows commits that aren't yours, commit locally and don't push. Use `ListAgents` and
  `SendMessage` to find the owner and agree that it pushes your commit along with its own. Note
  that in `night_shift_todo.md`. This happened on 2026-09-15, eighth hourly check (hams_com
  `afd538ff`).
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

## Standing decisions from Bruce -- act on these, don't ask

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

Still Bruce's (these involve money, accounts, or product direction): creating or re-scoping
credentials, and dismissing Dependabot alerts on GitHub. The macOS leg is no longer a billing item:
Bruce postponed it until the self-hosted Mac arrives, and hams_com `2520690c` sets `build-macos`
to `if: false`. Don't report its skipped job as a failure.

## Step 3: Record everything, always

Whether or not anything needed fixing, append a dated entry to
`/home/bruce/workspace/hams_com/night_shift_todo.md` (re-read the file fresh immediately before
editing, append at the very end, since other work may be concurrently touching it) summarizing:
what Dependabot alerts and CI failures currently exist across all three repos, what you fixed
(with real commit hashes and test output), what you pushed, and what genuinely still needs
Bruce's own input (named specifically, not vaguely). If truly nothing new was found (previous
alerts/failures already resolved, no new ones), say that plainly and briefly rather than padding
the entry -- a short "checked, nothing new" entry is the honest and correct output on a clean day,
not a failure to find something. Do NOT use the `spawn_task` chip mechanism for anything found
here -- Bruce has explicitly asked that this kind of tracking go directly into
`night_shift_todo.md` instead.

## Constraints

- Never delete or weaken a test to make something pass.
- Never force-push, never rewrite published history.
- If `gh` itself is not authenticated or a repo API call fails for a real access reason (not just
  "no alerts"), report that plainly rather than silently treating it as "no findings."
- If this is a repeat run (via the hourly schedule) rather than a fresh manual invocation, don't
  re-litigate an alert or failure you already investigated and recorded unless its real state has
  changed (newly reappeared, got worse, or a fix you applied didn't actually stick).

## Relationship to the scheduled task

This same procedure also runs automatically once an hour, for now (per Bruce's own explicit
instruction, subject to change), via a scheduled task (`dependabot-and-ci-watch`,
`/home/bruce/.claude/scheduled-tasks/dependabot-and-ci-watch/SKILL.md`) -- that task's own prompt
is this skill's content. Update both together if the procedure changes, so they don't drift apart.

This skill lives in `hams_shared` (a public repo, unlike `hams_com`) deliberately -- it has nothing
proprietary or sensitive in it (no trade-secret methodology, just a generic dependency/CI-watch
procedure) and applies equally to all three repos, matching this project's own established
convention of putting cross-cutting, content-free mechanical tooling in the shared public repo
rather than duplicating it per-repo or defaulting it into the private one.
