# Hams Test Infrastructure (`zero_sudo`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

This module is the unified testing infrastructure for the repository. It consolidates the Real Transaction Testing facility, the Daemon Integration Testing framework, and the UI Tour governance standards into a single, cohesive architecture.

---

## 1. Real Transaction Testing Facility

In standard Odoo testing, `TransactionCase` wraps the entire test execution inside an uncommitted PostgreSQL `SAVEPOINT`. While this makes tests extremely fast and prevents database pollution, it creates an artificial environment. This environment fundamentally breaks the ORM's local memory cache for inverse relational fields (like `One2many` relationships) and makes testing distributed workers impossible.

The `zero_sudo` module solves this by providing: **`RealTransactionCase`**.
**Because it inherits from Odoo's `HttpCase`, it natively supports full Werkzeug HTTP routing and testing utilities like `self.authenticate()` and `self.make_jsonrpc_request()`.**

### True Database Commits
You can safely call `self.env.cr.commit()` in your tests. This allows developers to write accurate tests for cross-worker cache invalidations (e.g., Redis pub/sub buses), background daemon polling, and lazy-loaded ORM relations.

### Automated ORM Tracking & Cleanup
Because real commits permanently write to the database, the facility dynamically instruments Odoo's `BaseModel.create()` during `setUp()`. It tracks the ID of every record created via the ORM and automatically executes a hard-delete (`unlink()`) on all tracked records during `tearDown()`. It uses a multi-pass approach (up to 5 attempts) to handle complex Foreign Key hierarchies.

### SQL Leak Detection
To guarantee a pristine database, the facility takes a mathematical snapshot of the exact row count of every table in the `public` PostgreSQL schema before the test begins. During `tearDown()`, it recounts the tables. If your test leaked data (e.g. via raw SQL `INSERT` without manual cleanup), the test will immediately crash with an `AssertionError`.

**Usage Example:**
```python
from odoo.tests.common import tagged
from odoo.addons.zero_sudo.tests.real_transaction import RealTransactionCase

@tagged('post_install', '-at_install')
class MyAdvancedTest(RealTransactionCase):
    def test_01_real_commit_behavior(self):
        user = self.env['res.users'].create({'name': 'Test User'})
        self.env.cr.commit()
```

---

## 2. Integration Daemon Testing (`HamsTransactionCase`)

This facility provides an automated execution wrapper to spin up external Python daemons (e.g., Pager Duty Spoolers, Redis Cache Managers) and run real HTTP/XML-RPC requests against them during the test phase.

* **Lifecycle Management:** It utilizes Python's `subprocess` to boot daemons in `setUpClass()` and guarantees zombie processes are SIGKILL'd in `tearDownClass()`.
* **Health Polling:** It automatically polls the daemon's port/endpoint until a `200 OK` is returned, preventing race conditions where test requests fire before the daemon binds to the socket.

---

## 3. UI Tour Development Guide

This module provides governance and utilities for writing robust UI tours in Odoo 19.

### The "Gold Standard" (Mandatory Tours)
A JavaScript UI Tour (`web_tour`) **MUST** be written for views meeting any of the following criteria:
* **Critical User Journeys:** High-stakes workflows such as upgrades, purchases, or administrative verifications.
* **Complex State Machines:** Form views utilizing dynamic `invisible`, `readonly`, or `required` attributes.
* **Custom Widgets:** Any view injecting custom JavaScript components.

### Justified Exceptions (When to use `<!-- burn-ignore-tour -->`)
The bypass tag is strictly reserved for scenarios where the ROI of a DOM tour is zero:
* **Simple Dictionary / Lookup Tables:** Basic CRUD views with no complex interactions.
* **Invisible / Programmatic Views:** Views designed to be invoked silently by background processes.
* **Read-Only Audit Logs:** Backend history views where user data mutation is impossible.
* **Micro-Inheritances:** Views inheriting a base view solely to inject a single `invisible="1"` field, an `xpath` removal, or a basic domain filter.

### Tour Targeting & Selectors
Tours MUST use schema-compliant selectors to deterministically handle race conditions.

**BANNED TARGETS:**
* `.col-md-6` and generic layout classes.
* `a:contains(...)`, `button:contains(...)`, `h1:contains(...)` (brittle translated strings).
* `data-*` attributes (breaks backend XML schema validation).

**Allowed Targeting Hierarchy:**
1. **Primary:** Native `name` attributes (e.g., `button[name="action_install"]`, `field[name="is_installed"]`).
2. **Secondary:** Structural IDs (`#wrap`, `.o_form_sheet`).
3. **Fallback:** Dedicated namespaced CSS classes starting with `o_tour_` (e.g., `.o_tour_create_site_btn`).

### Common Environmental Traps & Solutions (CRITICAL FOR AI AGENTS)
When developing tours in Odoo 19, you **MUST** navigate several strict environmental traps:

* **The Modal "Below a Modal" Trap:** Odoo's engine actively blocks clicks on background elements. A generic selector like `.btn-primary` will often match the background form instead of the active modal. You MUST scope your selectors to the active dialog (e.g., `.modal.show button[name='confirm_method']` or `.o_dialog button[name='...']`) and include a preliminary step to wait for the modal animation to finish rendering.
* **The Text Wrapping Trap:** You MUST NEVER use `a:contains(...)` or `button:contains(...)`. Odoo 19 frequently wraps internal button text in nested `<span>` tags, which instantly breaks these selectors. You MUST use `*:contains(...)` or target explicit attributes like `[data-menu-xmlid=...]`.
* **The Dropdown Trap:** Native `<select>` elements are deprecated in backend form views. Odoo 19 uses `.o_select_menu`. You MUST use two separate tour steps for dropdowns: one to click the dropdown container, and a subsequent step to click the `.o_select_menu_item`.
* **The Initialization Race Condition:** You MUST NEVER start a tour by having the first step manually execute `document.location.href = ...`. This causes severe race conditions. You MUST initialize the tour using the native `url: "/path"` property within the tour definition block.
* **The `expectUnloadPage` Trap:** You MUST NOT set `expectUnloadPage: true` on steps where navigation is conditional or when triggering an OWL soft-route (client action). If the page does not execute a hard browser reload, the Odoo test runner will fatally timeout after 20,000ms.
* **The Save Button Crash (Dirty Forms):** You MUST NEVER manually click the save button (`.o_form_button_save`) and immediately end the tour or navigate away. This leaves a dirty form view open, causing asynchronous network requests that corrupt subsequent tests. You MUST spread the `...TourUtils.safeSave()` macro into your step array to force a DOM blur and wait for the `.o_form_button_create` state.
* **Asset Registration Silent Failures:** Ensure your JavaScript tour files are actually loading. They MUST be explicitly declared in the module's `__manifest__.py` under the `assets` dictionary within the `web.assets_tests` key.

### Robustness Macros (`TourUtils`)
To combat Owl's asynchronous rendering delays and other race conditions, import `TourUtils` from `@zero_sudo/js/tour_utils`. It provides several safety macros you can spread into your tour steps:
* `TourUtils.waitForAbsence(selector, description)`: Pauses the tour until the specified element (e.g., a loading overlay or an old modal) is entirely removed from the DOM.
* `TourUtils.safeSave(saveTrigger, waitTrigger)`: Safely executes a form save by enforcing a DOM blur before clicking the save button and waiting for the RPC resolution.

---

## 4. Tour Failure Diagnostics (The Skeleton Dump)
If a tour times out or an assertion fails, the `tour_failure_dump.js` interceptor will automatically trigger. To protect LLM context windows, it mathematically condenses the raw HTML into an **Interactable DOM Skeleton**.
* Generic `<div class="col-md-6">` tags are aggressively pruned.
* Only the active interactive surface (buttons, inputs, links, modals, notifications, and elements with `name` or `o_tour_` classes) is dumped.
* The dump also prepends the active `window.location.hash` and a ledger of any pending/hung RPC network requests to aid in diagnosing backend blockages.

---

## 5. Orphaned Tour Class Audits
To prevent CSS bloat, the AST Burn List linter (`check_burn_list.py`) automatically extracts any class starting with `o_tour_` from backend XML views and cross-references it against all JavaScript tour files. If an `o_tour_` class is found in the DOM but never targeted by a test runner, the CI/CD pipeline will fail, mandating the removal of the dead code.

---

## 6. Technical Architecture & Security

### Core Architecture
The module implements three primary testing facilities:
1. **Real Transaction Testing (`RealTransactionCase`)**: Bypasses Odoo's standard `TestCursor` to provide a real, committable database connection. It uses ORM instrumentation ([@ANCHOR: orm_instrumentation]) and mathematical table snapshots ([@ANCHOR: leak_snapshotting]) to ensure database integrity.
2. **Integration Daemon Testing (`HamsTransactionCase`)**: Provides a lifecycle management wrapper for external Python daemons, including automated health polling. ([@ANCHOR: integration_daemon_testing]) Verified by [@ANCHOR: test_integration_daemon_testing]
3. **UI Tour Governance**: Defines standards for JavaScript-based UI tours and provides `TourUtils` for robust frontend testing.

It also includes a **Noisy Table Management** interface ([@ANCHOR: UX_NOISY_TABLE_MANAGEMENT]) to allow administrators to whitelist tables from leak detection.

### Security Design
- **Service Accounts**: Utilizes `user_real_transaction_service` ([@ANCHOR: user_real_transaction_service]) for background tasks and documentation injection.
- **Zero-Sudo Compliance**: Strictly avoids `.sudo()` by leveraging the `zero_sudo` security utilities and micro-privilege service accounts.
- **SQL Injection Prevention**: Uses `psycopg2.sql.Identifier` and `psycopg2.sql.Literal` for all dynamic SQL in the leak detector ([@ANCHOR: leak_verification]).

### Stories & Journeys
- **Real Transaction Testing Story**: Explains the need for real commits and how the facility handles them ([@ANCHOR: cursor_hijacking]).
- **Documentation Injection Story**: Describes the automated documentation setup process ([@ANCHOR: documentation_bootstrap]).
- **Developer Testing Flow Journey**: Guides developers through using `RealTransactionCase` for advanced integration tests.
- **Documentation Setup Flow Journey**: Details the technical steps of injecting documentation into the knowledge base.
