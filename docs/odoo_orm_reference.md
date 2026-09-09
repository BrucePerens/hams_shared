# Odoo ORM internals reference, for bug-hunt passes

Built from real findings during the 2026-09-09 `user_websites` bug-hunt pilot
(`hams_com/agents/skills/bug-hunt/SKILL.md` -- the review method itself lives in `hams_com`, a
private repo, since it's a competitive/trade-secret asset; this reference doc stays here in the
public `hams_shared` because it's just generic, publicly-derivable facts about the third-party
Odoo framework, not the method), after a process retrospective found the single
biggest source of wasted tool calls in a per-function review was re-discovering the same handful
of Odoo core facts from scratch each time (roughly 15-20% of one pass's own tool calls, per that
retrospective). Every fact below was independently verified by reading the real source at the
cited path -- not assumed from documentation or a name -- before being recorded here. When a
bug-hunt pass touches multi-company access, record rules, or mail sending, check here first
instead of re-deriving these facts by trial-and-error search.

**Odoo location on this box**: `/usr/lib/python3/dist-packages/odoo/`. Scope `find`/`grep` to this
path (or the target repo itself) rather than searching from `/` -- a full-filesystem search with
`-prune` was one of the concrete waste patterns the retrospective found.

## Multi-company access: `Environment.company` / `.companies`

File: `odoo/orm/environments.py`, `Environment.company` (~line 216) and `.companies` (~line 246).

- Both are `functools.cached_property`. They read `allowed_company_ids` from the environment's own
  context (set by `.with_company(company_id)` -- this is the *only* sanctioned way to set it; do
  not inject `allowed_company_ids` into a context dict by hand, an existing standing rule,
  `hams_shared/docs/adrs/0083_multi_tenant_context_management.md` decision 1).
- **Non-sudo**: if `allowed_company_ids` names any company not in `self.user._get_company_ids()`
  (the acting user's own real company scope), this raises `AccessError` -- immediately, from the
  cached-property getter, before any query executes. This is why a per-company loop that calls
  `.with_company(company)` for a company the acting user/service-account isn't actually scoped to
  crashes hard, not silently skips.
- **Sudo mode is explicitly unchecked**: the docstring's own words, "No sanity checks applied in
  sudo mode! When in sudo mode, a user can access any company, even if not in his allowed
  companies." A `.sudo()` call anywhere in a chain bypasses this whole mechanism -- if a bug-hunt
  claim needs to state whether a multi-company operation is actually access-controlled, check for
  `.sudo()` on the same chain, since its presence makes the `company_ids` restriction moot.
- `res.users._get_company_ids()` (referenced from `environments.py` line 239) is the real source
  of a user/service-account's own legitimate company scope -- read this method's own model, not
  the `company_ids` field alone, if a review needs to state precisely what a given account may
  operate across.

## Record rules: `ir.rule`'s own domain combination

File: `odoo/addons/base/models/ir_rule.py`, `_eval_context` (~line 38), `_compute_domain`
(~line 141), `_compute_domain_keys` (~line 76).

- Multiple `ir.rule` records matching the same model and the same acting user's groups are
  combined with **OR** across groups (`Domain.OR(group_domains)`, ~line 98/172) -- not AND. A
  single unconditionally-permissive rule (e.g. a domain of `[(1, '=', 1)]`) on one group the
  account belongs to grants that permissive access **regardless of** how restrictive any other
  matching rule is. Before treating a record-rule-scoped read as safe, enumerate *every* active
  `ir.rule` on that model that the acting account's own groups could match -- not just the one
  rule that looks relevant -- since the most permissive one wins.
- Global (`global=True`) rules are AND-ed with the OR-combined group rules, not OR-ed in with
  them (comment at ~line 85-91) -- a genuinely different combination rule for global vs.
  group-scoped rules on the same model; don't assume all rules on a model combine the same way
  without checking which kind each one is.

## `res.company`'s own default ordering

File: `odoo/addons/base/models/res_company.py`, `_order = 'sequence, name'` (~line 34).

- `res.company.search([])` (or any unordered search on this model) returns companies in
  `sequence, name` order, **not** creation order and **not** guaranteed to put `base.main_company`
  (or any other specific company) first. A loop that assumes "the main/first company will be
  processed before any others" needs an explicit `order=` or filter to guarantee that -- don't
  assume it from iteration happening to "usually" put main first in a small test database.

## `mail.template.send_mail` / `send_mail_batch`

File: `odoo/addons/mail/models/mail_template.py`.

- `send_mail_batch`'s own `email_values` parameter is applied via `values.update(email_values or
  {})` *after* the template's own qweb-rendered field values are computed -- so any key passed in
  `email_values` (e.g. `email_to`) unconditionally overrides what the template's own expression for
  that field would have produced, even if that expression reads from `ctx`/context values the
  caller never actually set. A template's own `{{ ctx.get('some_key') }}`-style expression can be
  dead code in practice if every real caller already passes that same field via `email_values` --
  check both the template's own field expressions and every call site's `email_values` dict before
  concluding either one is what actually determines the outgoing content.

## Adding to this file

When a bug-hunt pass independently verifies a new Odoo-core fact worth not re-deriving next time
(the same discipline as the skill's own "Growing this list" and "Friction log"), add it here
directly, in the same format: the exact file path and line/function, what was verified (quote the
source if it's a subtle behavior, like the sudo-mode warning above), and why it matters for a
review. Don't let a second pass re-discover the same fact from scratch.
