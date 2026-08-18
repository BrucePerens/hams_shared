# ADR 0085: hams_com Single-Site Deployment Policy

## Status
Accepted

## Scope
This ADR applies to `hams_com`-authored code only (models, security data, and
tests that live in a `hams_com` module, not inherited from a `hams_open`
mixin). It does not change anything about `hams_open`, which remains a
general-purpose library intended to serve deployments other than hams.com
and MUST continue to support genuine multi-company and multi-website use
correctly (see MASTER_10/MASTER_12 and the multi-tenant test-coverage audit
this ADR's companion work performed against `hams_open`).

## Context
`hams_com` is the application code for exactly one production deployment:
hams.com. There is exactly one `res.company` and one `website` record in a
real hams.com database. A candidate feature that might once have argued for
a second site -- ICS/first-responder training content for a non-ham
audience -- has been decided the other way: that content will live on
hams.com itself, addressed to the same single company and website, not on a
logically separate site. There is no currently planned scenario in which
hams.com becomes multi-company or multi-website.

Several `hams_com`-authored `ir.rule` records nonetheless scope their
`domain_force` by `company_id` and/or `website_id` (found in
`ham_base/security/security_rules.xml`, `ham_dns/security/security_rules.xml`,
`ham_events/security/security_data.xml`, and
`ham_shack/security/security_rules.xml` as of this writing). On a
single-company, single-website deployment, `('company_id', 'in',
company_ids)` and equivalent `website_id` clauses can never actually
restrict anything -- `company_ids` is always the one company, every real
record already carries that company's id -- so the clause is inert in
production. It is not free, though: it is a permanent maintenance and
correctness liability. The bidirectional-rule-combination bug found and
fixed this session in `pager_duty` (two same-group `ir.rule` records
combine with OR, not AND, in Odoo, so splitting a website rule and a
company rule silently defeats both) is exactly the class of bug this kind
of scoping invites, and it is being carried in `hams_com` for a scenario
that will never occur.

## Decision

1. **New `hams_com`-authored models, fields, and `ir.rule` records MUST NOT
   add `company_id`/`website_id` multi-tenancy scoping.** This targets
   isolation *rules* specifically, not the mere presence of a `company_id`
   field where Odoo core itself requires one (e.g. every `res.users` record,
   service accounts included, needs a `company_id`) -- that is population
   of a required field, not tenancy logic, and is unaffected by this policy.

2. **Fields and rules inherited from a `hams_open` mixin are exempt and MUST
   be left as-is.** `hams_open` is shared, general-purpose infrastructure
   that other deployments may legitimately run multi-company or
   multi-website. When a `hams_com` model inherits a mixin such as
   `user_websites.owned.mixin`, it receives that mixin's fields and
   behavior as part of using the shared architecture; `hams_com` code MUST
   NOT attempt to strip or special-case them for the single-site case, as
   doing so would fight the shared mixin's own guarantees.

3. **The four existing files listed under Context are simplification
   candidates, not bugs.** They are not incorrect on today's single-company,
   single-website deployment; they are unnecessary. Simplify opportunistically
   (drop the `company_id`/`website_id` branch from the `domain_force`, keep
   the rest of the rule) rather than as an urgent remediation.

4. **A future decision to stand up a genuinely separate site or company
   within the `hams_com` deployment is an architecturally significant
   change that MUST revisit this ADR explicitly**, not something introduced
   incidentally through a single feature's `ir.rule`.

## Consequences
`hams_com` code stays simpler and avoids a real, previously-demonstrated bug
class (same-group `ir.rule` OR-combination) for a tenancy scenario that
cannot occur in production. `hams_open` is unaffected and continues to be
held to the full multi-tenant correctness bar, since it serves deployments
this ADR does not apply to.
