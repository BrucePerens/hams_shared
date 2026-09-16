---
name: dependabot-ci-watch
description: >-
  Check all three real hams.com/hams_open/hams_shared GitHub repos for open Dependabot
  security alerts and CI build failures, investigate each for real, fix what's safely
  fixable (and push it), message another active session if something is urgent, and record
  genuinely open items in the night_shift_todo/ priority queue and resolved ones in
  night_shift_history.md (never a "nothing found" entry in either). Also runs automatically once an
  hour (for now) as
  a scheduled task (dependabot-and-ci-watch); this skill is the same work, invokable on demand
  in a fresh session. Triggers: dependabot, security alert, CI failure, build failure, check for
  vulnerabilities, check the build.
version: 28
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
add it directly to THIS FILE as part of that run's own commit, not only to a durable-tracking file.
See "Step 3" below for the real distinction between the `night_shift_todo/` priority queue (open,
actionable to-dos only -- full convention in the `night-shift-todo` skill) and `night_shift_history.md`
(the record of what already happened) -- THIS file is neither of those: it's the durable OPERATING
KNOWLEDGE the next run starts with, a third, separate purpose,
edited directly, committed and pushed alongside whatever else that run did (matching the "keep this
and the scheduled task's own prompt in sync" note at the bottom of this file). Don't let this file
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

## Step 1: Check Dependabot alerts

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

## Step 2: Check CI build status

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
  the last note said. The real count comes from `list_task_runs` on the `dependabot-and-ci-watch`
  task (`totalRuns`); its newest entry also gives this run's true `started_at`, which is the date
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
- The scheduled task's own prompt (`~/.claude/scheduled-tasks/dependabot-and-ci-watch/SKILL.md`)
  still says hams_com pushes are blocked by the missing `workflow` scope and unpushed commit
  `a723508b`. That is stale as of 2026-09-14: the scope was granted and `a723508b` is pushed.
  This file is authoritative; check `gh auth status` fresh. That prompt also described the retired
  single-file `night_shift_todo.md` throughout, which would have sent a scheduled run to write in
  the wrong place; a correction block naming the structured `night_shift_todo/` queue was inserted
  ahead of its Step 3 on 2026-09-15, so that drift is handled without rewriting all 72 lines.
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
  **You cannot tell which session made a commit from its authorship.** Every session on this box
  commits as `BrucePerens <bruce@perens.com>`, so a commit's author, its subject line, and even its
  topical similarity to work you saw a session doing are all worthless as identification. On
  2026-09-16 a run guessed an owner that way and messaged the wrong session twice. If you need to
  know whose a commit is, ask on `ListAgents`'s live sessions rather than inferring it -- and expect
  that sometimes nobody claims it, because the session that made it has already exited.
  **Check a claimed CI fix against `origin/main`, never the working tree.** A peer's fix often
  exists only as uncommitted edits to the shared tree, so `grep`ping the file on disk shows the
  clean, fixed form while CI is still failing on the pushed, unfixed one. Read the pushed content
  directly: `git show origin/main:<path> | grep -n <pattern>`. Found 2026-09-16, twenty-sixth
  hourly check, on the `offline_queue_test_lock` clippy failure -- the working tree had the async
  `tokio::sync::Mutex` fix and `origin/main` still had `std::sync::MutexGuard`.
  **Don't write an ordinal ("the Nth hourly check") you haven't actually counted.** Several notes
  in this file carry one, and it is easy to copy a plausible-looking number forward from whatever
  the last note said. The real count comes from `list_task_runs` on the `dependabot-and-ci-watch`
  task (`totalRuns`); its newest entry also gives this run's true `started_at`, which is the date
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

Still Bruce's (these involve money, accounts, or product direction): re-scoping or handling his own
existing credentials, and dismissing Dependabot alerts on GitHub. CI-only secrets a session creates
and controls itself (random shared keys, and since 2026-09-15 the release-signing keypairs) are not
on this list. Re-check any "still Bruce's" label against a primary source every run, rather than
copying it forward. The macOS leg is no longer a billing item:
Bruce postponed it until the self-hosted Mac arrives, and hams_com `2520690c` sets `build-macos`
to `if: false`. Don't report its skipped job as a failure.

## Step 3: Record the RIGHT thing in the RIGHT place

**Superseded 2026-09-15**: `night_shift_todo.md` (the single free-form file this section used to
describe) is now historical -- it grew past 8,900 lines of prose, genuinely hard for any session to
scan for "what's actually still open" without a full read-through. Real, currently-open to-dos now
go in the structured queue at `hams_com/night_shift_todo/` (one file per item, in a
`critical`/`high`/`medium`/`low` priority directory) instead. **Load the `night-shift-todo` skill
for the full convention** (file naming, frontmatter, claiming, what "done" actually means) before
filing or closing anything there -- this section only covers what's specific to
`dependabot-ci-watch`'s own use of it.

`night_shift_history.md`'s role is unchanged from before: the durable record of what already
happened -- completed fixes, resolved alerts, closed-out investigations. Bruce's own words on the
underlying distinction, which still holds: "the to-do list is for real to-dos, night_shift_history.md
is more appropriate for completed actions... if it's an 'I woke up and found no problems', just
delete those and tell the skill they are not worth archiving."

Concretely, at the end of every run:

1. **Nothing new, nothing changed, nothing to fix, nothing open** (the routine "checked, all
   clear" case -- this is most hourly runs on a healthy day): **write nothing anywhere.** No new
   file in `night_shift_todo/`, no entry in `night_shift_history.md`. (If you want a record that
   the check happened at all, that's what the scheduled task's own run log/notification already
   provides.)
2. **Something was found AND fully resolved this run** (a real alert or CI failure, investigated,
   fixed, tested, committed, and pushed -- all three, per `night-shift-todo`'s own "what done
   means" -- with nothing left open): append that summary -- what was wrong, what you changed, the
   real commit hash(es), and how you verified it -- directly to
   `/home/bruce/workspace/hams_com/night_shift_history.md` (re-read fresh immediately before
   editing, append at the end, purely additive). This is a completed action, which is exactly what
   that file is for.
3. **Something genuinely still needs attention** -- a real alert or failure you couldn't safely fix
   yourself, something that needs Bruce's own input (named specifically: which credential, which
   decision, which tradeoff -- never vaguely), or a fix you started but couldn't finish this run:
   file it as its own entry in `night_shift_todo/<priority>/`, per the `night-shift-todo` skill's
   own file format. Pick the priority honestly -- a real, currently-exploitable-shaped security gap
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
