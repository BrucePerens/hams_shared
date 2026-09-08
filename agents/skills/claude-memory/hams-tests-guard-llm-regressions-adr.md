---
name: hams-tests-guard-llm-regressions-adr
description: "On hams.com/hams_open/hams_com, tests exist to guard against LLM-introduced regressions, not just code bugs -- this mandates broader test coverage, especially for security rules, and should be codified as a standing ADR."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: a5bd8915-8036-4ef1-8db6-088f583dcf5c
  modified: 2026-08-22T16:53:28.968Z
---

While designing a tenant-count-independent fix for a privilege-escalation finding in `ses_webhook`
(replacing `.sudo()` calls and groups-less `ir.rule` records with a service-account architecture),
the user articulated a standing testing philosophy for the hams.com/hams_open and hams_com
codebases that goes beyond this one fix: tests are not only there to catch ordinary code defects,
they are there to guard against an LLM agent introducing a regression during autonomous or
semi-autonomous work — for example, an LLM widening an ACL row, removing a `groups` field from an
`ir.rule`, or otherwise loosening a security boundary without recognizing the consequence. Because
LLM-driven changes are a distinct and recurring risk in how this codebase gets modified, the user's
explicit conclusion was: "This mandates more test coverage, especially for any security rule."

The concrete instruction was "Let's make this an ADR" — meaning this principle should be written up
as a real, durable Architecture Decision Record in the codebase's own ADR series (this codebase
already has an established numbered ADR convention referenced throughout its own tooling and
security linter, e.g. ADR-0083, ADR-0022, ADR-0054, ADR-0081, ADR-0086), not just applied ad hoc to
the one fix in progress.

Practical implications for future work in this codebase:
- When adding or modifying any security-relevant rule (`ir.rule`, `ir.model.access.csv`, group
  membership, service-account scoping), write an explicit regression-guarding test for it, not just
  a test that the feature works — the test's purpose is to fail loudly if a *future* change
  (especially an LLM-driven one) silently narrows or widens access.
- This includes writing "exclusion" tests (a tenant/persona provably CANNOT see what isn't theirs)
  even when nothing currently indicates the code is broken — the value is in pinning down
  currently-correct behavior so it can't regress unnoticed, matching the existing precedent already
  established in this codebase's own `test_10_domain_multi_company_isolation`-style tests (whose own
  docstring says exactly this: "Nothing had ever proven it actually isolates two companies from each
  other").
- Multi-persona test coverage (verifying different classes of users — different tenants, portal
  users, service accounts — get correctly different outcomes from the same code path) is the
  user's explicit standing expectation for security-relevant work in this codebase, not a one-off
  ask for this particular fix.
