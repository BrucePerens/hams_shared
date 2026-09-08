---
name: hams-zero-sudo-philosophy
description: "The hams.com/hams_open codebase's standing minimum-privilege architecture — sudo is banned outright, replaced by narrowly-scoped service accounts, and any privilege-boundary change needs an explicit security justification before it's added."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-18T02:38:13.038Z
---

The hams.com/hams_open/hams_shared codebase follows a minimum-privilege philosophy distinct from (but complementary to) its fail-fast philosophy. `.sudo()` and `SUPERUSER_ID` are banned outright and enforced by an AST burn-list linter — not merely discouraged — because they are a "shotgun" privilege escalation that grants far more access than any single operation actually needs. In their place, the codebase uses specialized, narrowly-scoped service-account user IDs (provisioned via `zero_sudo.security.utils._get_service_uid()` and switched to with `with_user()`), each created to do exactly one job and holding exactly the group memberships that job requires — Postgres itself enforces this at the database level via `zero_sudo_get_service_uid()`, which raises if a service account is ever granted `base.group_system` or `base.group_erp_manager`. When code needs elevated access, the fix is to create or reuse a purpose-built service account, not to reach for broader privilege.

This same philosophy extends to the `ir.config_parameter` service-account read/write whitelist: the goal is not just to patch whatever key happens to crash next, but to only ever whitelist a parameter once its safety can be affirmatively argued (e.g., it is a plain non-secret operational setting that core Odoo reads incidentally as a side effect of a routine framework call, never a credential or anything with per-record security implications). The user is explicitly wary of expanding this whitelist reactively and expects a security justification — convincing enough to earn their buy-in — before any new parameter or privilege-boundary pattern is added, not just evidence that a test currently fails without it.
