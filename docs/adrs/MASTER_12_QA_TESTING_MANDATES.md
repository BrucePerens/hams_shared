# MASTER 12: QA & Automated Testing Mandates

## Status
Accepted (Consolidates ADRs 0044, 0049, 0050, 0051, 0052, 0053, 0054, 0058, 0059, 0060, 0061, 0063)

## Context & Philosophy
To guarantee the stability and security of the platform without a massive QA department, all architectural rules and linter bypasses MUST be mathematically proven by exhaustive automated tests before merging.

## Decisions & Mandates

### 1. Fast-Fail Test Pipeline
* CI/CD and deployment scripts MUST execute all static linters and anchor verifiers sequentially and instantly abort on failure before executing database rebuilds.

### 2. Strict Syntactic Parsing Mandate
* We explicitly ban the use of regular expressions for extracting, evaluating, or parsing any structured data. You MUST use native parsers (`lxml.etree`, `ast`, `json`, `yaml`). Regex is strictly confined to evaluating flat text or log lines.

### 3. AST Linter Anti-Evasion Protocols
* The Burn List linter actively blocks "dead code evasion" (placing required assertions after a `return`, `raise`, `break`, or `continue`) and "loop evasion" (wrapping rendering validations like `get_view` inside `for`/`while` loops). Tests must be written securely with genuine, sequentially executed assertions.

### 4. Linter Bypass Testing & AST Verification
* Using a bypass tag (`# burn-ignore`, `# audit-ignore-*`) requires an explicit automated test to prove the bypassed logic is safe. The bypass comment MUST cross-reference the test anchor. The linter performs Deep AST Test Verification to ensure the test function actually contains the required functional assertions.

### 5. Bidirectional Test Anchoring
* Code logic verified by a test MUST include a "Verified by" anchor tag. The test MUST include a "Tests" anchor tag. The CI/CD pipeline enforces this bidirectional mapping.

### 6. XPath Rendering Verification
* A successful `<xpath>` XML insertion does not guarantee the DOM renders correctly. Tests MUST physically execute `get_view()` or `url_open()` to prove the injected payload actually exists in the compiled architecture.

### 7. Cache Query Counting Mandate
* Any method utilizing `@tools.ormcache` MUST be tested using `with self.assertQueryCount(0):` (or cursor mocks) to mathematically guarantee zero SQL executions on a cache hit.

### 8. Security & Architecture Behavior Testing
* **Proxy Ownership & IDOR (Multi-Persona Testing):** Tests must rigorously prove data isolation across the entire spectrum of platform users. You MUST explicitly test all personas (Guest, Standard User, Domain Roles) to actively assert that Unauthorized Personas are violently denied access. Crucially, multi-persona tests MUST always include the Administrator persona (`base.user_admin`) to verify they either possess the correct global overrides or are correctly restricted by immutable architectural blocks (e.g., preventing log deletion).
* **Tests Guard Against LLM-Introduced Regressions, Not Only Human Bugs:** A significant and growing share of changes to this codebase are made by LLM agents working autonomously or semi-autonomously. Multi-persona tests exist as much to catch an agent silently narrowing or widening a security boundary (an ACL row loosened without recognizing the consequence, a `groups` field dropped from an `ir.rule`, a service account's scope quietly widened) as to catch an ordinary human-authored bug. This means: (a) **exclusion tests** (persona X provably cannot see/do Y) are as mandatory as inclusion tests (mechanism Z works), even when nothing currently indicates the code is broken — the value is in pinning down currently-correct behavior so a later change can't regress it unnoticed; and (b) security-relevant changes should extend a module's persona coverage rather than write single-purpose one-off tests, so future changes to that same boundary inherit the full persona matrix automatically. Precedent: `ses_webhook/tests/test_ses_webhook.py::test_10_domain_multi_company_isolation`'s own docstring states this reasoning directly — "Nothing had ever proven it actually isolates two companies from each other."
* **GDPR Erasure:** Tests must assert that calling the erasure hook actually executes the hard-delete cascade.
* **Self-Writeable Fields:** Tests must perform the actual write as a non-admin user on their own record and assert it took effect, not merely inspect the `SELF_WRITEABLE_FIELDS` return value for the field name. See MASTER_10 section 2; enforced by `check_self_writeable_field_tests.py`.

### 9. Transaction Boundaries & Test Realism
* **Anti-Mocking:** The `test_real_transaction.RealTransactionCase` facility MUST be used in favor of mocking to solve cross-thread or cross-transaction isolation issues (e.g., HTTP Controllers, Redis Pub/Sub, external daemons). It completely bypasses Odoo's test cursor wrapping, allowing tests to perform actual `env.cr.commit()` operations.

### 10. The View-Tour UI Mandate
* Every `<template>` and `<record model="ir.ui.view">` defined in XML MUST contain a bidirectional semantic anchor linking it to an automated JS Tour.

### 11. Bug Fix & Regression Testing Mandate
* **Test-Driven Remediation:** When a bug is discovered in production or during QA, the developer MUST write an automated test that specifically reproduces and isolates the bug alongside the fix, if the current test suite does not isolate it sufficiently.
* **Proof of Resolution:** The test suite is considered insufficient if it allowed the bug to manifest. The newly written regression test MUST mathematically fail on the broken code and strictly pass on the patched code, permanently immunizing the platform against regressions.
* **"Regression" includes future LLM-authored changes.** See Section 8's LLM-regression point: a regression test's job isn't finished once it proves today's fix works — it exists to keep working as a tripwire against a later automated change reintroducing the same class of defect, particularly for security/access-control code.
