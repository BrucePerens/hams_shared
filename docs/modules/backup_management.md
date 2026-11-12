# Backup Management Module

## Technical Specification

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

### 1. Automated Volume Synchronization
Copies the snapshot list reported by Kopia (`kopia snapshot list`, for files) or pgBackRest (`pgbackrest info`, for PostgreSQL) into `backup.snapshot`, from the hourly sync cron or the Sync Snapshots button. A separate daily cron queues the backups themselves; the hourly one never creates a backup.
* **Core Sync Anchor:** `backup_management:COMM_backup_sync_kopia`

* **Database Target Sync Anchor:** `backup_management:COMM_backup_sync_pgbackrest`

* **Cron Routine Orchestration:** `backup_management:COMM_cron_sync_all_backups`

### 2. Retention & Purge Governance
Applies Kopia retention and exclusion policies (only when an administrator clicks Apply Policies; it is skipped for pgBackRest configurations, where only Keep Daily is used, as `--repo1-retention-full`), and supplies the per-configuration data on the Backup Dashboard (filtered to the current website or no website).
* **Policy Application Engine:** `backup_management:COMM_backup_apply_policies`

* **Interactive Dashboard Telemetry:** `backup_management:COMM_backup_board_data`

### 3. Encrypted Credential Storage
Kopia repository passwords and object-storage secret keys are never stored in plaintext -- a Fernet symmetric key from the server environment variable `ODOO_BACKUP_CRYPTO_KEY` (or `HAMS_CRYPTO_KEY`) encrypts them at rest. The plain-text fields are computed on read and are visible only to the Backup Administrator and backup service-account groups. With no key configured, saving a secret raises an error instead of silently discarding it; with a wrong or rotated key, the field reads as `***ERROR***`.
* **Symmetric Encrypt/Decrypt Primitive:** `backup_management:COMM_crypt_field`

* **Generic Encrypted-Field Compute:** `backup_management:COMM_compute_encrypted_field`

* **Generic Encrypted-Field Inverse:** `backup_management:COMM_inverse_encrypted_field`

* **Kopia Password Compute:** `backup_management:COMM_compute_kopia_password`

* **Kopia Password Inverse:** `backup_management:COMM_inverse_kopia_password`

* **Object Storage Secret Key Compute:** `backup_management:COMM_compute_secret_key`

* **Object Storage Secret Key Inverse:** `backup_management:COMM_inverse_secret_key`

### 4. Path and Stanza Validation
Resolves symlinks and requires a Kopia local-storage target and any restore-drill script path to lie inside an allowed backup directory (`validate_backup_path()` in `models/utils.py`), and rejects a leading hyphen and shell metacharacters. Also enforces a strict alphanumeric/underscore stanza name for pgBackRest.
* **Security Path Constraint:** `backup_management:COMM_check_security_paths`

### 5. Asynchronous Bastion Dispatch (ADR-0071)
Engine operations (sync, backup, policy application, restore drills) are offloaded to the RabbitMQ-backed worker daemon rather than executed inline, so a slow or hanging backup engine never blocks the web request.
* **Worker Dispatch Gate (creates a `pending` `backup.job`, sends the RabbitMQ message only after the transaction commits, and refuses callers who are neither Backup Administrators nor the backup service account; a failed send marks the job `failed`):** `backup_management:COMM_publish_to_worker`

* **Automated Restore Drill Trigger:** `backup_management:COMM_execute_restore_drill`

### 6. RabbitMQ Worker Daemon
The out-of-process `daemon/main.py` consumer that actually executes Kopia/pgBackRest commands and reports results back to Odoo over the JSON-2 API.
* **Odoo JSON-2 API Client:** `backup_management:COMM_json2_call`

* **Credential Fail-Fast Guard:** `backup_management:COMM_require_rabbitmq_credentials`

### 7. Installation and UI Support
* **Daemon Key Registration on Install:** `backup_management:COMM_post_init_hook`

* **Live Job Log Streaming:** `backup_management:COMM_append_log`

* **Manual Status Refresh:** `backup_management:COMM_action_refresh_status`

* **Legacy Dashboard Redirect:** `backup_management:COMM_backup_board`

## External Dependencies

* Odoo modules: `zero_sudo`, `binary_downloader`, `pager_duty` (failure and stale-backup alerts), `knowledge`, `hams_rabbitmq`, `daemon_key_manager`, plus `base`, `mail` and `website`. Python: `cryptography` (Fernet) and `pika` (RabbitMQ).

## Cross-Module Interfaces

### Compliance Monitoring
This module does not itself detect or report cross-tenant data leakage. Tenant isolation is enforced by record rules in `security/security.xml`, which limit Backup Administrators to configurations, snapshots and jobs of their own companies and of no website or their own website. Related reporting elsewhere:
* **Tenant Violation Reports:** For tracking frontend moderation workflow alerts, see `user_websites:UX_REPORT_VIOLATION`.
* **Automated Escalation:** Backup failures, stale backups (newest snapshot older than 26 hours) and snapshots smaller than a configuration's Minimum Size are escalated through `report_backup_failure()` to the `pager_duty` module, which opens a critical incident.

## Anchor reference

One entry per anchor, written from the code in `backup_management/` (models, daemon, hooks, controllers). Test anchors share the id of the function they test: the tests carry a "Tests" comment naming the same id, in `backup_management/tests/` and `backup_management/daemon/test_*.py`. See also `backup_management/README.md` for the field-level entries.

### `backup_management:COMM_backup_sync_kopia` [@ANCHOR: backup_management:COMM_backup_sync_kopia]
Purpose: copy the snapshot list of a Kopia repository into `backup.snapshot`. It is carried by `backup.config.action_sync_snapshots()`, a `@public_rpc` method that loops over `self` and calls `_publish_to_worker("sync_snapshots")` for each configuration, then returns `True`. The worker daemon runs `kopia snapshot list --json` (`config_engine == "kopia"`) and calls `action_process_snapshot_data` to upsert the rows. Callers: the Sync Snapshots button, `action_test_connection()` and the hourly `cron_sync_all_backups()`.

Special concerns: asynchronous, nothing runs inline; each call creates one `pending` `backup.job` per configuration. It never creates a backup. Access is gated by `_publish_to_worker`. Test: `tests/test_backup.py`. See also `COMM_publish_to_worker`, `COMM_cron_sync_all_backups`.

### `backup_management:COMM_backup_sync_pgbackrest` [@ANCHOR: backup_management:COMM_backup_sync_pgbackrest]
Purpose: the same synchronisation for a PostgreSQL configuration (`engine == "pgbackrest"`). It shares the single method `action_sync_snapshots()` with the Kopia anchor; the difference is in the daemon, which runs `pgbackrest info --stanza=<target_path> --output=json`, adding `--repo=<n>` when the configuration names a repository number, or the S3 arguments for `s3`/`b2` storage. `info` is read-only and runs as the daemon user directly, unlike `pgbackrest backup`, which goes through the privileged sidecar.

Special concerns: the stanza name must match `^[a-zA-Z0-9_]+$` (see `COMM_check_security_paths`). Test: `tests/test_backup.py`. See also `COMM_backup_sync_kopia`.

### `backup_management:COMM_cron_sync_all_backups` [@ANCHOR: backup_management:COMM_cron_sync_all_backups]
Purpose: the hourly cron entry point, `backup.config.cron_sync_all_backups()`, decorated `@api.private @api.model`, with no arguments and no return value.

Behaviour, in order: it runs as the backup service account and searches up to 1000 configurations; reads the newest snapshot start time of each with one SQL query; then for each configuration calls `action_sync_snapshots()`. It reports a stale-backup incident through `report_backup_failure()` when the newest snapshot is older than 26 hours (allowing 24 hour cron jitter), reports each problem from `_backup_job_problems()` (three consecutive failed jobs, or no successful job in 26 hours), and queues `_execute_restore_drill()` when a restore drill script is set and `last_drill_time` is older than 7 days.

Special concerns: it never creates a backup; the separate daily `cron_trigger_scheduled_backups()` does that. Alerts repeat every hour while the condition lasts; `pager_duty` deduplicates by source. Test: `tests/test_backup.py` (cron test).

### `backup_management:COMM_backup_apply_policies` [@ANCHOR: backup_management:COMM_backup_apply_policies]
Purpose: push the retention and exclusion settings of a Kopia configuration to the repository. `action_apply_policies()` is a `@public_rpc` method run only when an administrator clicks Apply Policies.

Behaviour: for each record it skips configurations whose engine is not `kopia` and otherwise calls `_publish_to_worker("kopia_policy")`. The daemon runs `kopia policy set -- <target_path>` with `--keep-latest`, `--keep-daily`, `--keep-weekly`, `--keep-monthly` taken from `keep_daily`, `keep_weekly`, `keep_monthly` (defaults 7, 4, 6) and one `--add-ignore=<line>` per non-blank line of `exclude_patterns`. Returns `True`, or the form action dictionary when exactly one record was given and a job was created. pgBackRest retention is applied by the backup command itself (`--repo1-retention-full`). Test: `tests/test_backup.py`.

### `backup_management:COMM_backup_board_data` [@ANCHOR: backup_management:COMM_backup_board_data]
Purpose: supply the per-configuration data the Backup Dashboard shows. `backup.config.get_board_data()` is a `@public_rpc @api.model` method with no arguments; the context key `website_id` limits the result to configurations with no website or that website.

Returns a list of dictionaries (at most 10000), each holding `id`, `name`, `engine`, `target_path`, plus `latest_snapshot` (the row of the SQL view `backup.latest.snapshot.view`: `snapshot_id`, `start_time`, `size_bytes`, `status`, or `False`), `is_stale` (`True` when there is no snapshot or it is more than 26 hours old) and `latest_job` (the newest `backup.job`: `state`, `job_type`, `create_date`, or `False`). An empty database gives `[]`. It uses three queries in total (no per-row queries) and is read-only. Test: `tests/test_backup_view.py`.

### `backup_management:COMM_crypt_field` [@ANCHOR: backup_management:COMM_crypt_field]
Purpose: the single symmetric encrypt and decrypt primitive for the stored secrets, `backup.config._crypt_field(value, decrypt=False)`.

| Argument | Type | Meaning |
|---|---|---|
| `value` | str or False | The plaintext (encrypt) or the stored Fernet token (decrypt). |
| `decrypt` | bool | `False` encrypts, `True` decrypts. |

Returns the token (encrypt) or plaintext (decrypt) as a string, `False` for an empty value, and the literal `"***ERROR***"` when a decrypt fails with `InvalidToken` or `ValueError` (wrong or rotated key, corrupt value; the failure is logged as a warning). Error: with no key configured (see `_get_fernet`, which reads `ODOO_BACKUP_CRYPTO_KEY` then `HAMS_CRYPTO_KEY`), encrypting a non-empty value raises `UserError` so a secret is never silently dropped; decrypting or encrypting an empty value returns `False`. No locking and no side effects beyond the log. The key is never logged. Test: `tests/test_backup_crypto.py`.

### `backup_management:COMM_compute_encrypted_field` [@ANCHOR: backup_management:COMM_compute_encrypted_field]
Purpose: the shared body of the compute methods of the two secret fields. `_compute_encrypted_field(plain_field, crypt_field)` takes two field names (strings), and for every record in `self` sets the plain field to `_crypt_field(<crypt field>, decrypt=True)`. Nothing is returned. The plain fields are not stored, so this runs on read; the field-level `groups` limit who may read them to Backup Administrators and the backup service account. A wrong key shows as `***ERROR***`; with no key the value reads as `False`. Used by `COMM_compute_kopia_password` and `COMM_compute_secret_key`. Test: `tests/test_backup_crypto.py`.

### `backup_management:COMM_inverse_encrypted_field` [@ANCHOR: backup_management:COMM_inverse_encrypted_field]
Purpose: the shared body of the inverse (write) methods. `_inverse_encrypted_field(plain_field, crypt_field)` takes two field names and, for every record, stores `_crypt_field(<plain field>)` into the stored `*_crypt` column. Nothing is returned. It is called by Odoo when the plain computed field is written. An empty plain value stores `False`. With no key configured and a non-empty value it raises `UserError` from `_crypt_field`, so the write fails and nothing is saved. Used by `COMM_inverse_kopia_password` and `COMM_inverse_secret_key`. Test: `tests/test_backup_crypto.py`.

### `backup_management:COMM_compute_kopia_password` [@ANCHOR: backup_management:COMM_compute_kopia_password]
Purpose: compute method of the field `kopia_password`. Decorated `@api.depends("kopia_password_crypt")`, it calls `_compute_encrypted_field("kopia_password", "kopia_password_crypt")`, so the plaintext Kopia repository password is decrypted from the stored column each time it is read. Arguments: none beyond `self`. Returns nothing. Visible only to the groups `group_backup_admin` and `group_backup_service_account`; the service account needs it to build the worker payload in `_publish_to_worker`. Test: `tests/test_backup_crypto.py`.

### `backup_management:COMM_inverse_kopia_password` [@ANCHOR: backup_management:COMM_inverse_kopia_password]
Purpose: inverse method of `kopia_password`. It calls `_inverse_encrypted_field("kopia_password", "kopia_password_crypt")`, encrypting a password typed into the form into `kopia_password_crypt`. Arguments: none beyond `self`. Returns nothing. Raises `UserError` when no encryption key is configured on the server, so an admin is told instead of saving a Kopia repository with no password. Rotating the key makes stored passwords unreadable (`***ERROR***`) until they are entered again. Test: `tests/test_backup_crypto.py`.

### `backup_management:COMM_compute_secret_key` [@ANCHOR: backup_management:COMM_compute_secret_key]
Purpose: compute method of `secret_key`, the object-storage (S3 or Backblaze B2) secret access key. `@api.depends("secret_key_crypt")`; it calls `_compute_encrypted_field("secret_key", "secret_key_crypt")`. Arguments: none beyond `self`; returns nothing. The companion `access_key` is not encrypted but has the same field groups. Readable only by Backup Administrators and the backup service account. Test: `tests/test_backup_crypto.py`.

### `backup_management:COMM_inverse_secret_key` [@ANCHOR: backup_management:COMM_inverse_secret_key]
Purpose: inverse method of `secret_key`; it calls `_inverse_encrypted_field("secret_key", "secret_key_crypt")` so the secret key is stored only as a Fernet token. Arguments: none beyond `self`; returns nothing. Raises `UserError` when the server has no encryption key. Clearing the field stores `False`. The plaintext travels to the worker only inside the RabbitMQ job payload built by `_publish_to_worker`, which is why the message queue must be treated as a secret-bearing channel. Test: `tests/test_backup_crypto.py`.

### `backup_management:COMM_check_security_paths` [@ANCHOR: backup_management:COMM_check_security_paths]
Purpose: the constraint `_check_security_paths()` (`@api.constrains("target_path", "restore_drill_script", "engine", "storage_type")`) that stops a configuration holding a dangerous path or name.

Rules: for a Kopia configuration with local storage, `target_path` goes through `validate_backup_path()` (`models/utils.py`): it must resolve, after symlinks, inside one of the allowed directories (`/var/lib/odoo/backups`, `/var/lib/odoo/backup_repo`, `/var/backups/global`, `/opt/hams/backup`, `/mnt/backup`, `/opt/hams/daemons/backup_worker/scripts`), must not start with `-`, contain a shell metacharacter, or contain a `..` component. For pgBackRest, `target_path` is a stanza name and must match `^[a-zA-Z0-9_]+$`. A non-empty `restore_drill_script` goes through `validate_backup_path()`. Errors: `UserError` (path) or `ValidationError` (stanza). The same path check runs again in `_publish_to_worker`, and the daemon re-validates the script. Test: `tests/test_backup_security.py`.

### `backup_management:COMM_publish_to_worker` [@ANCHOR: backup_management:COMM_publish_to_worker]
Purpose: the one gate through which every engine operation is handed to the worker (ADR-0071). `_publish_to_worker(engine, payload_extra=None)`.

| Argument | Type | Meaning |
|---|---|---|
| `engine` | str | The action: the configuration's own engine (`kopia`, `pgbackrest`), or `kopia_policy`, `sync_snapshots`, `restore_drill`, `restore_cmd`. |
| `payload_extra` | dict or None | Extra payload keys merged over the base payload (for example `script`). |

Behaviour: raises `AccessError` unless the caller is superuser or in `group_backup_admin` or `group_backup_service_account`. For each record it re-validates a local Kopia path, creates a `backup.job` as the service account with state `pending` and log `Queued in RabbitMQ...`, builds a JSON payload (job and configuration ids, engine, `config_engine`, target, storage settings, `access_key`, `secret_key`, `kopia_password`, retention, exclusions) and registers `publish_to_rabbitmq` on `cr.postcommit`, so the message is sent only after the transaction commits; a failed send marks the job `failed`. It deliberately does not switch company. Returns the form action of the new job when one record was processed, otherwise `True`. Tests: `tests/test_backup_security.py`.

### `backup_management:COMM_execute_restore_drill` [@ANCHOR: backup_management:COMM_execute_restore_drill]
Purpose: queue the automated restore drill, a periodic check that a backup can be restored. `_execute_restore_drill()` requires exactly one record (`ensure_one`), validates `restore_drill_script` with `validate_backup_path()` when it is set, and returns `_publish_to_worker("restore_drill", {"script": <path>})`. The daemon refuses the job unless the script is a `.py` file, executable, inside `/opt/hams/daemons/backup_worker/scripts` (or `BACKUP_WORKER_SCRIPTS_DIR`) and free of `..`; on success it writes `last_drill_time`. Called from `cron_sync_all_backups()` when the last drill is older than 7 days. Errors: `UserError` for a bad path, `AccessError` from the gate. Test: `tests/test_backup.py`.

### `backup_management:COMM_json2_call` [@ANCHOR: backup_management:COMM_json2_call]
Purpose: the worker daemon's client for Odoo's JSON-2 API, `_json2_call(model, method_name, svc_uid=None, **kwargs)` in `daemon/main.py`.

| Argument | Type | Meaning |
|---|---|---|
| `model` | str | Odoo model name, for example `backup.job`. |
| `method_name` | str | Method to call. |
| `svc_uid` | int or None | Sent as header `X-Odoo-Service-Uid` when given. |
| `kwargs` | any | Sent as the JSON body (`ids`, `vals`, ...). |

It POSTs to `<ODOO_URL>/json/2/<model>/<method_name>` with the bearer API key and `X-Odoo-Database`, 15 second timeout. Returns the decoded JSON. Errors: `OdooUnavailableError` (an `OdooAPIError` subclass) for HTTP 401, 403, 5xx or a connection failure, so the consumer puts the job back on the queue; `OdooAPIError` for other HTTP errors, an `error` field in the reply, or bad JSON. On the first 401 it re-reads the key file once (`_reload_odoo_key`) and retries, because a rotated key once stopped all backups. Blocking, synchronous, no retries beyond that. The key is never logged. Tests: `daemon/test_main.py`.

### `backup_management:COMM_require_rabbitmq_credentials` [@ANCHOR: backup_management:COMM_require_rabbitmq_credentials]
Purpose: fail-fast guard at the start of the worker daemon. `_require_rabbitmq_credentials()` takes no arguments and returns nothing; it raises `RuntimeError` when `RMQ_USER` or `RMQ_PASS` is empty, refusing to fall back to RabbitMQ's well-known `guest`/`guest` account. The daemon runs backup and restore commands with real file access, so it must never connect with default credentials. Called by `main()` before the consumer connects. Side effects: none. Test: `tests/test_backup_worker_real.py`.

### `backup_management:COMM_post_init_hook` [@ANCHOR: backup_management:COMM_post_init_hook]
Purpose: the module install hook, `post_init_hook(env)` in `hooks.py`. It looks up the service account `backup_management.user_backup_service_internal` through `zero_sudo.security.utils._get_service_uid`, registers the daemon "Backup Worker RabbitMQ Consumer" in `daemon.key.registry` (key file `/opt/hams/etc/keys/backup_worker.env`) so the key vault can provision its API key, and calls `_bootstrap_knowledge_docs()` to install the module's manuals. Argument `env` is the Odoo environment supplied by the installer; nothing is returned. It runs as the service account, not as superuser. Idempotent through the registry's `register_daemon`. Test: `tests/test_hooks.py`.

### `backup_management:COMM_append_log` [@ANCHOR: backup_management:COMM_append_log]
Purpose: let the worker stream output into a job. `backup.job.append_log(text_chunk)` is `@public_rpc`; for each record it appends `text_chunk` (str) to `output_log`, or sets it when the log is empty, so large buffers are never resent. Returns nothing. It is called over JSON-2 by the daemon as the command prints. Not atomic: two concurrent appends to one job can lose a chunk, which the single-consumer worker (prefetch 1) avoids. The log can contain command output, so job read access is limited by the record rules. Test: `tests/test_backup_job.py`.

### `backup_management:COMM_action_refresh_status` [@ANCHOR: backup_management:COMM_action_refresh_status]
Purpose: the Refresh button on the job form. `backup.job.action_refresh_status()` is `@public_rpc`, takes no arguments, does nothing and returns `True`, so the browser re-reads the record and shows the log the worker has appended. It does not change job state; the cleanup of abandoned jobs is done by `_auto_refresh_status()` (processing jobs idle for 2 hours) and `_fail_stale_pending_jobs()` (pending for 6 hours). Test: `tests/test_backup_job.py`.

### `backup_management:COMM_backup_board` [@ANCHOR: backup_management:COMM_backup_board]
Purpose: keep the old URL `/backup/board` working. `BackupBoard.backup_board()` (`controllers/board.py`, `auth="user"`, `website=True`) takes no arguments and returns an HTTP redirect to `/odoo?action=<id>` for the client action `backup_management.action_backup_board_client`, or to `/odoo` when that action does not exist. It reads nothing and writes nothing; the board's data comes from `get_board_data` (`COMM_backup_board_data`) over RPC. Visitors who are not logged in are sent to the login page by Odoo. Test: `tests/test_backup_board_controller.py`.

### `user_websites:UX_REPORT_VIOLATION` [@ANCHOR: user_websites:UX_REPORT_VIOLATION]
Purpose: the public "Report Content Violation" form handler in the `user_websites` module, listed here only as a related report path (this module raises no tenant violation reports). `report_violation(url, reason, description, email, website_honeypot, **post)` on route `/website/report_violation` (POST, public, CSRF-protected). A filled honeypot is discarded silently with a success redirect; `form_shield` checks (proof of work, rate limits) and structural checks follow; the description is cut to 5000 characters; the row is created in `content.violation.report` as the `user_websites` service account, recording the signed-in reporter, and the owner or group is found from the first path segment. Always returns a redirect with `report_submitted=1` or a `report_error=` code. Tests: `user_websites/tests`, anchor `user_websites:test_report_violation_honeypot_rejected`.
