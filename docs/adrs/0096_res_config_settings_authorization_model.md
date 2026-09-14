# ADR 0096: `res.config.settings` Authorization Is All-or-Nothing, By Design

## Status
Accepted

## Context
`night_shift_todo.md` ("saving ANY Settings page can crash with an AccessError", hams_com,
2026-09-13) found that `user_websites/models/res_config_settings.py`'s own `set_values()` override
unconditionally rewrote `group_user_websites_administrator.user_ids` on every single Settings save,
company-wide, regardless of which page's fields actually changed. `res.config.settings` is one
`TransientModel` Odoo's own MRO chains through EVERY installed module's own `get_values()`/
`set_values()` override, unconditionally, on every save -- so this one module's side effect on a
security group's membership cascaded into `mail`'s own discuss-channel resubscription, which needed
a real `ir.config_parameter` secret a service account is correctly never granted, crashing the
entire settings-save request for whatever page a human administrator was actually trying to save.

Bruce's own response, verbatim, to three proposed patches for that one crash: "This sounds wrong, it
sounds like we haven't established what each user can set, and we haven't provided a coherent
general solution to the problem." The investigation that followed found the crash was a symptom of
two independent design errors, not a fluke:

1. `user_websites` was the only Settings-contributing module, across both `hams_com` and
   `hams_open`, whose `set_values()`/`get_values()` had any side effect beyond persisting a plain
   scalar `config_parameter` value. Every other contributor (`ham_dns`, `ham_training`, `web_map`,
   `caching`, `cloudflare`, `distributed_redis_cache`, `hams_base`, `hams_s3`, `advertising`) only
   ever writes independent scalar fields, which is exactly what `res.config.settings`'s own
   "fires unconditionally on every unrelated save" contract can safely tolerate.
2. `user_websites/security/ir.model.access.csv` was the only place in either repo granting
   `res.config.settings` model-level access to any group other than `base.group_system` --
   specifically full read/write/create/unlink to `group_user_websites_administrator`, a
   content-moderation-tier role that does not imply `base.group_system`. `ir.model.access.csv`
   grants are per (model, group), never per field, so this was never actually scoped to that
   module's own 3 fields; it handed that role read/write on every field any installed module merges
   onto the shared model (confirmed concretely: `distributed_redis_cache`'s `redis_password`,
   `cloudflare`'s `cloudflare_api_token`). The "General Settings" menu item independently requires
   `base.group_system` (`base_setup.menu_general_settings`), so this was never reachable through
   normal navigation, but `ir.model.access.csv` is enforced by the ORM regardless of menu
   visibility -- a direct RPC call from that role, bypassing the menu, was not.

Checking vendored Odoo core (`account`, `hr`, `base_setup`, and `base` itself) confirmed this is not
a gap in Odoo's own model: `base/security/ir.model.access.csv` grants `res.config.settings` to
`base.group_system` ONLY (`1,1,1,0`), full stop, in the entirety of Odoo core. The `groups="..."`
attribute core modules use throughout their own `res_config_settings_views.xml` (`account.
group_account_manager`, `hr.group_hr_manager`, etc.) is layered ON TOP OF that mandatory
`base.group_system` gate -- it narrows which sections a System Administrator sees, never a
substitute for the model-level write requirement, and nobody who lacks `base.group_system` reaches
the Settings form at all. Odoo's own equivalent for "manage an arbitrary group's membership"
(`res.groups`'s own form view, `base.action_res_groups`) is likewise gated to `base.group_erp_manager`
-- effectively the same System-Administrator tier -- with no narrower "delegate management of just
this one group" mechanism built into core anywhere. `user_websites` wasn't discovering a real gap
Odoo left unfilled; it invented a bespoke mechanism to solve a problem Odoo already solves more
safely with its own Users & Groups UI.

## Decision

1. **`res.config.settings` write access is `base.group_system` only, full stop.** No module in
   either repo may add its own `ir.model.access.csv` row naming a different group for this model.
   A future row like that is the same mistake recurring, not a legitimate delegation pattern --
   there is no such thing as "let this narrower role edit just its own Settings fields," because
   `ir.model.access.csv` grants are per model, not per field.
2. **View-level `groups="..."` on `<app>`/`<block>`/`<setting>` is a visibility toggle, never a
   security boundary.** It may be used, matching core Odoo's own idiom, to hide a section from a
   System Administrator who lacks some additional functional group -- but the underlying field
   remains writable by anyone who already holds `base.group_system`, and no module may rely on it
   as the only gate for a sensitive field.
3. **`set_values()`/`get_values()` overrides may only persist independent scalar
   `config_parameter`-backed fields.** No side effects on any other model (a security group's
   membership, a `res.users` record, cascading business logic) belong here, because this method
   fires unconditionally on every installed module's Settings save, regardless of which page's own
   fields actually changed -- a side effect here must be safe to run constantly, on every save, for
   every reason, including reasons that have nothing to do with the field that triggered it.
4. **Delegating a narrower, module-specific administrative role goes through Odoo's own Users &
   Groups mechanism** (`res.groups`'s own form view / `base.action_res_groups`, or a `res.users`
   form's Access Rights tab when the role shares a `res.groups.privilege` with a broader group in
   the same category) -- never reimplemented as a bespoke field-plus-write on `res.config.settings`.
   That surface is already correctly gated and is not wired through the unconditional
   `set_values()` mechanism, so it cannot reproduce this failure mode.

## Consequences
`user_websites`'s `user_websites_administrators_ids` field and its `get_values()`/`set_values()`
overrides were removed outright (not patched) as the first real application of this ADR --
`night_shift_todo.md` records the specific commit. Settings now offers a direct link/button to the
group's own record in the Groups screen instead of reimplementing membership management. The
deleted `ir.model.access.csv` row is not replaced by a narrower one; `group_user_websites_
administrator` now has no access to `res.config.settings` at all, matching every other
non-`base.group_system` group in both repos. Any future module considering a similar "let this
specific role manage this one setting, from Settings" design should read this ADR first and use the
Users & Groups mechanism (decision 4) instead of re-deriving the same mistake.
