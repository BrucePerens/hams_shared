---
name: hams-own-model-extension-preference
description: "For hams.com/hams_open models the codebase itself defines, prefer adding fields/views directly to the base model instead of extending it from another in-house module via Odoo's _inherit -- now codified as ADR 0086."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-18T23:45:54.868Z
---

When one hams.com/hams_open module extends a model that *another module in this same codebase*
already defines (as opposed to a genuinely third-party model like Odoo core's `res.partner` or
`res.users`, which can only be extended via `_inherit` since the base definition isn't ours to
edit), the user's stated preference is to move the extension's fields, methods, and view changes
directly into the base model's own file instead of adding a second, cross-module `_inherit`
declaration. This is now a formal, numbered rule: ADR 0086 (`hams_open/hams_shared/docs/adrs/
0086_own_model_extension_consolidation.md`), added at the user's explicit request after this
pattern kept producing real bugs, not just style debt.

The pattern has repeatedly caused genuine, hard-to-diagnose defects, each invisible from reading
either module in isolation because the bug lived in the *interaction* between two files that both
assumed they fully controlled the model:
- `ham.equipment` (`ham_profile`/`ham_shack`): `_name = "ham.equipment"` declared alongside an
  unrelated `_inherit` mixin, omitting itself from `_inherit` -- crashed Odoo's registry loading.
- `ham.elmer.topic` (`ham_club_management`/`ham_onboarding`): the same model declared
  independently by two modules with no `_inherit` relationship at all; Odoo's registry silently
  merges same-`_name` classes by load order, so which module's `ir.model.access.csv` rows even
  resolve depends on install order, not on anything either module declares.
- `ham.repeater.public.view` and `ham.operator.index` (both `_auto = False` SQL-view models): the
  base model builds its table via a hand-written `init()`; a cross-module `_inherit` class also
  overrode `init()`, and neither called `super()`, so only one version's `CREATE VIEW` ever ran.
  The losing side's extra columns were live ORM fields with no backing database column --
  `ham.operator.index` was worse, since the *extension's* `init()` was the one that won and it
  silently dropped the base's `DISTINCT ON`, its `callsign IS NOT NULL` filter, and even the
  `name` column the model declares as a stored field.
- `ham.dx.spot` (`ham_shack`/`ham_dx_cluster`): the base AbstractModel permanently blocks
  `create()`, so the extending module's six declared fields were dead on arrival; untangling it
  also surfaced an unrelated live bug (a controller calling a method name that never existed
  anywhere in the class, silently swallowed by a broad `except`).

ADR 0086 adds two refinements beyond the original preference:
1. **`_auto = False` SQL-view models get an absolute ban on cross-module extension, no exemption**
   -- a second `init()` override on a hand-written-view model is a load-order-dependent
   correctness bug by construction, not a style issue, since Odoo has no `super()`-chaining
   contract for `init()` across `_inherit`.
2. **The exemption for genuinely optional/pluggable modules has a concrete test**: a cross-module
   `_inherit` is unavoidable, and must be left alone, whenever the extension references the
   *extending* module's own models/service accounts/utilities -- merging it into the base model
   would require the base module to gain a dependency on the module that already depends on it,
   which Odoo's acyclic module graph can't support. Confirmed examples that must stay as `_inherit`:
   `pager_duty` extending `zero_sudo.security.utils`, `cloudflare` extending
   `edge.routing.domain`, `ham_sk_workflow` extending `zero_sudo.security.log` (via
   `selection_add`) and `hams_helpdesk.ticket`, `ham_training` extending `ham.testing.progress`,
   `ham_relay_bridge` extending `ham.equipment`. The tell: if satisfying the preference would mean
   the base module importing from, or gaining a manifest dependency on, the extending module, the
   extension is exempt.
