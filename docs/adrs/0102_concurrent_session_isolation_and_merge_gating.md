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
     dedicated night-watch reviewer PAT (GitHub personal access token; decision 4) returns `BrucePerens`, identical to the
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
     ever run it would deadlock every future `hams_shared` PR, so `hams_shared` has
     `enforce_admins: false` (as in the first bullet above) and the required review only, no required status check, until a runner is
     registered there too (see the tracked follow-up). The full `hams_shared/tools/test.py` Odoo suite as
     a check (on either repo) is a further, separate follow-up: it needs a runner matching this dev box's
     Postgres/`odoo`-user environment, real infrastructure work, not a settings change.
     **Superseded 2026-10-02 -- see the addendum at the end: there is no burn-list check any more, and
     `test.py` is never to run in CI.**

**4. Withdrawn 2026-10-02: there is no night-watch fast-merge path. `night-shift/*` PRs go through
ordinary human review like every other PR.** The 2026-09-22 design was a dedicated local script
(`hams_shared/tools/night_watch_review_and_merge.py`, run once per night-watch hourly cycle, never in
GitHub Actions -- standing policy: AI agents do not run in GitHub Actions) that found open PRs whose head
branch matched `night-shift/*` and submitted an approving review plus a merge using a **second, dedicated
PAT** (`~/.secrets/hams_com_ci/NIGHT_WATCH_REVIEWER_GITHUB_PAT`, `Pull requests` and `Contents` read/write,
repo-scoped to `hams_com` and `hams_shared`, no `Administration`). It never worked, confirmed live
2026-09-22 against `BrucePerens/hams_shared#4`: `GraphQL: Review Can not approve your own pull request`.
A fine-grained PAT authenticates as the GitHub *account* that issued it, not as a separate bot identity,
and every credential on this box is `BrucePerens` -- the same account that opens every PR -- so GitHub's
self-approval block applies whichever token submits the review.

The earlier text here said the design was "left in place ... not deleted, so a future fix doesn't have to
rediscover this." That is superseded: the script was deleted on 2026-10-02 (it is in git history), and
this paragraph is now the record a revival needs. The script was dormant code holding merge capability
under the admin account; it only failed safe because its `--approve` call raised before the merge call
ran. Do not assume a variant that skipped the approval would be refused: the `hams_com` and `hams_shared`
rulesets give the admin role a `pull_request` bypass (checked 2026-10-02), so the admin account can merge a
PR past the required review. A revival needs all of the following:
   - **A genuinely separate GitHub identity** with write access (a bot/service account or a GitHub App
     installation), not another PAT on `BrucePerens`. The same identity would also let decision 3's
     `enforce_admins` go back on.
   - **A re-specified merge gate.** The design gated on required status checks, but since the 2026-10-02
     addendum there is no required check on any repository, so that gate is vacuous; something like "the
     Rust workflows that ran on this head passed" would have to be named explicitly. (The deleted script
     did not check statuses itself either; it relied on GitHub refusing the merge.)
   - **Disjoint credentials.** GitHub has no permission that allows merging a PR without also allowing a
     content write (verified against GitHub's REST documentation on 2026-09-22), so the reviewer
     credential's scope is functionally identical to the ticket-triage PAT's. The separation between
     "night-watch can fast-merge its own work" and "ticket-triage can never self-merge" would rest
     entirely on two different secrets with disjoint access: the
     ticket-triage MCP server must never be able to read the reviewer credential, and the reviewer must
     never be able to read the ticket-triage PAT. Both belong under `~/.secrets/`, each in its own file
     (this dev box's secret convention, not AWS Secrets Manager).

`ai-triage/*` PRs (`hams_helpdesk` ticket triage) are covered by decision 3's unconditional required
review: no script or scheduled job on this box approves or merges any PR, and the admin bypass
(`gh pr merge --admin`) is used only for a merge Bruce has reviewed or directed. The `night-shift/<slug>`
branch naming is still used by night-watch and other night-shift sessions as a plain naming convention;
nothing acts on it automatically.

**5. Orphaned worktrees are swept, never force-deleted, on the same hourly cadence.**
`hams_shared/tools/sweep_orphan_worktrees.py`: `git worktree prune` for any worktree whose directory is
already gone (always safe); for one still present, `git worktree remove` only if it is clean and fully
pushed; anything with uncommitted or unpushed content is left alone and flagged as a `night_shift_todo/`
entry instead of destroyed. `ExitWorktree`'s own keep/remove prompt already covers a session's graceful
exit and already refuses to remove a dirty worktree -- this sweep only covers the case that prompt can't
reach: crashes, force-quits, and worktrees created by hand with `git worktree add`.

**6. A pure `hams_shared` submodule-pointer bump in `hams_open` is pushed directly to `main`, no PR**
(Bruce's decision, 2026-10-02). Its content was already reviewed and gated as `hams_shared`'s own PR, so a
second PR around a one-line gitlink change reviews nothing new and only delays production pickup (a
`hams_shared` change reaches `hams1` (the production server, see [ADR 0103](0103_pgbackrest_privileged_backup_sidecar.md) and [ADR 0106](0106_tenants_inside_one_odoo.md)) only once `hams_open` records it). Server side, `hams_open`'s ruleset
`main-pr-required-no-direct-push` (id 24045234) had its admin-role bypass changed from `pull_request` to
`always` the same day so the push is accepted. That bypass cannot be scoped to submodule changes on a
personal (non-organization) repository, so it lets **any** direct admin push through, and every session on
this box authenticates as the admin account: the narrowness of this exception is enforced by the written rule
in `AGENTS.md`'s `site_rules` (on `main`, nothing ahead of `origin/main`, only the `hams_shared` path staged,
target commit reachable from `hams_shared`'s `origin/main`), not by GitHub. Everything else on `hams_open`
still goes through a PR exactly as before; `hams_com` and `hams_shared` are unchanged.

## Consequences

Local working-tree damage from one session's mistake is now contained to that session's own worktree --
the failure mode that actually happened on 2026-09-22 cannot recur in the same form. Server-side branch
protection, not a client-side procedure, is what actually stops a bad push from landing on `main`, and it
applies identically to every session including an admin-authenticated one. The cost is real: worktrees use
more disk per session, and merging now goes through a PR rather than a direct push (every PR, including
night-watch's own `night-shift/*` ones, lands via `--admin` by Bruce -- see decision 4's withdrawal); the one
exception is the direct push of a `hams_shared` submodule-pointer bump to `hams_open` (decision 6). The shared
`pre-push` hook (decision 2) is only the client-side early-warning layer, and none of this can enforce itself if a session goes looking
for ways around it, which is exactly why decision 3 does not depend on anything running on the session's
own machine.

## Addendum, 2026-10-02: GitHub CI runs only the Rust tests; the burn-list gates are gone

Bruce's decision, 2026-10-02, for all three repositories (hams_com, hams_open, hams_shared). The text
above is kept as the record of what was decided on 2026-09-22; where it conflicts with this addendum,
this addendum wins.

- **The only tests GitHub CI runs are the Rust tests across architectures**: hams_com's
  `build-relay.yml` (multi-architecture matrix: GitHub-hosted x86_64 containers plus the native arm64
  `pi500-1` leg), `build-server-daemons.yml`, and `test-jetson.yml` (native on `jetson-1`). Advisory
  security jobs that are not test duplicates (pip-audit, scheduled cargo-audit / cargo-deny,
  dependency-watch) stay.
- **CI never runs `hams_shared/tools/test.py`**, nor the Odoo-loading parts of `run_linters.py`. Odoo
  tests are run by the working session on the dev box and the other test hosts. This replaces decision
  3's "full `test.py` Odoo suite as a check" follow-up, which is now explicitly not wanted. The rule
  itself lives in `AGENTS.md`'s `site_rules`.
- **The burn-list CI workflow is removed** from hams_com (#553) and hams_shared (#75), so decision 3's
  `required_status_checks` bullet no longer describes anything: no `burn-list` check is wanted on
  either repository. The hams_shared follow-up to register a runner so that check could be required
  there is retired, not pending. hams_shared has no workflow that needs a self-hosted runner.
  Housekeeping still owed as of this addendum: hams_com's ruleset `main-pr-required-no-direct-push`
  (id 24045180) still lists `burn-list` as a required check, which can now never report. Until Bruce
  removes it, every hams_com PR shows that check as waiting and lands only via `--admin`, as decision 3
  already describes.
- **The `check_burn_list.py` gate in the shared `pre-push` hook is removed** (hams_shared #74). Decision
  2's hook remains, but only for the fast-forward check and the refusal of `--force` to `main`.
  `check_burn_list.py` is still the local linter sessions run themselves; it is no longer a gate in CI
  or in a hook.
- **The hams-devbox self-hosted runner is stopped and disabled** (hams_com #443, 2026-10-01). Its jobs
  run on GitHub-hosted runners. The self-hosted runners still in use are the just-in-time `pi500-1` and
  `jetson-1` boards, which hold no publish secrets.
- Decision 4's fast-merge path is **withdrawn**: it could never self-approve (every credential is the
  same GitHub account), and its status-check gate became vacuous once no check was required.
  `hams_shared/tools/night_watch_review_and_merge.py` is deleted and the night-watch skill no longer
  runs it. Decision 4 now records what a revival would need.

## Related

* ADR 0099 (superseded by this ADR; retained in git history, not renumbered for reuse).
* `daemons/hams_ticket_triage_mcp/main.py` -- the `ai-triage/` branch convention and PAT scoping this ADR
  depends on.
* `docs/proposals/GENERALIZED_AI_CONTEXT_MCP_SERVER.md` -- the ticket-triage agent's own design, including
  the earlier `gh pr merge --admin` bypass finding that prompted decision 3.
