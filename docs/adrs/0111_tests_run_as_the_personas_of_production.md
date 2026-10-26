# ADR 0111: Tests Run As the Personas of Production

## Status
Accepted (Bruce, 2026-10-26: "Tests must understand what persona is appropriate, and where multiple personas are warranted, and run them").

## Context

Two incidents in one week passed a green suite because the suite ran code as someone nobody is.

1. **The event cron.** `ham_events.ir_cron_event_series_generate` runs a code server action as `ham_events.user_event_service`. Odoo refuses a code
   action to a user without write access to the action's model (`ir.actions.server._can_execute_action_on_records`, "Forbidden server action"), and
   the ACL row of that service account was read-only. The cron failed five times and Odoo switched it off on 2026-10-13. The test called the model
   method as that user, which skips the check.
2. **The pager link.** The record rule of `pager.incident` compared `website_id` with `user.website_id.id`. Every internal administrator has no
   website, so the rule hid every incident that carries one and `/mail/view` fell back to Discuss. Test users were created with a website.

Odoo's `res.users.website_id` is empty for all 279 active users of production (the member's own site, `website_slug` from `user_websites`, is a different thing and 264 of them have one); the tests were written for users that do not exist.

## Decision

**1. A catalogue of personas modelled on production**, in `zero_sudo/tests/test_persona_support.py` beside `HamsTransactionCase`, with the counts it was read from:
public visitor, portal member, internal staff, administrator, pager admin, helpdesk staff, each with `user.website_id` empty as on hams.com; the portal member is also bound to a website (`portal_member_with_website`; Odoo refuses to bind an internal user), because these modules are open source and meant for multi-tenant installs that bind logins to a website. A service account is
the data record its module installs (`service(env, xmlid)`), and a cron is its own `user_id` (`cron(env, ref)`), never a copy. `HamsTransactionCase` and
`HamsHttpCase` carry the helpers (`assert_persona_access`, `authenticate_as`).

**2. The persona follows from what the test targets.** `@personas(...)` runs the body once per persona, each in a rolled-back savepoint and its own
`subTest`. `cron_ref=` names the cron's user; `model=` and `mode=` take the groups the ACL rows and record rules name plus the users who must be refused;
`route_auth=` and `route_groups=` take them from `auth=` and the groups a controller checks. A test naming no persona fails; so does a model with no ACL
row to derive from. Nothing runs as the superuser by omission.

**3. A cron's action is run, not its method.** `run_cron_action` calls `server_action.with_user(cron.user_id).run()`, the path `ir.cron` takes, including
the write-access gate. The sweep over all crons calls that same gate (`_can_execute_action_on_records`) without running the action: a cron may fetch from
a third-party server, which a test machine must not do.

**4. Two sweeps need no per-feature work.** Every installed `ir.cron` must pass the gate as its own user. Every `ir.rule` whose domain reads an optional
attribute of `user` (usually `website_id`) is evaluated for a user with the attribute empty and one with it set; a rule that then compares records with
the empty value hides them from that user and must be listed, with its reason, in `DELIBERATE_STRICT`, or in `OPEN_FINDINGS` while unreviewed. A rule in
neither list fails the test, and so does a listed rule that no longer collapses, so the lists only shrink.

**5. The pre-commit check fails closed.** `git-hooks/pre-commit` refuses the commit when it cannot find its checkers (a worktree whose `hams_shared` is an
empty directory searches the main checkout too) and adds `check_staged_files.py`: no staged file over 5 MB, none under a `target/` directory.

## Consequences

- A test that wants a user the catalogue lacks adds it to the catalogue, with the production fact it was modelled on.
- The sweeps run over the modules installed in the run. A module run covers the crons and rules of that module only when `zero_sudo` (or `ham_base` in
  hams_com) runs in the same invocation; a full run covers everything.
- GitHub CI still runs no tests (Bruce, 2026-10-02); the sweeps run in the sessions that run the suites.

## Working in a Worktree

- Make it under the repository it belongs to: `<repo>/.claude/worktrees/<name>` (git-ignored, and the place `test.py`'s `resolve_repo_layout` and `fix_worktree_symlinks.py` expect).
- Run `python3 fix_worktree_symlinks.py` from the new worktree before anything else: its `hams_shared` is otherwise an empty directory (hams_open) or a dangling relative link (hams_com). To test against a `hams_shared` branch, make a worktree of `hams_shared` and point the link at it by hand; `git rebase` refuses while `hams_shared` is a symlink, so replace it by an empty directory and run `git update-index --no-skip-worktree hams_shared` first, then link it again.
- The anchor scan and the Odoo addons path need the other repository: a hams_open worktree has no `hams_com` sibling, so the scan reports about 1,700 false findings. Set `HAMS_COM_TREE=<a hams_com checkout or worktree>` (and `HAMS_OPEN_TREE=<a hams_open checkout or worktree>` from a hams_com worktree) for `verify_anchors.py` and `test.py`.
- `HamsHttpCase` starts headless Chrome in every test and the test is skipped, not failed, when Chrome does not start. A test that only makes HTTP requests uses `HttpCase` with `PersonaMixin`. Check the log for `skipped` after a run of an HTTP test.
