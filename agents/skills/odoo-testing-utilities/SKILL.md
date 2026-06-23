---
name: odoo-testing-utilities
description: Provides guidance and architectural mandates on how to write tests using the custom zero_sudo test harnesses (HamsTransactionCase, HamsHttpCase, RealTransactionCase). Trigger when writing or debugging tests.
---

# Odoo Custom Testing Utilities

This project utilizes highly customized Odoo test harnesses located in `zero_sudo.tests.common` and `zero_sudo.tests.real_transaction` to deal with security hardening, background daemons, headless Chrome stability, and exact database cursor requirements. You MUST use these classes instead of standard Odoo test classes.

## 1. `HamsTransactionCase`
**Inherits from:** `odoo.tests.common.TransactionCase`
**Use for:** Standard backend tests, data manipulation, ORM logic, and testing background daemons.
**Key Features:**
- `start_daemon(script_path, args, env_vars, health_url, timeout)`: Safely spawns background processes, tracking them for automatic cleanup in `tearDownClass`.
- Automatically calls `wait_for_werkzeug_threads()` during teardown to prevent `concurrent delete` and serialization errors from daemon RPC requests that outlive the test block.
- **NEVER** use `self.env.cr.commit()` in this case unless you are managing your own savepoints.

## 2. `RealTransactionCase`
**Inherits from:** `odoo.tests.common.HttpCase`
**Use for:** Tests that require real, committed database transactions, such as testing PostgreSQL row-level locks, triggers, or external workers that read from the database from a separate connection.
**Key Features:**
- Physically hijacks the Odoo test cursor to disable isolated rollbacks.
- You **MUST** use `self.env.cr.commit()` to make data visible to external processes or HTTP controllers running outside the test cursor.
- Manually cleans up the database in `_real_teardown()` using a 5-attempt resilient retry loop to absorb cascading `SerializationFailure` errors. Do not rely on Odoo's native rollback.

## 3. `HamsHttpCase`
**Inherits from:** `odoo.tests.common.HttpCase`
**Use for:** UI Tests, JavaScript Tours, and Headless Chrome interactions.
**Key Features:**
- **Jules Watchdog Suppressions:** Automatically injects suppressions into the browser to prevent headless Chrome from crashing on unhandled promise rejections (like missing translations or fetch aborts).
- **Chrome Fallback Retries:** Intercepts `ChromeBrowser` initialization and automatically retries upon failure.
- `navigate_and_screenshot(url_path, prefix="screenshot_")`: A safe wrapper to navigate to a URL and capture a screenshot. **CRITICAL:** This method automatically multiplexes the browser instance (`self.start_hams_browser()`). Do not instantiate raw `ChromeBrowser` manually or you will trigger OOM and `NoSuchProcess` leaks.
- Avoid native `HttpCase.browser_js` if a Tour can be used. If writing a Tour, reference `odoo-ui-tours` skill.

## 4. `SafePatchMixin`
**Included in:** All `Hams*` cases.
**Use for:** Mocking attributes and methods securely.
**Key Features:**
- Provides `safe_patch` and `safe_patch_object`.
- Wraps mocked calls in a `DiagnosticMock` that enforces a recursion depth limit (default 5). This prevents infinite loops caused by aggressive recursion in test environments.

## Golden Rules
1. **Never use standard `TransactionCase` or `HttpCase` directly.** Always inherit from the `Hams*` equivalents.
2. **Handle serialization failures gracefully.** If you implement manual teardown loops, use `wait_for_werkzeug_threads(timeout=5.0)` to drain background requests before deleting records, and absorb or log `SerializationFailure` gracefully without breaking the test runner.
3. **No manual `ChromeBrowser` instantiation.** Rely on `self.start_hams_browser()`.
