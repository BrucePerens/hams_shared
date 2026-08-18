# MASTER 10: Core Identity & Access Control

## Status
Accepted (Consolidates ADRs 0006, 0008, 0015, 0036)

## Context & Philosophy
Balancing an open community platform with stringent anti-spam and anti-hijacking controls requires nuanced authorization patterns that go beyond standard Odoo backend permissions.

## Decisions & Mandates

### 1. Proxy Ownership Pattern (0008)
* Users manage personal websites and blogs. Because Odoo restricts core UI creation (`ir.ui.view`) to administrators, models MUST inherit the `user_websites.owned.mixin`.
* This allows users to assign themselves as the proxy `owner_user_id`. Controllers temporarily escalate to a Service Account strictly to execute the database write, constrained mathematically by the mixin validating ownership context.
* **CMS vs. App Segregation:** Because `website.page` creation triggers these Proxy Ownership intercepts, system facilities and interactive apps MUST be built as standard HTTP controllers returning `<template>` views. Do not create `website.page` records for static or dynamic app views unless the paradigm explicitly requires end-user drag-and-drop CMS editing.

### 2. The "Self-Writable Fields" Idiom (0015)
* To allow users to modify their personal settings on the locked `res.users` table without `.sudo()`, models MUST override the `SELF_WRITEABLE_FIELDS` property (`super().SELF_WRITEABLE_FIELDS + [...]`) and explicitly append the allowed preference fields. This is the actual hook Odoo core's `res.users.write()` consults for the self-write bypass; a method named `_get_writeable_fields` does not exist in core and is never called by it.
* **Mandatory write-proof test shape:** a test claiming to verify a `SELF_WRITEABLE_FIELDS` override MUST perform the actual write, as a non-admin user, on that user's own record (`some_user.with_user(some_user).write({...})`), then assert the field's new value took effect. Merely asserting a field name appears in `SELF_WRITEABLE_FIELDS`'s return value is NOT sufficient and does not satisfy this mandate: several modules once overrode a method Odoo core never calls at all (`_get_writeable_fields` instead of the real property above), and their tests still "passed" for years because they only checked list membership, never that a real write actually landed. The override MUST carry a `# Verified by [@ANCHOR: <name>]` tag, and the test proving the write MUST carry the matching `# Tests [@ANCHOR: <name>]` tag (see MASTER_12 section 5, Bidirectional Test Anchoring). `hams_shared/tools/check_self_writeable_field_tests.py`, run as a standard lint step, statically enforces both the anchor linkage and the write-proof shape (a `.write(...)` call, a `.with_user(...)` call, and an assertion textually after the write) via AST inspection of the linked test.

### 3. Public Guest User Idiom (0036)
* Unauthenticated public submissions (like violation reports) MUST NOT use `.sudo().create()` in the controller.
* Instead, the model MUST grant `perm_create=1` explicitly to `base.group_public` via `ir.model.access.csv`, relying on database-level Access Control.

### 4. Secure Admin Password Management (0006)
* The master database password MUST NEVER be stored in plaintext. It must be pre-hashed (PBKDF2-SHA512) via the `tools/hash_admin_password.py` utility. The core initialization module uses raw SQL to inject it, bypassing Odoo's double-hashing ORM.

### 5. Domain Sandbox Mandate (No ERP Group Escalation)
* Standard community users MUST NOT be assigned native Odoo ERP backend groups. Specifically, you MUST NEVER grant `base.group_user` (Internal User) to a community member, as this exposes the `/web` backend, employee directories, and internal chatter.
* * All community users must be restricted to `base.group_portal` combined with domain-specific groups (e.g., `custom_module.group_custom_operator`).
* * Record Rules and ACLs across all custom modules must evaluate against `base.group_portal` or the specific domain groups, NOT `base.group_user`.
* * If a user requires elevated rights to interact with a native Odoo model (such as submitting a `survey.user_input`), the transaction MUST be proxied through a Service Account using the Zero-Sudo architecture. Remember that views must be used preferentially rather than increasing privilege when a view will work..

### 6. User Impersonation & Accountability
* Administrators investigating specific user dashboard states must use a secure "Login As" facility to temporarily swap their `request.session.uid`.
* To maintain zero-trust accountability and prevent administrative abuse, this action MUST instantly trigger an immutable `message_post` to the target user's chatter history, logging the administrator's exact name and the intrusion timestamp.
