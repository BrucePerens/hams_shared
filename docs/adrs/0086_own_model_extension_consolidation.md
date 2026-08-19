# ADR 0086: Own-Model Extension Consolidation

## Status
Accepted

## Scope
This ADR governs how in-house modules (any module in `hams_com` or
`hams_open`) extend a *model this codebase itself defines*. It does not
apply to `_inherit` of genuine Odoo/OCA core models (`res.users`,
`res.partner`, `mail.thread`, etc.), and it does not apply to mixins
designed for multiple modules to compose (e.g. `user_websites.owned.mixin`).

## Context
Several in-house models were split across two modules using `_inherit`
(module B adding fields/methods to a model module A defines) purely because
the feature landed in a separate module, not because the split served any
architectural purpose. A single afternoon auditing this pattern found real,
load-bearing bugs behind it, not just style debt:

- **`ham.equipment`** (`ham_profile` extending `ham_shack`): declared with
  `_name = "ham.equipment"` *and* `_inherit = ["user_websites.owned.mixin"]`
  -- omitting itself from `_inherit` -- which crashed Odoo's registry
  loading the moment a third module (`ham_relay_bridge`) tried to
  `inherit_id`-extend the view, with "Field X does not exist".
- **`ham.elmer.topic`** (`ham_club_management` and `ham_onboarding` each
  declaring the model independently, no `_inherit` at all): Odoo's registry
  silently merges same-`_name` classes by load order with no formal
  dependency between the two declaring modules. Which module's `ir.model`
  xmlid actually gets created -- and therefore which module's
  `ir.model.access.csv` rows resolve at all -- depends on install order,
  not on anything either module declares.
- **`ham.repeater.public.view`** and **`ham.operator.index`** (both
  `_auto = False` SQL-view models extended cross-module): the base model
  builds its table via a hand-written `init()` that issues its own
  `CREATE OR REPLACE VIEW`. A second module's `_inherit` class also
  declared its own `init()` override, and neither called `super()`. Only
  one `init()` wins the merge, so the *other* module's added fields were
  live ORM field declarations with **no backing database column** --
  `ham.operator.index`'s case was worse: the extension's version of
  `init()` was the one that actually ran, and it silently dropped the base
  version's `DISTINCT ON`, its `callsign IS NOT NULL` filter, and the
  `name` column the model itself declares as a stored field.
- **`ham.dx.spot`** (`ham_shack` extending `ham_dx_cluster`'s AbstractModel):
  the base model permanently blocks `create()` (a deliberate zero-DB
  ephemeral-store pattern), making the extending module's six declared
  fields dead on arrival -- they could never be populated. Untangling it
  surfaced a second, independent bug: a controller called a method name
  (`create_spot_rpc`) that had never existed anywhere in the class,
  silently swallowed by a broad `except Exception`.

In every one of these cases, the bug was invisible from reading either
module in isolation -- it only existed in the *interaction* between two
files that both assumed they fully controlled the model.

## Decision

1. **When an in-house module needs fields or behavior on a model this
   codebase itself defines, that code MUST go directly into the model's own
   base file, not into a separate `_inherit` extension in another module,
   unless a cross-module `_inherit` is unavoidable** (see Exemption below).
   This applies even retroactively: when working in a model's base file for
   an unrelated reason and a stray cross-module extension of it is noticed,
   fold it in as part of that work rather than leaving it.

2. **`_auto = False` SQL-view models are never extended cross-module,
   full stop, with no exemption.** A second `init()` override on a
   `_table_query`/hand-written-view model is not a style problem -- it is a
   silent, load-order-dependent correctness bug by construction, since
   Odoo has no `super()`-chaining contract for `init()` across `_inherit`.
   All columns for a given view live in one file with one `init()`.

3. **Exemption: genuinely optional/pluggable modules.** A cross-module
   `_inherit` is unavoidable, and MUST be left alone, when the extension
   references the *extending* module's own models, service accounts, or
   utilities (e.g. `pager_duty` extending `zero_sudo.security.utils`,
   `cloudflare` extending `edge.routing.domain`, `ham_sk_workflow` adding a
   `selection_add` value to `zero_sudo.security.log`). Merging these into
   the base model would require the base module to depend on the module
   that already depends on it -- Odoo's module graph must stay acyclic, so
   this is a hard constraint, not a preference. The tell: if satisfying
   this ADR for a given extension would mean the base module importing
   from, or gaining a manifest dependency on, the extending module, the
   extension is exempt.

4. **When in doubt about which module should own a model that's about to
   gain a cross-module extension, prefer the module the referencing code
   (views, other models, `ir.model.access.csv`) most depends on -- usually
   whichever module loads first in the dependency graph.**

## Consequences
Fewer models have their real shape split across files that don't know
about each other, which is exactly the condition that let the bugs above
go undetected. `_auto = False` view models in particular become
single-owner by rule, eliminating an entire class of silent
`init()`-collision bugs. The exemption in (3) keeps this from fighting
Odoo's own plugin architecture: optional modules (`cloudflare`,
`pager_duty`, `ham_sk_workflow`, `ham_relay_bridge`, `ham_training`, etc.)
keep using `_inherit` for their own concern-specific fields, since folding
those into a shared foundational module would trade one bug class for a
worse one -- an un-loadable dependency cycle, or a foundational module that
silently accretes optional-feature-specific fields no other deployment of
it wants.
