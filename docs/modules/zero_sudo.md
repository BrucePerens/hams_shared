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

**Start-up preload** `register_hook_preload_service_uids` `preload_service_uid_cache`. The `_register_hook()` of `zero_sudo.security.utils` runs once for each registry build and calls `_preload_service_uid_cache()`. That method reads every `ir_model_data` row of model `res.users` whose user has `is_service_account` set and stores `(database name, xml_id) -> uid` in a process-wide dict. On the production configuration the registry is built in the master process before any worker forks, so every worker inherits the filled table. Only the resolution is cached, never the verdict. Entries are keyed by database so a multi-database deployment cannot mix them up, and a reload replaces only the entries of the database being loaded. A service account that did not exist at load time falls back to the full lookup and is deliberately not written back, because the dict is not transaction-scoped and a rolled-back test would otherwise leak into later calls.

**No default-env flush** `COMM_get_service_uid_no_transaction_flush`. The live check in `_get_service_uid` runs its SELECT inside a savepoint opened with `flush=False`, after flushing through the caller's own environment. The default savepoint flushes the transaction's `default_env`, and a public request has none yet when a service account is resolved before authentication (the case on every anonymous page view), which made Odoo log "Missing default_env, flushing as public user" and look up the public user each time. Flushing the caller's environment still makes a pending ORM write to the service user visible to the check.

#### `_get_deterministic_hash(input_string)` `[@ANCHOR: deterministic_hash]`
Generates a deterministic 32-bit integer hash for `pg_advisory_xact_lock`.
* **Returns:** `int`.

**Purpose.** PostgreSQL advisory locks need an integer key, while callers name resources by string; this turns the string into a stable integer so every worker locking the same name takes the same lock.

**Argument.** `input_string`: any value; a non-string is converted with `str()`.

**Returns.** An `int` between 0 and 2147482646: the first eight hex digits of the SHA-256 of the UTF-8 text, modulo 2147483647. It raises nothing, is pure and is stable across processes and restarts.

**Special concerns.** Not a security hash: two names can collide, which only makes unrelated resources share a lock. Test: `COMM_test_deterministic_hash`.

#### `_get_system_param(key, default=None)` `[@ANCHOR: get_system_param]`
Safely retrieves a whitelisted system configuration parameter, reading it as `zero_sudo.config_service_internal`. A key not on the read whitelist is logged as `param_access_denied` and raises `AccessError`.

**Arguments.** `key`: the `ir.config_parameter` name. `default`: returned when the parameter is unset. The method is an `@api.model` on `zero_sudo.security.utils`.

**Returns.** The stored string, or `default`.

**Errors.** `AccessError` when the key is not on the read whitelist (`_get_param_read_whitelist`). A key containing `secret`, `key`, `password`, `token`, `auth`, `crypt` or `cert` gets the message that it matches restricted cryptographic patterns, any other the message to register it in `zero_sudo/models/security_utils.py`.

**Special concerns.** An allowed read runs as `zero_sudo.config_service_internal`, not with `.sudo()`. A denied read writes a `zero_sudo.security.log` row as the facility account. Cryptographic secrets come from `_get_crypto_secret`, never from here. Test: `COMM_test_01_mechanical_secret_block_enforcement`.

#### `_notify_cache_invalidation(model_name, key_value)` `[@ANCHOR: coherent_cache_signal]`
Emits a PostgreSQL `NOTIFY` event to synchronize distributed caches. It delegates to `distributed_redis_cache`'s `notify_model_invalidation()`, which signals on the `distributed_cache_invalidation` channel that the `distributed_redis_cache` cache-manager daemon listens on. Invalidation is for the whole model; `key_value` must be non-empty but does not narrow it. What this signal does and does not reach on other workers is spelled out in [../../../zero_sudo/docs/stories/cache_signaling.md](../../../zero_sudo/docs/stories/cache_signaling.md).

#### `_get_crypto_secret()` `[@ANCHOR: get_crypto_secret]`
Retrieves the root cryptographic key from environment or local file, bypassing DB. It checks the `HAMS_CRYPTO_KEY` environment variable, then `/var/lib/odoo/hams_crypto.secret`, then Odoo's `admin_passwd` setting. If none is set, or the value is `admin`, it logs an error and returns an empty string, so callers must refuse to encrypt or sign.

#### `_invalidate_model_cache(model_name)` `[@ANCHOR: invalidate_model_cache]`
Securely invalidates the entire cache for a specific model. Requires the user to have write access to the model (members of `base.group_system` skip this check). It clears that model's entries in Redis and in this worker's local cache, notifies the other workers through `_notify_cache_invalidation`, and records a `cache_invalidation` security log entry.

#### `_set_kv(key, value)` `[@ANCHOR: set_kv_procedure]`
High-performance atomic key-value update using an optimized Postgres procedure (`zero_sudo_set_kv`, a single `INSERT ... ON CONFLICT (key) DO UPDATE` upsert).

**Arguments.** `key` and `value`: strings, passed as bound parameters (`SELECT zero_sudo_set_kv(%s, %s)`), never concatenated into the SQL.

**Returns.** Nothing. After the call the method runs `invalidate_model()` on `zero_sudo.kv`, because direct SQL bypasses the ORM cache.

**Special concerns.** One round trip, atomic under concurrent writers, last write wins; no Python-side existence check. Read back with `_get_kv`, which returns `None` for a key never set. Tests: `COMM_test_set_kv_procedure`, `COMM_test_set_kv_sql_check`.
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
`svg_allowlist_install`) so every `fields.Html` write, and every module that
imported `html_sanitize` by name, rebuilds each `<svg>` from a strict element/attribute/value
allowlist and leaves the rest of the document to the stock sanitizer. Design, threat model,
residual risks and the vector list: `docs/stories/svg_allowlist_sanitizer.md`. Tests:
`tests/test_svg_sanitizer.py`, shared corpus `tests/svg_corpus.py`.

### `zero_sudo.daemon.utils`
Provides centralized, private utilities for managing background daemon processes tightly coupled with Odoo.
* `_start_daemon_process(script_path, args, env_vars)` `_start_daemon_process`: Starts `script_path` under the current Python interpreter (`sys.executable`, falling back to `/usr/bin/python3`) with `shell=False`, in its own session so it is detached from the web request and so `_stop_daemon_process` can signal its whole process group. The child's `PYTHONPATH` is the parent's `sys.path` followed by any `PYTHONPATH` already in the environment, which lets a daemon import Odoo addons; `env_vars` are then merged over that environment. It returns the `Popen` object.
* `_stop_daemon_process(process)`: Safely terminates a running daemon.
* `_poll_health_check(url, timeout, interval)` `_poll_health_check`: Polls an HTTP endpoint to verify daemon health before proceeding with tests. It sends `HEAD` requests until one returns 200, accepts only `http` and `https` URLs, and raises `UserError` after `timeout` seconds (default 30).

### Shared offline queue (`static/src/js/offline_store.js`)
`OfflineStore` is a small IndexedDB-backed queue that frontend modules (the QSO (radio contact) logger in `ham_shack`, `ics_forms`) use to keep a record locally while the browser is offline and send it later. It keeps records in an object store keyed by `uuid` with an index on `timestamp`; the database and store names are constructor arguments whose defaults match the names `ham_shack` first used, so existing browser data survived the move into this module. Its methods are `init()`, `saveLog(logData)`, `getPendingLogs()` and `deleteLog(uuid)`.

`isStorageFullError(err)` `is_storage_full_error` tells a caller whether a rejected `saveLog()` means the browser refused the write because the disk or the site's storage quota is full. It returns true for a `QuotaExceededError` (code 22) as Chrome and the spec raise it, and for Firefox's `NS_ERROR_DOM_QUOTA_REACHED` (code 1014), and false for anything else, including no error at all. Callers use it to tell the operator "Disk Full" instead of a generic or misleading network error.

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
* **Secure Privilege Escalation** `COMM_story_secure_escalation`: Narrative on how developers securely escalate privileges using service accounts instead of `.sudo()`. [Read Story](../../../zero_sudo/docs/stories/secure_escalation.md)

* **Blocking Service Account Login** `COMM_story_login_blocking`: How the system prevents service accounts from accessing the interactive web interface. [Read Story](../../../zero_sudo/docs/stories/login_blocking.md)

* **Parameter Whitelisting** `COMM_story_parameter_whitelisting`: Protection of sensitive system parameters from unauthorized access. [Read Story](../../../zero_sudo/docs/stories/parameter_whitelisting.md)

* **Multi-Website Awareness** `story_multi_website`: How the security core behaves in multi-website environments. [Read Story](../../../zero_sudo/docs/stories/multi_website.md)

* **Coherent Cache Signaling** `COMM_story_cache_signaling`: Ensuring cache consistency across multiple Odoo workers using Postgres NOTIFY. [Read Story](../../../zero_sudo/docs/stories/cache_signaling.md)

* **Deterministic Hashing** `COMM_story_deterministic_hash`: Generation of stable integer hashes for PostgreSQL advisory locks. [Read Story](../../../zero_sudo/docs/stories/deterministic_hashing.md)

* **Centralized Documentation Bootstrap** `story_zero_sudo_doc_installer`: How documentation is centrally installed across the platform. [Read Story](../../../zero_sudo/docs/stories/documentation_bootstrap.md)

### Journeys
* **Service Account Lifecycle** `COMM_journey_service_account_lifecycle`: The end-to-end flow of a service account from provisioning to secure execution. [Read Journey](../../../zero_sudo/docs/journeys/service_account_lifecycle.md)

* **Securing Configuration Parameters** `COMM_journey_securing_configuration`: The workflow for safely integrating and accessing new configuration parameters. [Read Journey](../../../zero_sudo/docs/journeys/securing_configuration.md)

* **Developer Integration** `journey_developer_integration`: End-to-end workflow for developers to integrate with the Zero-Sudo ecosystem. [Read Journey](../../../zero_sudo/docs/journeys/developer_integration.md)
</stories_and_journeys>

## 7. Function-Level Anchors

The `COMM_`-prefixed anchors below are the ones the module's stories and journeys cite; each marks the function described.

* **`_get_service_uid`** `COMM_get_service_uid`: resolves the account's XML ID to a user id with raw SQL `COMM_get_service_uid_sql_resolve`, then verifies in SQL that the account is active, flagged as a service account and holds no global administrative group `COMM_get_service_uid_sql_verify`. Verified by `COMM_test_get_service_uid`. See section 3 for what is cached.
* **`_get_system_param`** `COMM_get_system_param`, **`_get_deterministic_hash`** `COMM_deterministic_hash`, **`_get_crypto_secret`** `COMM_get_crypto_secret`: described in section 3.
* **`_notify_cache_invalidation`** `COMM_coherent_cache_signal`, tested by `COMM_test_coherent_cache_signal`: delegates to `distributed_redis_cache`'s `notify_model_invalidation()` (section 3).
* **`_set_kv`** `COMM_set_kv_sql_check`: the atomic key-value upsert (section 5).
* **`is_service_account`** `COMM_is_service_account_field`: the boolean field on `res.users` that marks a service account.
* **`web_login`** `COMM_web_login_interceptor`: the override in the `Home` controller that refuses interactive logins for service accounts, using a direct SQL check `COMM_web_login_interceptor_check`.

## 8. External Dependencies
* `python`: `["markdown", "psycopg2", "requests"]`

## Anchor reference

### `register_hook_preload_service_uids` [@ANCHOR: zero_sudo:register_hook_preload_service_uids]

**Purpose.** The Odoo `_register_hook` override on `zero_sudo.security.utils` that fills the service-account uid cache once when the registry is built, so the first request after start-up does not pay the lookup.

**Interface.** `_register_hook(self)`; no arguments, returns nothing. It calls the parent hook, then `_preload_service_uid_cache()` (`zero_sudo:preload_service_uid_cache`).

**Special concerns.** Odoo runs it once per registry build, in the master process before workers fork, so every worker inherits the filled dictionary by copy-on-write. It only populates the resolution cache; the live safety check still runs on every `_get_service_uid` call.

**See also.** Test in `zero_sudo/tests/test_security_utils.py`.

### `preload_service_uid_cache` [@ANCHOR: zero_sudo:preload_service_uid_cache]

**Purpose.** `_preload_service_uid_cache()` loads the map from service-account XML id to user id into a process-wide dictionary at start-up, so `_get_service_uid` can skip the `ir_model_data` lookup.

**Interface.** No arguments (`@api.model`); returns nothing. It runs `SELECT d.module || '.' || d.name, d.res_id FROM ir_model_data d JOIN res_users u ON u.id = d.res_id WHERE d.model = 'res.users' AND u.is_service_account = true`, then replaces this database's entries in `_SERVICE_UID_CACHE` (keys are `(dbname, xml_id)`) and logs the count.

**Special concerns.** It caches only the resolution, which is fixed once a `noupdate="1"` data record installs, and never the safety verdict (active, flagged, no privilege escalation), which an administrator can change at any time. The cache is a plain dictionary, never evicted, and a lookup miss does not write back. Entries for other databases are kept.

**See also.** `COMM_get_service_uid`, `register_hook_preload_service_uids`.

### `COMM_get_service_uid_no_transaction_flush` [@ANCHOR: zero_sudo:COMM_get_service_uid_no_transaction_flush]

**Purpose.** Marks the part of `_get_service_uid` that flushes pending ORM writes through the caller's own environment and opens the savepoint without Odoo's default flush.

**Why.** The default `savepoint()` flushes the transaction's `default_env`. A public request has none yet when the tenant-site routing resolves its service account, before authentication gives the request a user, so Odoo logged "Missing default_env, flushing as public user" and looked up `base.public_user` on every public request. The procedures only SELECT; the one thing the flush buys is that a pending write to the service user (active flag, groups) is visible, and `self.env.flush_all()` does that. The code then uses `self.env.cr.savepoint(flush=False)`.

**Special concerns.** Side effect only on the transaction: pending writes are sent to the database. No arguments of its own.

**See also.** Test `COMM_test_get_service_uid_no_transaction_flush` in `zero_sudo/tests/test_service_uid_no_default_env.py`.

### `svg_allowlist_install` [@ANCHOR: zero_sudo:svg_allowlist_install]

**Purpose.** `install()` in `zero_sudo/svg_sanitizer.py` makes Odoo's HTML sanitizer SVG-aware, so schematics written as inline SVG survive a write to a `fields.Html` field while the rest of the document is sanitized exactly as before.

**Interface.** No arguments; returns nothing. It replaces `odoo.tools.mail._Cleaner.__call__` with a wrapper (`call_with_svg_allowlist`) that calls `clean_document`, which rebuilds each `<svg>` subtree from an allowlist and hands the remainder to the original method. `style` and `class` are passed through as "dropped" when the cleaner options say so.

**Special concerns.** Idempotent: the wrapper carries the marker `_hams_svg_allowlist`, and a second call returns without wrapping again; the original is kept as `__wrapped__`. It patches the class rather than `html_sanitize`, so modules that imported the function by name are covered. Process-wide, run once at import.

**Security.** Script, `foreignObject`, `use`, animation and external references stay out; only the allowlist is kept. See `zero_sudo:svg_allowlist_sanitizer`.

### `_start_daemon_process` [@ANCHOR: zero_sudo:_start_daemon_process]

**Purpose.** `_start_daemon_process(script_path, args=None, env_vars=None)` in `zero_sudo/models/daemon_utils.py` starts a Python daemon script as a subprocess for integration tests.

**Arguments.** `script_path`: path of the script, run with the current interpreter (`sys.executable`, else `/usr/bin/python3`). `args`: extra command-line arguments (list). `env_vars`: extra environment (dict), merged over a copy of the current environment.

**Returns.** The `subprocess.Popen` handle; the caller must stop it.

**Special concerns.** The child's `PYTHONPATH` gets the parent's `sys.path` prepended so the daemon sees the same modules. It starts in a new session and uses no shell. The log line records the program name and argument count only, because an argument can be a secret and the log outlives the process.

**See also.** Test `zero_sudo/tests/test_tdd_fixes.py`; `_poll_health_check`.

### `_poll_health_check` [@ANCHOR: zero_sudo:_poll_health_check]

**Purpose.** `_poll_health_check(url, timeout=30, interval=1)` waits until a daemon's health URL answers, so a test does not race a daemon that is still starting.

**Arguments.** `url`: an `http` or `https` URL, HEAD-requested; `timeout`: seconds to keep trying (default 30); `interval`: seconds between tries (default 1).

**Returns.** True when the URL answers 200 within the time; false when the timeout elapses.

**Errors.** `UserError` ("Health check URL must use http or https") for any other scheme, a defence against `file://` reads or internal requests; the method is private so it cannot be reached through RPC.

**Special concerns.** Blocks the calling thread with sleeps. Only used by tests against a local daemon.

**See also.** `_start_daemon_process`; test `zero_sudo/tests/test_tdd_fixes.py`.

### `is_storage_full_error` [@ANCHOR: is_storage_full_error]

**Purpose.** `isStorageFullError(err)` in `zero_sudo/static/src/js/offline_store.js` recognises the browser error for "storage full", so the offline logger can tell the operator "Disk Full" and not a generic or misleading error, and a full disk does not silently lose offline contacts.

**Argument.** `err`: any thrown value or DOMException; `null` or `undefined` is accepted.

**Returns.** `true` for a `QuotaExceededError` (Chrome and the specification, legacy code 22) or `NS_ERROR_DOM_QUOTA_REACHED` (Firefox, code 1014), by name or code; otherwise `false`. It never throws.

**Special concerns.** Pure and synchronous; used by `OfflineStore` write paths.

**See also.** Test `zero_sudo/static/tests/offline_store.test.js`.

### `COMM_story_secure_escalation` [@ANCHOR: COMM_story_secure_escalation]

**Purpose.** The story document `zero_sudo/docs/stories/secure_escalation.md`: how a developer gets extra privilege without `.sudo()`.

**Flow.** The developer finds a need for elevated rights (creating a record the current user may not), calls `_get_service_uid` (`COMM_get_service_uid`) for a predefined service account and runs the operation with `.with_user(svc_uid)`. The function guarantees the account exists (resolved by raw SQL, `COMM_get_service_uid_sql_resolve`) and is active and flagged as a service account (`COMM_get_service_uid_sql_verify`), so a deactivated account cannot be revived for background work.

**Special concerns.** Documentation only; it is the reason the platform linter forbids `.sudo()`.

**See also.** `COMM_test_get_service_uid`.

### `COMM_story_login_blocking` [@ANCHOR: COMM_story_login_blocking]

**Purpose.** The story document `zero_sudo/docs/stories/login_blocking.md`: why and how service accounts are barred from interactive login. It is the narrative anchor that ties the flag, the random password, the interceptor, the SQL check and the audit log together.

**Flow.** An administrator or data file sets `is_service_account` (`COMM_is_service_account_field`); creation assigns a 128-byte random password; a login attempt succeeds at the credential step; `web_login` (`COMM_web_login_interceptor`) runs the direct SQL check (`COMM_web_login_interceptor_check`), destroys the session, redirects to the login page with an error and writes `zero_sudo.security.log`.

**Special concerns.** The story is documentation only; it carries no code and no arguments.

**See also.** Tests `COMM_test_web_login_interceptor` and `COMM_test_is_service_account_field`.

### `COMM_story_parameter_whitelisting` [@ANCHOR: COMM_story_parameter_whitelisting]

**Purpose.** The story document `zero_sudo/docs/stories/parameter_whitelisting.md`: how system parameters are protected from exfiltration, in particular by server-side template injection.

**Flow.** A module calls `_get_system_param` (`COMM_get_system_param`) or `_set_system_param` (`COMM_set_system_param`); the key is checked against the read or write whitelist; keys that look cryptographic are refused outright; an allowed key is fetched with the micro-privilege account `zero_sudo.config_service_internal`. A developer adds a safe key by extending the whitelist methods. Cryptographic secrets never enter the whitelist: they come from `_get_crypto_secret` (`COMM_get_crypto_secret`).

**Special concerns.** Documentation only.

**See also.** `COMM_journey_securing_configuration`.

### `story_multi_website` [@ANCHOR: story_multi_website]

**Purpose.** The story document `zero_sudo/docs/stories/multi_website.md`, which explains how the `zero_sudo` core behaves with several websites on one Odoo.

**Content.** Three points. The service-account login check (`COMM_web_login_interceptor_check`) is global: a flagged account is logged out whatever site it signed in through. The documentation installer (`COMM_zero_sudo_doc_installer`) is website-agnostic and writes to the one central knowledge base. System parameters (`COMM_get_system_param`) are global `ir.config_parameter` rows guarded by the whitelist.

**Special concerns.** Documentation only; no code and no arguments.

### `COMM_story_cache_signaling` [@ANCHOR: COMM_story_cache_signaling]

**Purpose.** The story document `zero_sudo/docs/stories/cache_signaling.md`: how cache coherence is kept across workers.

**Flow.** A record changes; the code calls `_notify_cache_invalidation` (`COMM_coherent_cache_signal`) with the model name and a key; the function issues a PostgreSQL `NOTIFY` on `distributed_cache_invalidation` with a JSON payload; the cache-manager daemon listens and republishes to Redis; `_invalidate_model_cache` (`COMM_invalidate_model_cache`) is the checked, logged public form of the same action.

**Special concerns.** Documentation only. Invalidation is for the whole model whatever key is passed.

**See also.** Tests `COMM_test_coherent_cache_signal`, `COMM_test_invalidate_model_cache`.

### `COMM_story_deterministic_hash` [@ANCHOR: COMM_story_deterministic_hash]

**Purpose.** The story document `zero_sudo/docs/stories/deterministic_hashing.md`: how a developer obtains an integer lock key from a resource name.

**Flow.** A developer has a string naming a resource; `_get_deterministic_hash` (`COMM_deterministic_hash`) returns a 32-bit integer from its SHA-256; the same string always gives the same integer, so it can go straight into an advisory lock and prevent two background tasks working on one resource at once.

**Special concerns.** Documentation only. The hash is stable but collisions are possible, which is acceptable for locking.

**See also.** Test `COMM_test_deterministic_hash`.

### `story_zero_sudo_doc_installer` [@ANCHOR: story_zero_sudo_doc_installer]

**Purpose.** The story "Centralized Documentation Bootstrap" (`zero_sudo/docs/stories/documentation_bootstrap.md`): how a module's documentation reaches the knowledge base without each module doing it.

**Content.** A system administrator expects a module's guide to be available after installation. `zero_sudo` attempts the installation centrally, after the registry is fully loaded. For developers it supports both knowledge-article models (it checks which exists) and installs the guide named by the `knowledge_docs` manifest key.

**Special concerns.** Documentation only. The installer is `COMM_zero_sudo_doc_installer` in `zero_sudo/__init__.py`.

### `COMM_journey_service_account_lifecycle` [@ANCHOR: COMM_journey_service_account_lifecycle]

**Purpose.** The journey document `zero_sudo/docs/journeys/service_account_lifecycle.md`, following a service account from creation to use in a secure execution context.

**Stages.** (1) Provisioning: a module's XML data defines a `res.users` record with `is_service_account` true, and creation assigns a random password (`COMM_service_account_password_generation`). (2) Isolation check: a browser login as that user is stopped by the interceptor (`COMM_web_login_interceptor`). (3) Secure retrieval: code calls `_get_service_uid` and runs `with_user(uid)` instead of `.sudo()`.

**Special concerns.** A narrative anchor with no arguments or return value; the accounts it describes are the only way Zero-Sudo code gains privilege.

**See also.** `COMM_is_service_account_field`, `COMM_get_service_uid`.

### `COMM_journey_securing_configuration` [@ANCHOR: COMM_journey_securing_configuration]

**Purpose.** The journey document `zero_sudo/docs/journeys/securing_configuration.md`: how a new configuration parameter is integrated without `.sudo()`.

**Stages.** (1) A module needs a setting such as `my_module.api_endpoint`. (2) The developer extends `zero_sudo.security.utils` and adds the key to the read and write whitelists (`_get_param_read_whitelist`, `_get_param_write_whitelist`); both must list it. (3) Code reads and writes it with `_get_system_param` and `_set_system_param`. (4) Protection against exfiltration: unlisted or secret-looking keys are refused and logged.

**Special concerns.** Documentation only.

**See also.** `COMM_get_system_param`, `COMM_set_system_param`.

### `journey_developer_integration` [@ANCHOR: journey_developer_integration]

**Purpose.** The journey "Developer Integration" (`zero_sudo/docs/journeys/developer_integration.md`): the steps a module author follows to use the `zero_sudo` core.

**Steps.** (1) Add `zero_sudo` to `depends` in `__manifest__.py`. (2) Define a service account in the module's XML data with `is_service_account` true. (3) Replace each `.sudo()` with `self.env['zero_sudo.security.utils']._get_service_uid('my_module.my_service_account')` followed by `.with_user(uid)`. The platform linter rejects `.sudo()`.

**Special concerns.** Documentation only.

**See also.** `COMM_story_secure_escalation`, `COMM_journey_service_account_lifecycle`.

### `COMM_get_service_uid` [@ANCHOR: COMM_get_service_uid]

**Purpose.** `_get_service_uid(xml_id)` resolves the XML id of a service account (for example `daemon_key_manager.user_daemon_key_manager_service`) to its user id, so code can run `with_user(uid)` rather than use `.sudo()`. It is the Zero-Sudo way to escalate.

**Interface.** `xml_id` is a string `module.name`. It returns the integer user id.

**Errors.** `AccessError` for a malformed id (empty, not a string, no dot) and for any failure in the database procedures (no such account, inactive, not a service account), because a raw PostgreSQL exception is translated to `AccessError` so existing `except AccessError` handlers work.

**Special concerns.** Flushes the ORM, opens a savepoint (a raised procedure aborts the transaction otherwise) and runs `zero_sudo_get_service_uid(xml_id)`, or on a cache hit only `zero_sudo_verify_service_uid(uid, xml_id)`. The cache is per worker, keyed by database and xml id, and a miss is not written back. See `COMM_get_service_uid_sql_resolve` and `COMM_get_service_uid_sql_verify`.

**See also.** Tests `COMM_test_get_service_uid`, `COMM_story_secure_escalation`.

### `COMM_get_service_uid_sql_resolve` [@ANCHOR: COMM_get_service_uid_sql_resolve]

**Purpose.** The first half of the service-account lookup: resolving an XML id to a user id with a single PostgreSQL procedure call instead of ORM reads.

**Behaviour.** Inside `_get_service_uid`, on a cache miss, `SELECT zero_sudo_get_service_uid(%s)` with the xml id runs in a savepoint and returns the uid from the first column. The procedure raises (`RAISE EXCEPTION`) when the account does not exist, is disabled or is not a service account; the surrounding code converts that `psycopg2.errors.RaiseException` to `AccessError` ("Service account resolution failed for ...").

**Special concerns.** Direct SQL avoids ORM interception and caching. The savepoint matters: a procedure exception aborts the whole transaction, and without it the caller's next statement would fail with `InFailedSqlTransaction`. The result is not stored back in the cache, so a rolled-back test transaction cannot leak a uid.

**See also.** `COMM_test_get_service_uid_sql_resolve`.

### `COMM_get_service_uid_sql_verify` [@ANCHOR: COMM_get_service_uid_sql_verify]

**Purpose.** The second half of the lookup: confirm that the service account is still valid (active and flagged as a service account).

**Behaviour.** On a cache hit `_get_service_uid` already knows the uid and runs only `SELECT zero_sudo_verify_service_uid(%s, %s)` with the uid and the xml id. The same live check also runs inside the full resolve procedure on a miss. If it fails, the procedure raises and the call becomes `AccessError`.

**Special concerns.** Runs on every call, so an account deactivated after the worker started is refused at once even though its uid is cached. It only reads, inside a savepoint with `flush=False` (the ORM is flushed through the caller's own environment first, which avoids Odoo's "missing default_env" public-user lookup on public requests).

**See also.** Tests `COMM_test_get_service_uid_sql_verify`, `COMM_test_privilege_escalation_block_sql`.

### `COMM_test_get_service_uid` [@ANCHOR: COMM_test_get_service_uid]

**Purpose.** The test of `_get_service_uid` as a whole: a valid service account resolves to its uid and invalid ids are refused.

**Scenario.** `zero_sudo/tests/test_security_utils.py` calls the function with a real service-account xml id and a bad one, asserting the uid in the first case and `AccessError` (or the translated database exception) in the second; the recorder part is described under the SQL anchors.

**What it guards.** `COMM_get_service_uid` and the story `COMM_story_secure_escalation`.

**Special concerns.** `TransactionCase`.

### `COMM_get_system_param` [@ANCHOR: COMM_get_system_param]

**Purpose.** `_get_system_param(key, default=False)` reads an `ir.config_parameter` for code that may not use `.sudo()`, while stopping a template-injection attacker from reading secrets this way.

**Interface.** `key` is the parameter name; `default` is returned when it is unset. It returns the stored string or the default.

**Errors.** `AccessError` when the key is not on the read whitelist (`_get_param_read_whitelist`). A key containing `secret`, `key`, `password`, `token`, `auth`, `crypt` or `cert` gets the message that it matches restricted cryptographic patterns; any other gets the message to register it in `zero_sudo/models/security_utils.py`.

**Side effects.** A denied read writes a `zero_sudo.security.log` row (reason `param_access_denied`, login set to the key). An allowed read runs as the service account `zero_sudo.config_service_internal`.

**See also.** Test `COMM_test_01_mechanical_secret_block_enforcement`; story `COMM_story_parameter_whitelisting`.

### `COMM_deterministic_hash` [@ANCHOR: COMM_deterministic_hash]

**Purpose.** `_get_deterministic_hash(input_string)` turns a string into a stable 32-bit integer, used mainly as the key for PostgreSQL advisory locks (`pg_advisory_xact_lock`), which need an integer.

**Interface.** `input_string` is any value; a non-string is converted with `str()`. It returns an `int` from 0 up to 2147482646: the first eight hex digits of the SHA-256 of the UTF-8 text, read as an integer and taken modulo 2147483647. It raises nothing.

**Special concerns.** Pure and deterministic across processes and restarts, so two workers locking the same name take the same lock. It is not a security hash: different strings can collide, which only makes two unrelated resources share a lock (safe, just slower).

**See also.** Test `COMM_test_deterministic_hash`; story `COMM_story_deterministic_hash`.

### `COMM_get_crypto_secret` [@ANCHOR: COMM_get_crypto_secret]

**Purpose.** `_get_crypto_secret()` supplies the platform's root cryptographic secret to code that may not use `.sudo()` or read the database. It is the only sanctioned way to obtain it; the parameter whitelist refuses crypto keys by design.

**Interface.** No arguments. It returns the secret as a string, or the empty string `""` when none is configured.

**Order of lookup.** The environment variable `HAMS_CRYPTO_KEY`; else the file `/var/lib/odoo/hams_crypto.secret` (stripped); else Odoo's `admin_passwd`. An `admin_passwd` that is unset or equal to `admin` counts as no secret.

**Errors.** An unreadable file is logged as a warning and skipped. When nothing usable is found it logs an error and returns `""`, never a hardcoded fallback.

**Special concerns.** The method is decorated with `@distributed_cache()` (RAM cache, avoiding repeated file reads), so a changed secret needs a cache clear; the tests clear it. The secret must never be logged or returned to a client.

**See also.** Test `COMM_test_get_crypto_secret`.

### `COMM_coherent_cache_signal` [@ANCHOR: COMM_coherent_cache_signal]

**Purpose.** `_notify_cache_invalidation(model_name, key_value)` tells every other Odoo worker that a model's cached entries are stale, so in-memory caches do not serve old content until their 24 hour expiry. Callers include blog posts and the edge-routing domain code.

**Interface.** `model_name` is a model name; `key_value` must be truthy (a falsy model name or key returns without doing anything) but does not narrow the invalidation, which is always the whole model; it is kept only so existing call signatures stay unchanged. Returns nothing.

**Special concerns.** It delegates to `distributed_redis_cache.redis_cache.notify_model_invalidation`, which sends a PostgreSQL `NOTIFY` on the channel `distributed_cache_invalidation` with a JSON payload `{"model": ..., "dbname": ...}`. The listener daemon `distributed_redis_cache/daemons/cache_manager.py` republishes it to Redis. The notification goes out when the transaction commits, and `NOTIFY` succeeds even when no one listens, so a wrong channel fails silently.

**See also.** Tests `COMM_test_coherent_cache_signal` and `COMM_test_coherent_cache_signal_batch`.

### `COMM_test_coherent_cache_signal` [@ANCHOR: COMM_test_coherent_cache_signal]

**Purpose.** The test that `_notify_cache_invalidation` issues a `pg_notify`.

**Scenario.** `zero_sudo/tests/test_security_utils.py` records `cr.execute` calls, calls `_notify_cache_invalidation("res.partner", "test_key")` and filters the recorded statements for `SELECT pg_notify(%s, %s)`, asserting the channel and JSON payload are those the cache-manager daemon listens for.

**What it guards.** `COMM_coherent_cache_signal`.

**Special concerns.** Restores `execute`; no real listener involved.

### `COMM_set_kv_sql_check` [@ANCHOR: COMM_set_kv_sql_check]

**Purpose.** Marks the raw SQL statement inside `_set_kv` that calls the procedure `zero_sudo_set_kv`, so the repository's SQL audit has a named justification and a test for it.

**Statement.** `SELECT zero_sudo_set_kv(%s, %s)` with the key and the value as bound parameters (never concatenated into the text), so injection is not possible.

**Why raw SQL.** A single upsert call is faster and atomic; the ORM cache is invalidated afterwards by `invalidate_model()` on `zero_sudo.kv`.

**Errors.** Any database error propagates to the caller unchanged.

**See also.** `COMM_set_kv_procedure`, test `test_12_kv_store` (`COMM_test_set_kv_sql_check`).

### `COMM_is_service_account_field` [@ANCHOR: COMM_is_service_account_field]

**Purpose.** The boolean field `is_service_account` on `res.users`, the one flag that marks a user as a background-process identity.

**Definition.** `fields.Boolean(string="Is Service Account", default=False, groups="base.group_system")`; the help text reads "Flags this user as an internal service account. Prevents interactive web logins." Only system administrators can read or write it through the ORM. Data files set it; `create` and `write` on `res.users` assign a random password when it is true.

**Who reads it.** The login interceptor (`COMM_web_login_interceptor_check`), `_get_service_uid` (a service uid must be active and flagged), the daemon key manager (`user_id` must be a service account) and the repository-wide cron audit that tells a narrow service-account cron owner from an ordinary admin.

**Concurrency.** An ordinary column; a change takes effect on the next request.

**See also.** `COMM_test_is_service_account_field`.

### `COMM_web_login_interceptor` [@ANCHOR: COMM_web_login_interceptor]

**Purpose.** Keeps service accounts out of the interactive web interface. `ZeroSudoHome.web_login` (in `zero_sudo/controllers/main.py`) wraps Odoo's own `/web/login` route and, after a successful sign-in, ends the session when the account is a service account.

**Interface.** `web_login(self, redirect=None, login=None, **kw)`, an HTTP route. `redirect` is Odoo's post-login target and `login` the attempted login name; if `login` is not a named argument the handler takes it from `request.params`. It calls the parent route first and returns its response, or a redirect back to the login page with an error when blocked.

**Errors.** None raised to the caller; a blocked login is a redirect.

**Special concerns.** Runs per request, on the request's own cursor. The check itself is `COMM_web_login_interceptor_check`. A blocked attempt is logged to `zero_sudo.security.log` with the trusted client IP.

**See also.** Test `COMM_test_web_login_interceptor`; story `COMM_story_login_blocking`.

### `COMM_web_login_interceptor_check` [@ANCHOR: COMM_web_login_interceptor_check]

**Purpose.** The decision inside `web_login`: is the user who just authenticated a service account? If so the session is destroyed.

**Behaviour.** When `request.session.uid` is set after the parent route, the code runs `SELECT is_service_account FROM res_users WHERE id = %s` with the uid as parameter, and treats a true first column as a service account. Direct SQL is deliberate: it needs no `.sudo()` and bypasses access rules for the isolation check (ADR-0005), and the source comment tells future developers not to change it to `.sudo()`.

**Side effects.** For a service account it writes a `zero_sudo.security.log` row as the facility service account, recording the login and the IP from `_get_trusted_client_ip()` (not the raw `remote_addr`, which is loopback behind the tunnel), then logs the session out.

**Errors.** No row means no block.

**See also.** `COMM_test_web_login_interceptor_check`.

### `register_hook_preload_service_uids` [@ANCHOR: zero_sudo:register_hook_preload_service_uids]

**Purpose.** The Odoo `_register_hook` override on `zero_sudo.security.utils` that fills the service-account uid cache once when the registry is built, so the first request after start-up does not pay the lookup.

**Interface.** `_register_hook(self)`; no arguments, returns nothing. It calls the parent hook, then `_preload_service_uid_cache()` (`zero_sudo:preload_service_uid_cache`).

**Special concerns.** Odoo runs it once per registry build, in the master process before workers fork, so every worker inherits the filled dictionary by copy-on-write. It only populates the resolution cache; the live safety check still runs on every `_get_service_uid` call.

**See also.** Test in `zero_sudo/tests/test_security_utils.py`.

### `preload_service_uid_cache` [@ANCHOR: zero_sudo:preload_service_uid_cache]

**Purpose.** `_preload_service_uid_cache()` loads the map from service-account XML id to user id into a process-wide dictionary at start-up, so `_get_service_uid` can skip the `ir_model_data` lookup.

**Interface.** No arguments (`@api.model`); returns nothing. It runs `SELECT d.module || '.' || d.name, d.res_id FROM ir_model_data d JOIN res_users u ON u.id = d.res_id WHERE d.model = 'res.users' AND u.is_service_account = true`, then replaces this database's entries in `_SERVICE_UID_CACHE` (keys are `(dbname, xml_id)`) and logs the count.

**Special concerns.** It caches only the resolution, which is fixed once a `noupdate="1"` data record installs, and never the safety verdict (active, flagged, no privilege escalation), which an administrator can change at any time. The cache is a plain dictionary, never evicted, and a lookup miss does not write back. Entries for other databases are kept.

**See also.** `COMM_get_service_uid`, `register_hook_preload_service_uids`.

### `COMM_get_service_uid_no_transaction_flush` [@ANCHOR: zero_sudo:COMM_get_service_uid_no_transaction_flush]

**Purpose.** Marks the part of `_get_service_uid` that flushes pending ORM writes through the caller's own environment and opens the savepoint without Odoo's default flush.

**Why.** The default `savepoint()` flushes the transaction's `default_env`. A public request has none yet when the tenant-site routing resolves its service account, before authentication gives the request a user, so Odoo logged "Missing default_env, flushing as public user" and looked up `base.public_user` on every public request. The procedures only SELECT; the one thing the flush buys is that a pending write to the service user (active flag, groups) is visible, and `self.env.flush_all()` does that. The code then uses `self.env.cr.savepoint(flush=False)`.

**Special concerns.** Side effect only on the transaction: pending writes are sent to the database. No arguments of its own.

**See also.** Test `COMM_test_get_service_uid_no_transaction_flush` in `zero_sudo/tests/test_service_uid_no_default_env.py`.

### `svg_allowlist_install` [@ANCHOR: zero_sudo:svg_allowlist_install]

**Purpose.** `install()` in `zero_sudo/svg_sanitizer.py` makes Odoo's HTML sanitizer SVG-aware, so schematics written as inline SVG survive a write to a `fields.Html` field while the rest of the document is sanitized exactly as before.

**Interface.** No arguments; returns nothing. It replaces `odoo.tools.mail._Cleaner.__call__` with a wrapper (`call_with_svg_allowlist`) that calls `clean_document`, which rebuilds each `<svg>` subtree from an allowlist and hands the remainder to the original method. `style` and `class` are passed through as "dropped" when the cleaner options say so.

**Special concerns.** Idempotent: the wrapper carries the marker `_hams_svg_allowlist`, and a second call returns without wrapping again; the original is kept as `__wrapped__`. It patches the class rather than `html_sanitize`, so modules that imported the function by name are covered. Process-wide, run once at import.

**Security.** Script, `foreignObject`, `use`, animation and external references stay out; only the allowlist is kept. See `zero_sudo:svg_allowlist_sanitizer`.

### `_start_daemon_process` [@ANCHOR: zero_sudo:_start_daemon_process]

**Purpose.** `_start_daemon_process(script_path, args=None, env_vars=None)` in `zero_sudo/models/daemon_utils.py` starts a Python daemon script as a subprocess for integration tests.

**Arguments.** `script_path`: path of the script, run with the current interpreter (`sys.executable`, else `/usr/bin/python3`). `args`: extra command-line arguments (list). `env_vars`: extra environment (dict), merged over a copy of the current environment.

**Returns.** The `subprocess.Popen` handle; the caller must stop it.

**Special concerns.** The child's `PYTHONPATH` gets the parent's `sys.path` prepended so the daemon sees the same modules. It starts in a new session and uses no shell. The log line records the program name and argument count only, because an argument can be a secret and the log outlives the process.

**See also.** Test `zero_sudo/tests/test_tdd_fixes.py`; `_poll_health_check`.

### `_poll_health_check` [@ANCHOR: zero_sudo:_poll_health_check]

**Purpose.** `_poll_health_check(url, timeout=30, interval=1)` waits until a daemon's health URL answers, so a test does not race a daemon that is still starting.

**Arguments.** `url`: an `http` or `https` URL, HEAD-requested; `timeout`: seconds to keep trying (default 30); `interval`: seconds between tries (default 1).

**Returns.** True when the URL answers 200 within the time; false when the timeout elapses.

**Errors.** `UserError` ("Health check URL must use http or https") for any other scheme, a defence against `file://` reads or internal requests; the method is private so it cannot be reached through RPC.

**Special concerns.** Blocks the calling thread with sleeps. Only used by tests against a local daemon.

**See also.** `_start_daemon_process`; test `zero_sudo/tests/test_tdd_fixes.py`.

### `is_storage_full_error` [@ANCHOR: is_storage_full_error]

**Purpose.** `isStorageFullError(err)` in `zero_sudo/static/src/js/offline_store.js` recognises the browser error for "storage full", so the offline logger can tell the operator "Disk Full" and not a generic or misleading error, and a full disk does not silently lose offline contacts.

**Argument.** `err`: any thrown value or DOMException; `null` or `undefined` is accepted.

**Returns.** `true` for a `QuotaExceededError` (Chrome and the specification, legacy code 22) or `NS_ERROR_DOM_QUOTA_REACHED` (Firefox, code 1014), by name or code; otherwise `false`. It never throws.

**Special concerns.** Pure and synchronous; used by `OfflineStore` write paths.

**See also.** Test `zero_sudo/static/tests/offline_store.test.js`.

### `story_multi_website` [@ANCHOR: story_multi_website]

**Purpose.** The story document `zero_sudo/docs/stories/multi_website.md`, which explains how the `zero_sudo` core behaves with several websites on one Odoo.

**Content.** Three points. The service-account login check (`COMM_web_login_interceptor_check`) is global: a flagged account is logged out whatever site it signed in through. The documentation installer (`COMM_zero_sudo_doc_installer`) is website-agnostic and writes to the one central knowledge base. System parameters (`COMM_get_system_param`) are global `ir.config_parameter` rows guarded by the whitelist.

**Special concerns.** Documentation only; no code and no arguments.

### `story_zero_sudo_doc_installer` [@ANCHOR: story_zero_sudo_doc_installer]

**Purpose.** The story "Centralized Documentation Bootstrap" (`zero_sudo/docs/stories/documentation_bootstrap.md`): how a module's documentation reaches the knowledge base without each module doing it.

**Content.** A system administrator expects a module's guide to be available after installation. `zero_sudo` attempts the installation centrally, after the registry is fully loaded. For developers it supports both knowledge-article models (it checks which exists) and installs the guide named by the `knowledge_docs` manifest key.

**Special concerns.** Documentation only. The installer is `COMM_zero_sudo_doc_installer` in `zero_sudo/__init__.py`.

### `journey_developer_integration` [@ANCHOR: journey_developer_integration]

**Purpose.** The journey "Developer Integration" (`zero_sudo/docs/journeys/developer_integration.md`): the steps a module author follows to use the `zero_sudo` core.

**Steps.** (1) Add `zero_sudo` to `depends` in `__manifest__.py`. (2) Define a service account in the module's XML data with `is_service_account` true. (3) Replace each `.sudo()` with `self.env['zero_sudo.security.utils']._get_service_uid('my_module.my_service_account')` followed by `.with_user(uid)`. The platform linter rejects `.sudo()`.

**Special concerns.** Documentation only.

**See also.** `COMM_story_secure_escalation`, `COMM_journey_service_account_lifecycle`.
