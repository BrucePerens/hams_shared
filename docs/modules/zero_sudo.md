# Zero-Sudo Security Core [@ANCHOR: zero_sudo_main] (`zero_sudo`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

This module is the foundational security layer for our Odoo ecosystem. It enforces a strict **Zero-Sudo Architecture** (Architecture Decision Record ADR-0002) to prevent privilege escalation vulnerabilities and physically isolates background service accounts from interactive web sessions (ADR-0005). ADR-0002 and ADR-0005 are consolidated in [MASTER_01_SECURITY_ZERO_SUDO.md](../adrs/MASTER_01_SECURITY_ZERO_SUDO.md).

## 🛡️ Core Security Missions

1.  **Eliminate `.sudo()` Usage:** Developers are forbidden from using Odoo's native `.sudo()` command, which switches the environment into superuser mode (`env.su`) for the whole call, so access rules are no longer applied; "Zero-Sudo" means none of it. This module provides a secure, audited alternative via the **Service Account Pattern**.
2.  **Web Isolation:** Automated background tasks (Service Accounts) are strictly prohibited from logging into the website interface, preventing session hijacking or accidental human interference.
3.  **Mechanical Whitelisting:** Critical system parameters (like cryptographic keys) are blocked from being accessed or modified through the Zero-Sudo parameter helpers (`_get_system_param` / `_set_system_param`) unless they are explicitly registered in a secure code-level whitelist.
4.  **Security Audit Trail:** Blocked service-account logins, denied system-parameter reads and writes, and model cache invalidations are logged for administrator review.
5.  **High-Performance Security Interop:** Critical security lookups and atomic key-value (KV) operations are optimized via PostgreSQL procedures to minimize latency and ensure transaction-safe execution.

---

## 📖 User Guide (Non-Technical)

### What is a Service Account?
A "Service Account" is a special user profile used only by background programs, bots, and automated tasks. Because these accounts often have powerful permissions, they are **forbidden** from being used to log into the website through a browser.

### Managing Service Accounts:
1.  **Restricting an Account:** Go to **Settings > Users**, select a user, and check the **"Is Service Account"** box on the **Access Rights** tab. Only Settings administrators (`base.group_system`) can see or change this box.
2.  **Access Denied:** Once flagged, any login attempt with that account via the browser will result in an "Access Denied" error. This is a deliberate security feature.
3.  **Automatic Protection:** Every Service Account is automatically assigned an extremely long, randomized password that is impossible to guess, ensuring it can only be accessed by the automated system intended to use it. Turning the box on or off replaces the password with a new random one, and later password changes to a Service Account are ignored.

### Security Audit Logs:
Administrators can monitor security events in **Settings > Zero-Sudo Security > Security Audit Logs**. The following events are tracked:
*   **Service Account Web Login Attempt:** Logs when a background account signs in to the web interface with its correct password (the session is then ended).
*   **God-Mode Security Block Tripped:** A reserved event type for a background account trying to use global administrative privileges. Nothing in this repository currently records it; a blocked attempt is refused with an access error to the program that asked instead.
*   **Unauthorized System Parameter Access:** Logs when a process attempts to read a restricted system setting.
*   **Unauthorized System Parameter Write:** Logs when a process attempts to change a restricted system setting.
*   **Model Cache Invalidation:** Logs when a user or process manually clears the cached data for one kind of record (one model), not the whole system.

---

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for LLMs and Integrators developing custom downstream modules...
</system_role>

<architecture>
## Core Architecture

1.  **Service Account Pattern**: High-privilege operations are offloaded to dedicated `res.users` records flagged with `is_service_account=True`.
2.  **Centralized Security Utilities**: The `zero_sudo.security.utils` model provides cached methods for secure UID retrieval, system parameter whitelisting, and deterministic hashing.
3.  **Web Isolation Interceptor**: A controller override on `web_login` that uses raw SQL to check and block interactive logins for service accounts. The JSON login route (`/web/session/authenticate`) is overridden the same way, and an `ir.http._authenticate` check refuses every later request from a service-account session except the RPC (remote procedure call) endpoints `/jsonrpc` and `/xmlrpc/...`.
4.  **Automated Documentation Bootstrap**: An inheritance of `ir.module.module` that centrally manages the installation of HTML documentation from module manifests. On every registry load it installs or updates each installed module's `knowledge_docs` entries as `knowledge.article` records, skipping a document whose content hash is unchanged, and does nothing when the `knowledge.article` model is not installed. Documents are visible only to administrators unless their manifest entry sets `"public": True`.
</architecture>

<security_design>
## Security Design (ADR-0002, ADR-0005)

-   **Anti-IDOR (Insecure Direct Object Reference) & Privilege Escalation**: `_get_service_uid` performing direct SQL lookups (the PL/pgSQL functions in `data/postgres_procedures.xml`) strictly rejects any account that is disabled, is not flagged as a service account, or holds a global administrative group (`base.group_system`, `base.group_erp_manager`), directly or through implied groups. A rejection raises `AccessError`.
-   **Mechanical Secret Block**: `_get_system_param` and `_set_system_param` enforce hardcoded whitelists (`_get_param_read_whitelist()` and `_get_param_write_whitelist()`) to prevent SSTI (Server-Side Template Injection) exfiltration. Any key not on the list is denied with `AccessError` and logged. For a denied read, a key matching a cryptographic pattern (e.g., "secret", "token") only changes the error message; the pattern check does not apply to whitelisted keys, some of which are credentials.
</security_design>

---

<service_account_pattern>
## 1. The Service Account Pattern

You are strictly FORBIDDEN from using `.sudo()` inline. To escalate privileges:
1. Define your service account in your module's XML data and set `<field name="is_service_account" eval="True"/>`.
2. Retrieve its UID securely:
   `svc_uid = self.env['zero_sudo.security.utils']._get_service_uid('your_module.user_xml_id')`
3. Execute using the impersonation idiom:
   `self.env['target.model'].with_user(svc_uid).create(vals)`
</service_account_pattern>

---

<shared_service_accounts>
## 2. Centralized Shared Service Accounts

When a daemon strictly requires native ERP framework interactions that mandate `base.group_user`, they MUST temporarily assume one of the two centralized proxy accounts. These are the only two accounts this module declares with `base.group_user`; its other service accounts (`config_service_internal`, `gdpr_service_internal`, `user_lockout_service_internal`, and the never-acting `orphaned_record_owner`) hold only narrow groups or none:

### A. Central Mail Service Account (`zero_sudo.mail_service_internal`)
* **Use Case:** Execute `message_post()`, `send_mail()`, or interact with the `mail.thread` chatter.

### B. Odoo Facility Service Account (`zero_sudo.odoo_facility_service_internal`)
* **Use Case:** Complex ORM cascades that deeply assume internal user rights.
</shared_service_accounts>

---

<python_api>
## 3. Python API Reference (`zero_sudo.security.utils`)

#### `_get_service_uid(xml_id)` `[@ANCHOR: get_service_uid]`
Safely retrieves the database ID of a Service Account. Only the XML ID to user ID lookup is RAM-cached (preloaded at registry load). The safety check (account active, flagged as a service account, no administrative groups) runs live on every call, and a failure raises `AccessError`.
* **Arguments:** `xml_id` (str): The external ID (e.g., `'your_module.your_service_account'`).
* **Returns:** `int` (The User ID).

**Start-up preload** `[@ANCHOR: zero_sudo:register_hook_preload_service_uids]` `[@ANCHOR: zero_sudo:preload_service_uid_cache]`. The `_register_hook()` of `zero_sudo.security.utils` runs once for each registry build and calls `_preload_service_uid_cache()`. That method reads every `ir_model_data` row of model `res.users` whose user has `is_service_account` set and stores `(database name, xml_id) -> uid` in a process-wide dict. On the production configuration the registry is built in the master process before any worker forks, so every worker inherits the filled table. Only the resolution is cached, never the verdict. Entries are keyed by database so a multi-database deployment cannot mix them up, and a reload replaces only the entries of the database being loaded. A service account that did not exist at load time falls back to the full lookup and is deliberately not written back, because the dict is not transaction-scoped and a rolled-back test would otherwise leak into later calls.

**No default-env flush** `[@ANCHOR: zero_sudo:COMM_get_service_uid_no_transaction_flush]`. The live check in `_get_service_uid` runs its SELECT inside a savepoint opened with `flush=False`, after flushing through the caller's own environment. The default savepoint flushes the transaction's `default_env`, and a public request has none yet when a service account is resolved before authentication (the case on every anonymous page view), which made Odoo log "Missing default_env, flushing as public user" and look up the public user each time. Flushing the caller's environment still makes a pending ORM write to the service user visible to the check.

#### `_get_deterministic_hash(input_string)` `[@ANCHOR: deterministic_hash]`
Generates a deterministic 32-bit integer hash for `pg_advisory_xact_lock`.
* **Returns:** `int`.

#### `_get_system_param(key, default=None)` `[@ANCHOR: get_system_param]`
Safely retrieves a whitelisted system configuration parameter, reading it as `zero_sudo.config_service_internal`. A key not on the read whitelist is logged as `param_access_denied` and raises `AccessError`.

#### `_notify_cache_invalidation(model_name, key_value)` `[@ANCHOR: coherent_cache_signal]`
Emits a PostgreSQL `NOTIFY` event to synchronize distributed caches. It delegates to `distributed_redis_cache`'s `notify_model_invalidation()`, which signals on the `distributed_cache_invalidation` channel that the `distributed_redis_cache` cache-manager daemon listens on. Invalidation is for the whole model; `key_value` must be non-empty but does not narrow it. What this signal does and does not reach on other workers is spelled out in [docs/stories/cache_signaling.md](docs/stories/cache_signaling.md).

#### `_get_crypto_secret()` `[@ANCHOR: get_crypto_secret]`
Retrieves the root cryptographic key from environment or local file, bypassing DB. It checks the `HAMS_CRYPTO_KEY` environment variable, then `/var/lib/odoo/hams_crypto.secret`, then Odoo's `admin_passwd` setting. If none is set, or the value is `admin`, it logs an error and returns an empty string, so callers must refuse to encrypt or sign.

#### `_invalidate_model_cache(model_name)` `[@ANCHOR: invalidate_model_cache]`
Securely invalidates the entire cache for a specific model. Requires the user to have write access to the model (members of `base.group_system` skip this check). It clears that model's entries in Redis and in this worker's local cache, notifies the other workers through `_notify_cache_invalidation`, and records a `cache_invalidation` security log entry.

#### `_set_kv(key, value)` `[@ANCHOR: set_kv_procedure]`
High-performance atomic key-value update using an optimized Postgres procedure (`zero_sudo_set_kv`, a single `INSERT ... ON CONFLICT (key) DO UPDATE` upsert).
</python_api>

---

<tour_utils>
## 4. UI Tour Macros (`tour_utils.js`)

The `TourUtils` object (`@zero_sudo/js/tour_utils`) provides centralized macros for Odoo UI Tours to guarantee architectural compliance and eliminate race conditions in headless browser testing.

#### `safeSave(saveButtonTrigger, waitTrigger)`
Generates a step array that clicks a save button and then waits (a native tour step, no polling loop) for `.o_form_saved`, the form renderer's own "persisted and not dirty" class, so the tour cannot end or navigate while the save RPC is still in flight. Pass `waitTrigger` to wait on something else instead. (Do not wait on `.o_form_button_create`: the control panel's always-present New button matches before the save resolves.)

#### `bypassDialogs()`
Generates a macro to intercept and bypass native blocking dialogs (`window.alert`, `window.confirm`) during testing. Intercepted dialogs are logged as warnings, and `confirm` always answers yes.

#### `mockExternalRequests(urlPattern, mockResponse)`
Generates a macro to intercept and mock `fetch` requests matching a specific URL pattern, preventing external network calls during tests. The pattern is a plain substring (not a regular expression); a matching request gets `mockResponse` as a JSON body with status 200.

#### `waitForAbsence(selector, description)`
Generates a macro that polls and explicitly waits for a specific DOM element to disappear from the page. It checks every 250 ms and fails the step after 10 seconds.

#### `deterministicInput(helpers, text)`
A direct execution function (used within a tour's `run` step) that safely injects text into the currently active input element and explicitly dispatches the `input`, `change`, and `keyup` events required to reliably awaken Odoo's frontend framework (e.g., Many2one debouncers).
</tour_utils>

## 5. Additional Models and Utilities

### Inline SVG allowlist sanitizer (`svg_sanitizer.py`) `[@ANCHOR: zero_sudo:svg_allowlist_sanitizer]`
Odoo's HTML sanitizer strips every SVG shape, so inline schematics render as empty boxes. At import
time this module patches `odoo.tools.mail._Cleaner.__call__` (installed from `__init__.py`,
`[@ANCHOR: zero_sudo:svg_allowlist_install]`) so every `fields.Html` write, and every module that
imported `html_sanitize` by name, rebuilds each `<svg>` from a strict element/attribute/value
allowlist and leaves the rest of the document to the stock sanitizer. Design, threat model,
residual risks and the vector list: `docs/stories/svg_allowlist_sanitizer.md`. Tests:
`tests/test_svg_sanitizer.py`, shared corpus `tests/svg_corpus.py`.

### `zero_sudo.daemon.utils`
Provides centralized, private utilities for managing background daemon processes tightly coupled with Odoo.
* `_start_daemon_process(script_path, args, env_vars)` `[@ANCHOR: zero_sudo:_start_daemon_process]`: Starts `script_path` under the current Python interpreter (`sys.executable`, falling back to `/usr/bin/python3`) with `shell=False`, in its own session so it is detached from the web request and so `_stop_daemon_process` can signal its whole process group. The child's `PYTHONPATH` is the parent's `sys.path` followed by any `PYTHONPATH` already in the environment, which lets a daemon import Odoo addons; `env_vars` are then merged over that environment. It returns the `Popen` object.
* `_stop_daemon_process(process)`: Safely terminates a running daemon.
* `_poll_health_check(url, timeout, interval)` `[@ANCHOR: zero_sudo:_poll_health_check]`: Polls an HTTP endpoint to verify daemon health before proceeding with tests. It sends `HEAD` requests until one returns 200, accepts only `http` and `https` URLs, and raises `UserError` after `timeout` seconds (default 30).

### Shared offline queue (`static/src/js/offline_store.js`)
`OfflineStore` is a small IndexedDB-backed queue that frontend modules (the QSO logger in `ham_shack`, `ics_forms`) use to keep a record locally while the browser is offline and send it later. It keeps records in an object store keyed by `uuid` with an index on `timestamp`; the database and store names are constructor arguments whose defaults match the names `ham_shack` first used, so existing browser data survived the move into this module. Its methods are `init()`, `saveLog(logData)`, `getPendingLogs()` and `deleteLog(uuid)`.

`isStorageFullError(err)` `[@ANCHOR: is_storage_full_error]` tells a caller whether a rejected `saveLog()` means the browser refused the write because the disk or the site's storage quota is full. It returns true for a `QuotaExceededError` (code 22) as Chrome and the spec raise it, and for Firefox's `NS_ERROR_DOM_QUOTA_REACHED` (code 1014), and false for anything else, including no error at all. Callers use it to tell the operator "Disk Full" instead of a generic or misleading network error.

### `zero_sudo.kv`
A high-performance Key-Value store table with cached reads (`_get_kv()` is `@distributed_cache`-decorated) and atomic writes via PostgreSQL procedures. In this repository it is used by the documentation bootstrap to record each installed document's content hash and article ID.
* Developers interact with this model via `_get_kv()` and `_set_kv()` in `zero_sudo.security.utils`.

### `zero_sudo.noisy_table`
A registry model (`zero_sudo_noisy_table`) used to declare PostgreSQL tables (by table name) that undergo high-frequency or technically necessary data mutations (e.g. `ir_logging`, `bus_bus`). Tables registered here are exempt from the strict end-of-test Database Leak Prevention checks of `RealTransactionCase` (`tests/real_transaction.py`), which also always skips its own built-in list of such tables.

### `zero_sudo.security.log`
The centralized audit log model (`zero_sudo_security_log`). Records blocked service-account logins, denied system-parameter reads and writes, and cache invalidation commands. 
* Rows are retained forever: no scheduled job purges, expires or truncates this log.
* Immutable design ensures even `base.group_system` users cannot delete or modify log records via the UI or ORM.

---

<stories_and_journeys>
## 6. Architectural Stories & Journeys

For detailed narratives and end-to-end workflows, refer to the following:

### Stories
* **Secure Privilege Escalation** `[@ANCHOR: COMM_story_secure_escalation]`: Narrative on how developers securely escalate privileges using service accounts instead of `.sudo()`. [Read Story](zero_sudo/hams_shared/docs/stories/secure_escalation.md)

* **Blocking Service Account Login** `[@ANCHOR: COMM_story_login_blocking]`: How the system prevents service accounts from accessing the interactive web interface. [Read Story](zero_sudo/hams_shared/docs/stories/login_blocking.md)

* **Parameter Whitelisting** `[@ANCHOR: COMM_story_parameter_whitelisting]`: Protection of sensitive system parameters from unauthorized access. [Read Story](zero_sudo/hams_shared/docs/stories/parameter_whitelisting.md)

* **Multi-Website Awareness** `[@ANCHOR: story_multi_website]`: How the security core behaves in multi-website environments. [Read Story](zero_sudo/hams_shared/docs/stories/multi_website.md)

* **Coherent Cache Signaling** `[@ANCHOR: COMM_story_cache_signaling]`: Ensuring cache consistency across multiple Odoo workers using Postgres NOTIFY. [Read Story](zero_sudo/hams_shared/docs/stories/cache_signaling.md)

* **Deterministic Hashing** `[@ANCHOR: COMM_story_deterministic_hash]`: Generation of stable integer hashes for PostgreSQL advisory locks. [Read Story](zero_sudo/hams_shared/docs/stories/deterministic_hashing.md)

* **Centralized Documentation Bootstrap** `[@ANCHOR: story_zero_sudo_doc_installer]`: How documentation is centrally installed across the platform. [Read Story](zero_sudo/hams_shared/docs/stories/documentation_bootstrap.md)

### Journeys
* **Service Account Lifecycle** `[@ANCHOR: COMM_journey_service_account_lifecycle]`: The end-to-end flow of a service account from provisioning to secure execution. [Read Journey](zero_sudo/hams_shared/docs/journeys/service_account_lifecycle.md)

* **Securing Configuration Parameters** `[@ANCHOR: COMM_journey_securing_configuration]`: The workflow for safely integrating and accessing new configuration parameters. [Read Journey](zero_sudo/hams_shared/docs/journeys/securing_configuration.md)

* **Developer Integration** `[@ANCHOR: journey_developer_integration]`: End-to-end workflow for developers to integrate with the Zero-Sudo ecosystem. [Read Journey](zero_sudo/hams_shared/docs/journeys/developer_integration.md)
</stories_and_journeys>

## 7. Function-Level Anchors

The `COMM_`-prefixed anchors below are the ones the module's stories and journeys cite; each marks the function described.

* **`_get_service_uid`** `[@ANCHOR: COMM_get_service_uid]`: resolves the account's XML ID to a user id with raw SQL `[@ANCHOR: COMM_get_service_uid_sql_resolve]`, then verifies in SQL that the account is active, flagged as a service account and holds no global administrative group `[@ANCHOR: COMM_get_service_uid_sql_verify]`. Verified by `[@ANCHOR: COMM_test_get_service_uid]`. See section 3 for what is cached.
* **`_get_system_param`** `[@ANCHOR: COMM_get_system_param]`, **`_get_deterministic_hash`** `[@ANCHOR: COMM_deterministic_hash]`, **`_get_crypto_secret`** `[@ANCHOR: COMM_get_crypto_secret]`: described in section 3.
* **`_notify_cache_invalidation`** `[@ANCHOR: COMM_coherent_cache_signal]`, tested by `[@ANCHOR: COMM_test_coherent_cache_signal]`: delegates to `distributed_redis_cache`'s `notify_model_invalidation()` (section 3).
* **`_set_kv`** `[@ANCHOR: COMM_set_kv_sql_check]`: the atomic key-value upsert (section 5).
* **`is_service_account`** `[@ANCHOR: COMM_is_service_account_field]`: the boolean field on `res.users` that marks a service account.
* **`web_login`** `[@ANCHOR: COMM_web_login_interceptor]`: the override in the `Home` controller that refuses interactive logins for service accounts, using a direct SQL check `[@ANCHOR: COMM_web_login_interceptor_check]`.

## 8. External Dependencies
* `python`: `["markdown", "psycopg2", "requests"]`
