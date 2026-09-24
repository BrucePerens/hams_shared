# ADR 0102: Concurrent-Session Isolation and Merge Gating (Replaces ADR 0099)

## Status
Accepted

## Context

ADR 0099 tried to make many concurrent Claude Code sessions share one working tree and one `.git/index`
safely, through a disciplined private-index commit procedure. It worked for the commit step, but never
protected the thing that actually broke on 2026-09-22: the shared *working tree* itself. A session running
`git read-tree origin/main` + `git checkout-index -a -f` to build a revert commit force-overwrote every
tracked file on disk, silently destroying another session's uncommitted edits (two files' worth of
in-progress work, recovered only because they happened to still be findable in a session transcript).
ADR 0099's own decision 4a already conceded the private index could go stale relative to a moving `HEAD`;
the real lesson is that no amount of git-index choreography protects a shared *filesystem*, because
`checkout-index -f`, `reset --hard`, and a bare `stash` all operate on the working tree directly, bypassing
the index discipline entirely.

The same incident, and an unrelated security test run earlier the same day, also exposed that "protect
`main`" was being done entirely client-side or with an admin bypass: `hams_com`'s branch protection required
one approving review, but `enforce_admins` was `false`, so every admin-authenticated push -- including every
push this session made all night -- sailed through with a logged-but-ignored "Bypassed rule violations"
warning. `hams_shared` had no branch protection configured at all. A `gh pr merge --admin` call had already
been used once, deliberately, to test whether this hole was real; it was.

Bruce's decision, 2026-09-22: move to per-session git worktrees instead of a shared checkout, and close the
branch-protection bypass with GitHub Pro's real server-side enforcement -- while being careful not to let
that enforcement accidentally grant the `hams_helpdesk` ticket-triage agent (`daemons/hams_ticket_triage_mcp`)
an automatic-merge path, since its input is ticket text a member wrote, a real prompt-injection surface that
must always reach a human.

## Decision

**1. Every concurrent session works in its own `git worktree`, not the shared checkout.** Use the harness's
`EnterWorktree`/`ExitWorktree` tools, which create a worktree under `.claude/worktrees/<name>/` on its own
branch. Each worktree has its own index and its own files: a destructive command run inside one (`checkout
-f`, `reset --hard`, an errant `stash`) can only ever damage that one session's own copy. Merging back to
`main` is an ordinary `git push` (or a PR, per decision 3) -- no shared-index choreography, no private
`GIT_INDEX_FILE`, no resynchronization step to forget. Run `python3 fix_worktree_symlinks.py` (repo root)
once per new worktree before relying on `hams_shared/tools/` or building `daemons/hams_local_relay` from
inside it -- unchanged from before this ADR.

**2. A shared `pre-push` hook requires a fresh fast-forward check and refuses `--force` to `main`.** Hooks
live in the common `.git` directory, so one hook installed once (`hams_shared/tools/git-hooks/pre-push`,
installed via `hams_shared/tools/install_git_hooks.sh`) covers every worktree of that repo automatically.
This is a convenience/early-warning layer, not the real enforcement -- a session could edit or delete its
own hook -- so it is backed by decision 3, which is enforced server-side and cannot be locally bypassed.

**3. Branch protection is real, server-side, everyone goes through the same required review and
status check -- with one necessary exception, `enforce_admins`, discussed below:**
   - `enforce_admins: false` on both repos, corrected 2026-09-22 (briefly `true` for under two
     hours). `BrucePerens` is the only GitHub identity with write access to either repo, and a
     fine-grained PAT scoped down to that same account is still that same account as far as
     GitHub's self-approval rule is concerned -- confirmed live: `gh api user` under the
     dedicated night-watch reviewer PAT (decision 4) returns `BrucePerens`, identical to the
     identity that opens every PR. With `enforce_admins: true`, this makes required review
     literally unsatisfiable on this repo: no PR, from any session, by any mechanism, could ever
     be approved by anyone, since the sole account able to review is always also the author.
     Two real PRs (#21, #22) sat unmergeable until this was found and reverted. `enforce_admins`
     protected against one specific threat -- an *agent* using `gh pr merge --admin` to route
     around review, which is what the earlier `ai-triage/mcp-e2e-test` security test actually
     found -- not against Bruce merging his own already-reviewed work, which is what `--admin`
     is now used for instead. Re-enabling `enforce_admins` needs a second GitHub identity with
     write access first (a real collaborator or bot account, not another PAT on the same
     account) so a genuine third party can review; until then, `--admin` merges by a human are
     expected and are the only way anything lands, and they still show up as "Bypassed rule
     violations" in the push output -- visible, not silent.
   - `required_pull_request_reviews.required_approving_review_count` stays at **1**, unconditionally, for
     every PR regardless of author or branch name. This is deliberate: it is the one guarantee that a
     `hams_helpdesk` ticket-triage PR (branch prefix `ai-triage/`, see
     `daemons/hams_ticket_triage_mcp/main.py`) always reaches Bruce before merging. GitHub's branch
     protection rules target the destination branch, not the PR's source, so there is no native way to
     make this requirement conditional on which branch opened the PR -- which is exactly why it must stay
     unconditionally on rather than being narrowed per-source.
   - `required_status_checks` requires the burn-list linter (`.github/workflows/burn-list-lint.yml`) on
     `hams_com`, where it's live. **Not yet enabled on `hams_shared`**: that repo has zero self-hosted
     runners registered to it (confirmed live via the GitHub API) -- `hams-devbox` is registered only at
     the `hams_com` repo level, and GitHub does not share repo-scoped runners across unrelated personal
     repos (that needs an organization, which these are not). Requiring a check with no runner able to
     ever run it would deadlock every future `hams_shared` PR, so `hams_shared` currently has
     `enforce_admins: true` and the required review only, no required status check, until a runner is
     registered there too (see the tracked follow-up). The full `hams_shared/tools/test.py` Odoo suite as
     a check (on either repo) is a further, separate follow-up: it needs a runner matching this dev box's
     Postgres/`odoo`-user environment, real infrastructure work, not a settings change.

**4. A dedicated local script was meant to give trusted autonomous work (night-watch) a fast merge
path, without running any agent through GitHub Actions** (standing policy: AI agents do not run in
GitHub Actions). **Currently non-functional, confirmed live 2026-09-22, tracked in
`night_shift_todo/medium/night-watch-fast-merge-path-cannot-self-approve-same-account-pat-b91af4d3.md`:**
a fine-grained PAT is tied to a GitHub *account*, not a separate bot identity, and every PAT on this
box authenticates as `BrucePerens` -- the same account that opens the PR in the first place. GitHub's
self-approval block applies regardless of which token submits the review, so the design below (a
second PAT with different scope) does not achieve what it set out to. Left in place as-designed
documentation of the intended mechanism and why it needs a real second GitHub identity, not deleted,
so a future fix doesn't have to rediscover this. Until such an identity exists, `night-shift/*` PRs
merge the same way everything else does: `--admin`, by Bruce, per decision 3's correction above.

`hams_shared/tools/night_watch_review_and_merge.py`, invoked once per night-watch hourly cycle (see that
skill's own `SKILL.md`), finds open PRs whose head branch matches `night-shift/*`, confirms required status
checks have passed, and submits an approving review plus merge using a **second, dedicated PAT**
(`~/.secrets/hams_com_ci/NIGHT_WATCH_REVIEWER_GITHUB_PAT`), distinct from whatever identity opened the PR.

This second PAT is required for a structural reason, not just caution: GitHub refuses to let a PR's own
author approve it, so the approving identity must differ from the authoring identity regardless of where
the approval logic runs. Its scope (`Pull requests: Read and write`, `Contents: Read and write`, repo-scoped
to `hams_com` and `hams_shared`, no `Administration`) is -- verified directly against GitHub's own REST
documentation, not assumed -- functionally identical to the ticket-triage PAT's scope, because GitHub has no
finer-grained permission that allows merging a PR without also allowing a content write. Identical scope
means the separation between "night-watch can fast-merge its own work" and "ticket-triage can never
self-merge" depends entirely on these being two different secrets with disjoint access, not on anything
GitHub's permission model enforces on its own:
   - The ticket-triage MCP server must never be able to read `NIGHT_WATCH_REVIEWER_GITHUB_PAT`.
   - The night-watch reviewer script must never be able to read the ticket-triage PAT.
   - Both live under `~/.secrets/`, this dev box's real secret convention (not AWS Secrets Manager, which
     this project barely uses) -- each in its own file, matching every other credential here.

Even with this fast path, `ai-triage/*` PRs are excluded by construction three separate ways: a different
branch prefix the reviewer script never matches, a different script that never runs against them, and a
different credential that can't act on them even if it were pointed at them by mistake. They fall through
to decision 3's unconditional required-review rule and always wait for Bruce.

**5. Orphaned worktrees are swept, never force-deleted, on the same hourly cadence.**
`hams_shared/tools/sweep_orphan_worktrees.py`: `git worktree prune` for any worktree whose directory is
already gone (always safe); for one still present, `git worktree remove` only if it is clean and fully
pushed; anything with uncommitted or unpushed content is left alone and flagged as a `night_shift_todo/`
entry instead of destroyed. `ExitWorktree`'s own keep/remove prompt already covers a session's graceful
exit and already refuses to remove a dirty worktree -- this sweep only covers the case that prompt can't
reach: crashes, force-quits, and worktrees created by hand with `git worktree add`.

## Consequences

Local working-tree damage from one session's mistake is now contained to that session's own worktree --
the failure mode that actually happened on 2026-09-22 cannot recur in the same form. Server-side branch
protection, not a client-side procedure, is what actually stops a bad push from landing on `main`, and it
applies identically to every session including an admin-authenticated one. The cost is real: worktrees use
more disk per session, and merging now goes through a PR rather than a direct push for anything that isn't
night-watch's own fast path -- both `pre-push` hook the client-side layer, and none of this can enforce
itself if a session goes looking for ways around it, which is exactly why decision 3 does not depend on
anything running on the session's own machine.

## Related

* ADR 0099 (superseded by this ADR; retained in git history, not renumbered for reuse).
* `daemons/hams_ticket_triage_mcp/main.py` -- the `ai-triage/` branch convention and PAT scoping this ADR
  depends on.
* `docs/proposals/GENERALIZED_AI_CONTEXT_MCP_SERVER.md` -- the ticket-triage agent's own design, including
  the earlier `gh pr merge --admin` bypass finding that prompted decision 3.
