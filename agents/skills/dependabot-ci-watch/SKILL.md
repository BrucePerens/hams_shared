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
version: 2
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

   **Real, confirmed gap in the current token's scope**: the `gh` token's scopes are
   `admin:public_key, gist, read:org, repo` -- notably NOT `workflow`. GitHub specifically
   requires the `workflow` OAuth scope to push ANY change to a file under `.github/workflows/`,
   even a plain `.sh` helper script that lives there but isn't itself a `.yml` workflow
   definition (confirmed directly: a real push touching `.github/workflows/publish_relay_binary.sh`
   was rejected with `refusing to allow an OAuth App to create or update workflow ... without
   'workflow' scope`, exit non-zero, commit made locally but not pushed). If you fix something
   under `.github/workflows/` (a real, plausible thing for this exact task to need, since CI
   script bugs live there), expect this same rejection. Do NOT attempt to work around it (no
   force-push, no alternate auth path, no creating your own token). Commit the fix locally as
   normal, then report in your `night_shift_todo.md` entry that it's committed-but-not-pushed
   specifically because of this scope gap, and name the exact fix: either Bruce pushes that one
   commit himself, or he runs `gh auth refresh -s workflow` (an interactive device-code approval
   only he can complete) to add the missing scope permanently.

## Step 2: Check CI build status

For each of the three repos, run:
```bash
gh run list --repo BrucePerens/<repo> --limit 15 --json databaseId,name,status,conclusion,createdAt,headBranch
```

For each run with `conclusion: "failure"` on the main/default branch that's NOT already recorded
in `night_shift_todo.md` (grep for the workflow name + rough date):
1. Get the real failure log: `gh run view <databaseId> --repo BrucePerens/<repo> --log-failed`
2. Read enough of the actual log to find the real root cause -- don't guess from the workflow
   name alone.
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
