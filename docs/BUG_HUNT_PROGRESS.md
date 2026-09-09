# Bug-Hunt Campaign Progress

A durable record of which functions, across `hams_com` and `hams_open`, have gone through the
bug-hunt skill's own claim-then-verify method (`hams_shared/agents/skills/bug-hunt/SKILL.md`,
ADR 0091). Bruce's own direction (2026-09-09): this method is valuable enough that the intent is
to eventually run the entire body of code through it, even if that takes a long time and many
sessions' worth of token budget -- this file exists so that intent survives across sessions
instead of restarting from zero each time, and so a session picking this up mid-campaign can see
at a glance what's covered, what's queued, and what to do next.

**How to resume**: read the "Queued / not yet started" list for whatever module you're about to
work, dispatch one function (or a deliberately-batched small cluster of trivial ones, per the
skill's own guidance) at a time per its own method, update this file's tables as each dispatch's
claim lands (move the function from Queued to Done, fill in the outcome), and update the
per-module/per-repo rollup counts. Check `<module>/claims/` for files that already exist before
dispatching -- a function with a real, current (non-stale, per `check_claims_freshness.py`) claim
doesn't need a fresh dispatch.

## Rollup

| Repo | Module | Functions in scope | Reviewed | Claims written | Real bugs found |
|---|---|---|---|---|---|
| hams_open | user_websites (models/) | ~93 (see per-file counts below; controllers/hooks not yet counted in full) | 8 | 8 | 6+ (see table) |
| hams_com | ham_logbook | 1 (prototype only) | 1 | 1 | 0 (clean) |
| *(all other modules, both repos)* | -- | not yet surveyed | 0 | 0 | -- |

This table is deliberately coarse at the repo/module level and precise at the function level only
for modules actively being worked -- keep it that way as the campaign grows; don't try to
pre-enumerate every function in every module up front, since that's its own significant survey
effort. Add a module's own function-level table (like the ones below) the session it's first
worked, not before.

## hams_open / user_websites

Per-file function counts (`grep -cE "^\s*def " <file>`, 2026-09-09; `models/__init__.py` has 0):

| File | Functions | Status |
|---|---|---|
| `models/res_users.py` | 22 | not started |
| `controllers/main.py` | 16 | not started |
| `models/website_page.py` | 10 | not started |
| `models/blog_post.py` | 8 | not started |
| `models/user_websites_groups.py` | 7 | not started |
| `models/content_violation_report.py` | 5 | **done** (5/5) |
| `models/blog_blog.py` | 5 | not started |
| `models/sql_views.py` | 4 | not started -- likely high-value next target, since `content_violation_report.py`'s own `increment_strike_count` claim found real questions about the stored procedure this file defines |
| `models/ham_gdpr_export_token.py` | 4 | not started |
| `models/res_users_moderation.py` | 3 | not started |
| `models/content_violation_appeal.py` | 3 | **done** (3/3) -- pilot cluster fully closed |
| `models/user_websites_owned_mixin.py` | 2 | not started -- referenced by multiple findings below (`_check_proxy_ownership_create`) as a borrowed invariant other models depend on; worth prioritizing since it's a real dependency, not just next-in-list |
| `models/res_config_settings.py` | 2 | not started |
| `hooks.py` | 1 | not started |
| `controllers/user_websites_api.py` | 1 | not started |

### `content_violation_report.py` and `content_violation_appeal.py` -- done, first pilot cluster

| Function | Claim file | Outcome |
|---|---|---|
| `_cron_notify_pending_reports` | `claims/cron_notify_pending_reports.md` | **Severe bug**: uncaught `AccessError` crashes the per-company loop for every company but `base.main_company`; separately, an over-permissive `ir.rule` leaks a cross-company count even for the company that doesn't crash. Root cause: loop iterates every company in the DB rather than the service account's own granted `company_ids` (bug class 17). First real use of EARS+FOR-EACH+ISOLATION phrasing; claim's own first-draft fix was corrected mid-review (see `hams-multicompany-fix-check-adr-0083-first` memory). |
| `_increment_strike_count` | `claims/increment_strike_count.md` | Docstring's "atomically locks and increments" is directionally true but was materially imprecise about the mechanism -- Odoo runs at Postgres `REPEATABLE READ`, so a real conflicting concurrent writer aborts with `SerializationFailure` rather than "blocking and applying in sequence" as first drafted (corrected before landing). The `FOR NO KEY UPDATE` pre-lock adds no serialization guarantee beyond a bare `UPDATE` at this isolation level. Two latent (not currently reachable) defects found in the PL/pgSQL procedure itself (`sql_views.py`): an unrecognized `table_name` or nonexistent `rec_id` silently no-ops with no lock, no update, no exception, no log -- new bug class 19. Also flagged a sibling test (`test_05_concurrent_strike_locking`) whose name overclaims coverage it doesn't provide (asserts a call happened, never runs real concurrent transactions) -- bug class 3. |
| `action_mark_under_review` | `claims/action_mark_under_review.md` | Bug: no server-side guard against invoking on a report already `action_taken`/`dismissed` -- only a view's `invisible` attribute. New bug-class candidate, since consolidated into the skill as class 18. |
| `action_dismiss` | `claims/action_dismiss.md` | Same class as above; more consequential (can dismiss a report whose owner/group already took a strike, with no reconciliation). |
| `action_take_action_and_strike` | `claims/action_take_action_and_strike.md` | **Severe, confirmed-reachable bug**: the real caller in `website_page.py` looks up an existing report by `(url, reporter)` only, not `state` -- resubmitting the same violation re-triggers strike-and-suspend on an already-fully-processed report (3 saves of one page = 3 strikes). Also: the `elif` branch silently sets `state="action_taken"` with zero strikes applied when neither `content_owner_id` nor `content_group_id` is set (reachable via an unrecognized-URL public report). |
| `_check_appeal_target` | `claims/check_appeal_target.md` | Clean -- confirmed correct by reading real Odoo ORM source, not the code comment's own account: `default=False` genuinely guarantees the constraint fires on `create()` even when both fields are omitted (verified against `Field._setup_attrs__`, `Model.default_get`, `Model._create()`), no DB-level backstop exists but none is needed today (no `create()`/`write()` override, no raw SQL writer found), and `sudo()` doesn't weaken it since `_validate_fields` runs under `sudo()` unconditionally. One precision-only overclaim found in the comment (says "create()/write()", but `write()` never applies field defaults at all) -- zero behavioral impact since both fields are plain stored `Many2one`s with no `inverse`/`related`/`compute`. |
| `action_approve` | `claims/action_approve.md` | Structural finding, not just a code finding: the function had **no base anchor of its own** (only a false `# Verified by [@ANCHOR: user_websites:test_tour_moderation_appeal]` citation -- that tour never authenticates as admin or calls `action_approve`; bug class 3) -- so the claims system had nothing to attach a hash to. The orchestrating session added the missing anchor (`COMM_appeal_action_approve`), a matching `# Tests [@ANCHOR: ...]` link on the test that actually exercises it (`test_02_submit_and_approve_appeal`), and a `docs/modules/user_websites.md` entry, then landed the claim. Confirmed bug class 18 (no server-side state guard) a fourth time, from the opposite direction of `action_reject`: re-approving an already-approved appeal re-runs the pardon call and re-posts an undeduplicated audit message (idempotent on final field values, not on the audit trail). |
| `action_reject` | `claims/appeal_action_reject.md` | Bug: no server-side state guard (same class 18); rejecting an already-*approved* appeal posts a message ("remains suspended") that's false at that moment, since the message text is chosen from `group_id`/`user_id` truthiness, not the real `is_suspended_from_websites` flag. Confirmed the actual suspension-enforcement code never reads this appeal's own `state` field, so the defect is data-integrity/audit-trail, not an active suspension bypass. FOR-EACH isolation confirmed genuinely maintained here (a clean result on that dimension, unlike the cron). |

### New bug classes and process findings from this cluster (all consolidated into the skill already)

- Bug class 17 (a for-each loop reaching into data outside the caller's real scope) and its
  correction (the fix is narrowing the loop's own domain, not collapsing to one query, per
  ADR-0083's own mandate for per-company `.with_company()` in cron jobs).
- Bug class 18 (state-machine guard only in client-rendered UI, no server-side enforcement) --
  confirmed independently by four different functions in this cluster (`action_mark_under_review`,
  `action_dismiss`, `action_reject`, `action_approve`).
- Bug class 19 (an enumerated `IF`/`ELIF` dispatch, in application or stored-procedure code, with
  no terminal `ELSE`/default-raise branch, so an unmatched value silently no-ops) -- found in the
  `increment_strike_count()` PL/pgSQL stored procedure.
- Two Friction-log entries: FOR-EACH being hollow when forced onto a bare bulk `write()`, and
  FOR-EACH not fitting a claim about two concurrent invocations of a function with no internal
  iteration (a candidate `CONCURRENT-WITH` pattern proposed, not yet adopted).
- `hams_shared/docs/odoo_orm_reference.md` created from real findings in this cluster (multi-company
  `AccessError` semantics, `ir.rule` OR-combination, `res.company`'s own default ordering,
  `mail.template.send_mail_batch`'s `email_values` override behavior).

## hams_com / ham_logbook

| Function | Claim file | Outcome |
|---|---|---|
| `_compute_callsign_stripped` | `hams_com/ham_logbook/claims/compute_callsign_stripped.md` | Clean -- the ADR 0091 prototype claim, written before any bug-hunt pass had run against it; not itself the product of an adversarial pass, just the worked example for the claim format. |

## Queued / not yet started (next reasonable targets, in rough priority order)

1. `user_websites/models/user_websites_owned_mixin.py` (2 functions) -- prioritized ahead of its
   position in the function-count table because multiple findings above already depend on its own
   `_check_proxy_ownership_create` as a borrowed invariant; understanding it directly is now
   overdue rather than merely next-in-line.
2. `user_websites/models/sql_views.py` (4 functions) -- the `increment_strike_count()` stored
   procedure this module defines is already under adversarial question from the pilot cluster.
3. `user_websites/models/res_users_moderation.py`, `res_config_settings.py`, `hooks.py`,
   `controllers/user_websites_api.py` -- small files, reasonable next batch.
4. The larger files in `user_websites` (`res_users.py`, `controllers/main.py`, `website_page.py`,
   `blog_post.py`, `blog_blog.py`, `user_websites_groups.py`, `ham_gdpr_export_token.py`).
5. Every other module in both `hams_com` and `hams_open` -- not yet surveyed at all. The first
   session to start a new module should add its own function-count table here, the same way
   `user_websites`'s was added, before dispatching.
