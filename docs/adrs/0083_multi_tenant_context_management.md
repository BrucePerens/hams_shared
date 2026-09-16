# ADR-0083: Multi-Tenant Context Management (company_id and website_id)

## Status
Accepted

## Context
The Hams.com infrastructure leverages Odoo's native multi-company and multi-website capabilities to support decentralized user websites, global privacy compliance, and multi-tenant domain mapping. Consequently, background daemons, API controllers, and ORM operations frequently access data that must be explicitly scoped to a specific tenant.

Historically, AI models and developers have failed to properly scope environment contexts, relying entirely on the default `self.env` state. This causes two critical failure modes:
1. **Cache Contamination:** Redis cache keys colliding across user websites.
2. **AccessErrors / Phantom Writes:** ORM operations executed by scheduled actions (CRON) or background daemons throwing `AccessError` because the environment's `allowed_company_ids` do not match the target records.

Furthermore, there is ambiguity regarding the "Base" portal versus individualized user websites.

**A specific trap worth naming, found live 2026-09-16**: when a narrow service account hits an
unexplained `AccessError` on `res.users` (or another model carrying Odoo's own built-in
multi-company `ir.rule`), the obvious fix -- add a new, permissive, group-scoped `ir.rule` for
that service account's own group -- can have NO effect at all, silently. Odoo combines `ir.rule`
records in two tiers: rules with an explicit `groups` field are OR-combined among themselves (any
one matching is enough), but that OR-combined result is then AND-combined with every GLOBAL rule
(no `groups` field) that exists for the model. `res.users` ships a global rule from Odoo's own
`base` module (`res_users_rule`, `base/security/base_security.xml`) restricting a "share"
(portal/public-type) user's visibility to same-company viewers. A new group-scoped grant, however
permissive, is ANDed against that global rule, not substituted for it -- it cannot widen access to
a target in a different company. The fix is not "add a wider group rule"; it is either genuine
`.with_company()` scoping to a company the acting env is actually authorized for, or -- often
better -- restructuring the operation so it never needs to `check_access('read')` on the
cross-company row at all (e.g. an ORM write that implicitly validates a newly-linked Many2many
co-record can sometimes be replaced with a narrower write that doesn't touch that relation).

## Decisions

### 1. Mandatory use of `.with_company()` for Cross-Boundary Operations
You MUST use the ORM method `.with_company(company_id)` to alter the environment context whenever background daemons, cron jobs, or API endpoints execute operations that write or evaluate multi-company records.
* Do not manually inject `allowed_company_ids` into the context dictionary. `.with_company()` is the architecturally mandated abstraction.
* **Example:** `self.env['ham.callbook'].with_company(target_company.id).search(...)`

### 2. Website & Company in Distributed Cache Keys
You MUST isolate all distributed cache entries by tenant. Any Redis cache key that stores user-facing, location-based, or UI-rendered data MUST include both the `company_id` and `website_id`.
* **Standard Key Format:** `ham_module:feature:{company_id}:{website_id}:{unique_identifier}`
* **Fallback:** If an API endpoint is hit outside of a website request context, `website_id` MUST gracefully fallback to `0`.

### 3. The "Hams.com" Portal Company Definition
There IS a primary company assigned to the central ham radio portal. The root Hams.com portal operates under the system's default base company (ID `1`), while decentralized "User Websites" operate under dynamically provisioned child companies or discrete websites tied to the base company depending on the user's tier.

Therefore, global shared services (e.g., Space Weather telemetry, FCC syncing) MUST map their default un-scoped data to the root company to ensure global propagation.

## Consequences
* **Enhanced Data Isolation:** Prevents SSRF or cache-poisoning attacks where one user website could potentially render data intended for another.
* **Stable Daemon Execution:** Background tasks will no longer fail intermittently due to record rule restrictions when operating on global directories (like `ham.callbook`).
* **Slight Overhead:** Developers and AI agents must proactively fetch the `company_id` from the current user or record and the `website_id` from the `request` object (via an explicit `try...except AttributeError` block accessing `request.website`) during API construction.
