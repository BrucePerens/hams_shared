# Hams Test Infrastructure (`zero_sudo`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

This module is the unified testing infrastructure for the repository. It consolidates the Real Transaction Testing facility, the Daemon Integration Testing framework, and the UI Tour governance standards into a single, cohesive architecture.

---

## 1. Real Transaction Testing Facility

In standard Odoo testing, `TransactionCase` wraps the entire test execution inside an uncommitted PostgreSQL `SAVEPOINT`. While this makes tests extremely fast and prevents database pollution, it creates an artificial environment. This environment fundamentally breaks the ORM's local memory cache for inverse relational fields (like `One2many` relationships) and makes testing distributed workers impossible.

The `zero_sudo` module solves this by providing: **`RealTransactionCase`**.
**Because it inherits from Odoo's `HttpCase`, it natively supports full Werkzeug HTTP routing and testing utilities like `self.authenticate()` and `self.make_jsonrpc_request()`.**

### True Database Commits
You can safely call `self.env.cr.commit()` in your tests. This allows developers to write accurate tests for cross-worker cache invalidations (e.g., Redis pub/sub buses), background daemon polling, and lazy-loaded ORM relations.

Each test method gets its own database connection, signed in as the `admin` user (falling back to user id 2 if there is no `admin` login). `registry.cursor()` is pointed at new real connections for the duration of the test, so code that opens its own cursor (a daemon request handler, a background thread) sees committed data and its own commits are real. `authenticate()` commits after signing in, because the login-log row it writes would otherwise hold a lock on the user's row until the test ends. The class also writes `web.base.url` (frozen) into the system parameters when it starts.

### Automated ORM Tracking & Cleanup
Because real commits permanently write to the database, the facility wraps Odoo's `BaseModel.create()` for the length of each test. It records the ID of every record created through the ORM and, when the test ends, deletes them with `unlink()` (as user id 2), models in reverse order of first creation. It makes up to 5 passes to get past foreign-key ordering problems; on the fifth pass, a model whose records still cannot be unlinked is cleaned with a plain SQL `DELETE` by id. Records created with raw SQL are not tracked, so they are not cleaned up.

### SQL Leak Detection
To detect leftover data, the facility counts the rows of every table in the `public` PostgreSQL schema before the test begins. At the end of the test, after the cleanup above, it counts them again. If any table's row count differs (for example because of a raw SQL `INSERT` without manual cleanup), the test fails with an `AssertionError` naming the tables and the size of each difference.

Some tables change during almost any test (message and notification tables, the bus, cron bookkeeping, sessions, attachments, and so on). Those are excluded from the comparison: a built-in list in `real_transaction.py` plus the active rows of the `zero_sudo.noisy_table` model, which administrators can edit (see the Noisy Table Management interface below).

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

`HamsTransactionCase` (in `zero_sudo/tests/common.py`) is the ordinary test base class for the repository's modules. Unlike `RealTransactionCase` it is built on Odoo's `TransactionCase`, so each test runs inside a rolled-back transaction. It also provides `start_daemon()`, a wrapper to run an external Python daemon (e.g. the pager-duty spooler) during a test and send real requests to it.

* **Lifecycle Management:** A test calls `start_daemon(script_path, args, env_vars, health_url, timeout)`. The daemon is started in its own process group through `zero_sudo.daemon.utils` and remembered per test class. In `tearDownClass()` each daemon's process group gets `SIGTERM`, and if it has not exited after 2 seconds the group is sent `SIGKILL`.
* **Health Polling:** If a `health_url` is given, the method requests it every half second until it answers HTTP 200 or 204, raising `RuntimeError` if the daemon exits first and `TimeoutError` when `timeout` (default 600 seconds) runs out. This avoids sending requests before the daemon is listening.
* **Test setup it also does:** a fresh random encryption key is patched in for the secret-reading helpers of the test run, `HAMS_CRYPTO_KEY` is set if unset, the `web.base.url` system parameter is written, and each test begins by flushing the Redis cache database (`flushall`) if the Redis cache is reachable.

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
* Any `:contains(...)` pseudo-selector in a tour trigger (brittle translated strings; the burn-list linter flags it in tour files, and Odoo 19's own trigger matching does not support it).
* Arbitrary `data-*` attributes added to backend XML views for tours (they break backend XML schema validation). The existing `data-menu-xmlid` attribute on menu items is fine.

**Allowed Targeting Hierarchy:**
1. **Primary:** Native `name` attributes (e.g., `button[name="action_install"]`, `field[name="is_installed"]`).
2. **Secondary:** Structural IDs (`#wrap`, `.o_form_sheet`).
3. **Fallback:** Dedicated namespaced CSS classes starting with `o_tour_` (e.g., `.o_tour_create_site_btn`).

### Common Environmental Traps & Solutions (CRITICAL FOR AI AGENTS)
When developing tours in Odoo 19, you **MUST** navigate several strict environmental traps:

* **The Modal "Below a Modal" Trap:** Odoo's engine actively blocks clicks on background elements. A generic selector like `.btn-primary` will often match the background form instead of the active modal. You MUST scope your selectors to the active dialog (e.g., `.modal.show button[name='confirm_method']` or `.o_dialog button[name='...']`) and include a preliminary step to wait for the modal animation to finish rendering.
* **The Text Wrapping Trap:** Do not select by visible text with `:contains(...)`. Odoo 19 frequently wraps button text in nested `<span>` tags, and the burn-list linter rejects the pseudo-selector in tour triggers. Target explicit attributes such as `name`, `[data-menu-xmlid=...]` or an `o_tour_` class instead.
* **The Dropdown Trap:** Native `<select>` elements are deprecated in backend form views. Odoo 19 uses `.o_select_menu`. You MUST use two separate tour steps for dropdowns: one to click the dropdown container, and a subsequent step to click the `.o_select_menu_item`.
* **The Initialization Race Condition:** You MUST NEVER start a tour by having the first step manually execute `document.location.href = ...`. This causes severe race conditions. You MUST initialize the tour using the native `url: "/path"` property within the tour definition block.
* **The `expectUnloadPage` Trap:** You MUST NOT set `expectUnloadPage: true` on steps where navigation is conditional or when triggering an OWL soft-route (client action). If the page does not execute a hard browser reload, the Odoo test runner will fatally timeout after 20,000ms.
* **The Save Button Crash (Dirty Forms):** You MUST NEVER manually click the save button (`.o_form_button_save`) and immediately end the tour or navigate away. This leaves a dirty form view open, causing asynchronous network requests that corrupt subsequent tests. You MUST spread the `...TourUtils.safeSave()` macro into your step array: it clicks the save button and then waits for `.o_form_saved`, which the form shows only after the save request has finished and the record is clean.
* **Asset Registration Silent Failures:** Ensure your JavaScript tour files are actually loading. They MUST be explicitly declared in the module's `__manifest__.py` under the `assets` dictionary within the `web.assets_tests` key.

### Robustness Macros (`TourUtils`)
To combat Owl's (Odoo's JavaScript UI framework) asynchronous rendering delays and other race conditions, import `TourUtils` from `@zero_sudo/js/tour_utils`. It provides several safety macros you can spread into your tour steps. Among them:
* `TourUtils.waitForAbsence(selector, description)`: Pauses the tour until the specified element (e.g., a loading overlay or an old modal) is entirely removed from the DOM.
* `TourUtils.safeSave(saveTrigger, waitTrigger)`: Two steps: click the save button (default `.o_form_button_save`), then wait for the saved-and-clean form state (default `.o_form_saved`).
* Others cover dismissing the website cookie bar (`dismissCookiesBar`), replacing the browser's blocking `alert`/`confirm` dialogs (`bypassDialogs`), mocking external `fetch` requests (`mockExternalRequests`), and waiting for text or an element (`waitForText`, `waitForElement`).

---

## 4. Tour Failure Diagnostics (The Skeleton Dump)
The `tour_failure_dump.js` script is loaded into the test browser. It reports a fatal event, and condenses the page into an **Interactable DOM Skeleton** so the output is small enough for an LLM (large language model) to read. It fires on an uncaught window error, an unhandled promise rejection (some benign kinds are ignored), the browser going offline, a network error on an `XMLHttpRequest`, and when its watchdog finds the main thread unresponsive or the page growing without bound. A tour failure that Odoo reports itself (a message containing "failed:") switches the dump off, since the test is already over. Messages known to be harmless (such as Owl's development-mode notice and ResizeObserver loop notices) are downgraded to ordinary log lines.
* Plain layout elements such as `<div class="col-md-6">` are dropped, keeping only their text.
* Buttons, inputs, links, selects, text areas, modals, notifications, and elements with a `name`, an `id`, a `data-menu-xmlid` or an `o_tour_` class are kept, with a few attributes each; text longer than 80 characters is cut.
* The dump is preceded by the page's URL hash (or path) and the number of requests still pending, to help diagnose a backend that never answered.

---

## 5. Orphaned Tour Class Audits
To prevent CSS bloat, the AST (abstract syntax tree) Burn List linter (`check_burn_list.py`; the Burn List is the project's list of banned code patterns and deprecated APIs, enforced by custom linters, see the "Burn List" entry in `hams_open/README.md`) collects every class starting with `o_tour_` from the XML files and the JavaScript files and cross-references them in both directions. An `o_tour_` class in XML that no JavaScript mentions is reported as an orphaned tour class (dead code to remove); one in JavaScript that no XML contains is reported as a dangling tour target (the tour would time out). These are errors in the linter, which developers run locally; GitHub's CI does not run it.

---

## 6. Technical Architecture & Security

### Core Architecture
The module implements three primary testing facilities:
1. **Real Transaction Testing (`RealTransactionCase`)**: Bypasses Odoo's standard `TestCursor` to provide a real, committable database connection. It uses ORM instrumentation ([@ANCHOR: orm_instrumentation]) and row-count snapshots of every table ([@ANCHOR: leak_snapshotting]) to ensure database integrity.

2. **Integration Daemon Testing (`HamsTransactionCase`)**: The shared test base class; it provides `start_daemon()`, a lifecycle wrapper for external Python daemons with health polling. ([@ANCHOR: integration_daemon_testing]) Verified by [@ANCHOR: COMM_test_integration_daemon_testing]
3. **UI Tour Governance**: Defines standards for JavaScript-based UI tours and provides `TourUtils` for robust frontend testing.

It also includes a **Noisy Table Management** interface ([@ANCHOR: COMM_UX_NOISY_TABLE_MANAGEMENT]) to allow administrators to whitelist tables from leak detection.

### Security Design
- **Service Accounts**: The testing facility defines no service account of its own in this repository's data files; the anchor [@ANCHOR: user_real_transaction_service] is only a documentation marker. The documentation installation described in [zero_sudo.md](zero_sudo.md) is a separate part of `zero_sudo`.
- **Zero-Sudo Compliance**: The test classes do not call `.sudo()`; cleanup deletes as user id 2 (`with_user(2)`).
- **SQL Injection Prevention**: Uses `psycopg2.sql.Identifier` and `psycopg2.sql.Literal` for the dynamic SQL in the leak detector and the fallback cleanup ([@ANCHOR: leak_verification]).

### Stories & Journeys
- **Real Transaction Testing Story**: Explains the need for real commits and how the facility handles them ([@ANCHOR: cursor_hijacking]).

- **Documentation Injection Story**: Describes the automated documentation setup process ([@ANCHOR: documentation_bootstrap]).
- **Developer Testing Flow Journey**: Guides developers through using `RealTransactionCase` for advanced integration tests.
- **Documentation Setup Flow Journey**: Details the technical steps of injecting documentation into the knowledge base.
