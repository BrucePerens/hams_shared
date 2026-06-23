---
name: odoo-development
description: Activated when working on Odoo 19+ modules, emphasizing dynamic SQL safety and psycopg2 parameterization.
---

# ODOO-SPECIFIC TECHNICAL STANDARDS

*Copyright © Bruce Perens K6BP. All Rights Reserved.
This software is proprietary and confidential.*

<system_role>
These standards apply specifically to Odoo 19+ module development. This document natively extends `LLM_GENERAL_REQUIREMENTS.md`.
All global operational mandates apply here.
</system_role>

<critical_guardrails>
## 1. ANTI-BIAS & THE BURN LIST (CRITICAL)
Your pre-training data is heavily biased toward older versions of Odoo and sloppy open-source security practices.
You MUST consciously filter your instincts and consult the `docs/LLM_LINTER_GUIDE.md`.
* **The Discovery Mandate:** Whenever a new trap is discovered, update `check_burn_list.py` to programmatically enforce it.
* **Parameterized Queries:** Use parameterized psycopg2 queries directly for data values.
* **Dynamic SQL Mandate:** If you dynamically alter query structure or inject schema identifiers, you are strictly FORBIDDEN from using f-strings.
You MUST use the `psycopg2.sql` module.
</critical_guardrails>

<architecture_rules>
## 2. ARCHITECTURE & COMMUNITY REUSE
* **The Reusability Mandate:** Evaluate existing Odoo 19 Community modules before architecting custom custom CRUD pipelines from scratch.
* **External Daemons & Workers:** Long-running processes or persistent sockets MUST NOT run inside Odoo WSGI workers.
Offload to external Python daemons communicating via JSON-RPC.
* **JIT Binary Self-Healing:** Use `shutil.which` to detect third-party binaries.
If absent, dynamically fetch the pre-compiled static binary.
</architecture_rules>

<orm_standards>
## 3. PYTHON & ORM STANDARDS
* **Constraints:** Use `models.Constraint` (Python class attribute).
* **Zero-Orphan Models:** Every custom model must have a CRUD entry in `ir.model.access.csv`.
* **Inverse Relationships:** For every `Many2one` on Model A, you MUST implement the inverse `One2many` on Model B.
* **Test Isolation Mandate:** In Odoo's `TransactionCase`, `One2many` inverse relations are not automatically populated in the local cache.
If a test relies on a `One2many` field, you MUST explicitly wire the relation in the test's `setUp`.
You are strictly **FORBIDDEN** from replacing `One2many` calls with explicit searches in production code just to bypass this caching artifact.
Call `self.user.invalidate_recordset()` inside tests.
* **Cron Batching:** Long-running scheduled actions MUST process records in manageable batches and programmatically re-trigger themselves.
</orm_standards>

<security_idioms>
## 4. SECURITY PATTERNS & NATIVE IDIOMS
**FORBIDDEN:** You are strictly forbidden from using `.sudo()` to bypass access errors.
**FORBIDDEN:** You MUST NEVER grant `base.group_user` (Internal User) to community members.
All external community access MUST be governed by `base.group_portal` and views (`_auto = False`).
You MUST utilize one of the following native idioms:
* **Centralized Security Utility:** Delegate system param/XML ID retrieval to `zero_sudo.security.utils`.
Prefix methods with `_` to block RPC.
* **Service Account Pattern:** Create an isolated `res.groups` with no human members, and a dedicated internal `res.users` (`is_service_account="True"`).
Execute logic using `.with_user(svc_uid)`.
    * **ORM Cascade Bypass:** Append `.with_context(mail_notrack=True)` to prevent `AccessError` on chatter logs when Service Accounts mutate core identity records. **🚨 CRITICAL TRAP:** NEVER use `prefetch_fields=False` during `.create()` operations or on models without chatter, as it causes a fatal `KeyError: 'record'` in Odoo 17+ ORM caching.
* **Public Guest User:** Grant `perm_create=1` to `base.group_public` for unauthenticated data submission.
* **Impersonation:** Shift environment context: `request.env['target.model'].with_user(user).create(...)`.
* **Login As:** Swap `request.session.uid` directly in HTTP controller, preceded by a `message_post` audit log.
* **Self-Writable Fields:** Override `_get_writeable_fields` on `res.users`.
* **Privilege Hierarchy:** `res.groups` must be nested under a `res.groups.privilege` record.
</security_idioms>

<frontend_and_views>
## 5. XML, VIEWS & QWEB STANDARDS
* **QWeb Logic:** Python built-ins (`getattr`, `hasattr`) are FORBIDDEN in QWeb.
* **Settings Views:** Must inherit `base.res_config_settings_view_form`. Target the form directly using `xpath`.
* **Cross-Module XPath (Dropzones):** To maintain strict Open Source isolation, do not hardcode downstream dependencies in base module views. You MUST use `<xpath>` to inject custom elements into designated "Dropzones" of our own base modules (e.g., injecting domain-specific widgets into `user_websites`). When doing this, you MUST utilize the explicit structural targets and reference the specific Semantic Anchors defined in the target module's `docs/modules/*.md` (or `README.md`) API contract.
* **Translations:** Include an `i18n/` directory containing a `.pot` file and `.po` files for the 7 most popular languages. Use `_()` in Python.
* **Regulatory Compliance:** Integrate with Odoo's native cookie consent mechanism. NO custom cookie banners.
* **GDPR:** Override `_get_gdpr_export_data(self)` and `_execute_gdpr_erasure(self)` on `res.users`.
* **SEO:** Dynamically inject OpenGraph tags into `website.layout` for public-facing profiles/blogs.
</frontend_and_views>

<controllers>
## 6. CONTROLLERS & ROUTING
* **CMS Segregation:** Build applications using dedicated HTTP controllers returning standard QWeb `<template>` views, NOT CMS-editable `website.page` records.
* **CSRF Exemption:** Routes with `csrf=False` MUST physically reside in a file ending in `_api.py`.
* **Anti-Spam:** Unauthenticated POST routes MUST implement reCAPTCHA or honeypots.
* **Explicit Parameter Binding:** Explicitly declare expected form inputs and query parameters in the controller method signature.
* **HTTP Exceptions:** Raise `werkzeug.exceptions.Forbidden()` or `werkzeug.exceptions.NotFound()`. Do NOT raise raw ORM exceptions.
</controllers>
