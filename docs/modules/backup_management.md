# Backup Management Module

## Technical Specification

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

### 1. Automated Volume Synchronization
Copies the snapshot list reported by Kopia (`kopia snapshot list`, for files) or pgBackRest (`pgbackrest info`, for PostgreSQL) into `backup.snapshot`, from the hourly sync cron or the Sync Snapshots button. A separate daily cron queues the backups themselves; the hourly one never creates a backup.
* **Core Sync Anchor:** `[@ANCHOR: backup_management:COMM_backup_sync_kopia]`

* **Database Target Sync Anchor:** `[@ANCHOR: backup_management:COMM_backup_sync_pgbackrest]`

* **Cron Routine Orchestration:** `[@ANCHOR: backup_management:COMM_cron_sync_all_backups]`

### 2. Retention & Purge Governance
Applies Kopia retention and exclusion policies (only when an administrator clicks Apply Policies; it is skipped for pgBackRest configurations, where only Keep Daily is used, as `--repo1-retention-full`), and supplies the per-configuration data on the Backup Dashboard (filtered to the current website or no website).
* **Policy Application Engine:** `[@ANCHOR: backup_management:COMM_backup_apply_policies]`

* **Interactive Dashboard Telemetry:** `[@ANCHOR: backup_management:COMM_backup_board_data]`

### 3. Encrypted Credential Storage
Kopia repository passwords and object-storage secret keys are never stored in plaintext -- a Fernet symmetric key from the server environment variable `ODOO_BACKUP_CRYPTO_KEY` (or `HAMS_CRYPTO_KEY`) encrypts them at rest. The plain-text fields are computed on read and are visible only to the Backup Administrator and backup service-account groups. With no key configured, saving a secret raises an error instead of silently discarding it; with a wrong or rotated key, the field reads as `***ERROR***`.
* **Symmetric Encrypt/Decrypt Primitive:** `[@ANCHOR: backup_management:COMM_crypt_field]`

* **Generic Encrypted-Field Compute:** `[@ANCHOR: backup_management:COMM_compute_encrypted_field]`

* **Generic Encrypted-Field Inverse:** `[@ANCHOR: backup_management:COMM_inverse_encrypted_field]`

* **Kopia Password Compute:** `[@ANCHOR: backup_management:COMM_compute_kopia_password]`

* **Kopia Password Inverse:** `[@ANCHOR: backup_management:COMM_inverse_kopia_password]`

* **Object Storage Secret Key Compute:** `[@ANCHOR: backup_management:COMM_compute_secret_key]`

* **Object Storage Secret Key Inverse:** `[@ANCHOR: backup_management:COMM_inverse_secret_key]`

### 4. Path and Stanza Validation
Resolves symlinks and requires a Kopia local-storage target and any restore-drill script path to lie inside an allowed backup directory (`validate_backup_path()` in `models/utils.py`), and rejects a leading hyphen and shell metacharacters. Also enforces a strict alphanumeric/underscore stanza name for pgBackRest.
* **Security Path Constraint:** `[@ANCHOR: backup_management:COMM_check_security_paths]`

### 5. Asynchronous Bastion Dispatch (ADR-0071)
Engine operations (sync, backup, policy application, restore drills) are offloaded to the RabbitMQ-backed worker daemon rather than executed inline, so a slow or hanging backup engine never blocks the web request.
* **Worker Dispatch Gate (creates a `pending` `backup.job`, sends the RabbitMQ message only after the transaction commits, and refuses callers who are neither Backup Administrators nor the backup service account; a failed send marks the job `failed`):** `[@ANCHOR: backup_management:COMM_publish_to_worker]`

* **Automated Restore Drill Trigger:** `[@ANCHOR: backup_management:COMM_execute_restore_drill]`

### 6. RabbitMQ Worker Daemon
The out-of-process `daemon/main.py` consumer that actually executes Kopia/pgBackRest commands and reports results back to Odoo over the JSON-2 API.
* **Odoo JSON-2 API Client:** `[@ANCHOR: backup_management:COMM_json2_call]`

* **Credential Fail-Fast Guard:** `[@ANCHOR: backup_management:COMM_require_rabbitmq_credentials]`

### 7. Installation and UI Support
* **Daemon Key Registration on Install:** `[@ANCHOR: backup_management:COMM_post_init_hook]`

* **Live Job Log Streaming:** `[@ANCHOR: backup_management:COMM_append_log]`

* **Manual Status Refresh:** `[@ANCHOR: backup_management:COMM_action_refresh_status]`

* **Legacy Dashboard Redirect:** `[@ANCHOR: backup_management:COMM_backup_board]`

## External Dependencies

* Odoo modules: `zero_sudo`, `binary_downloader`, `pager_duty` (failure and stale-backup alerts), `knowledge`, `hams_rabbitmq`, `daemon_key_manager`, plus `base`, `mail` and `website`. Python: `cryptography` (Fernet) and `pika` (RabbitMQ).

## Cross-Module Interfaces

### Compliance Monitoring
This module does not itself detect or report cross-tenant data leakage. Tenant isolation is enforced by record rules in `security/security.xml`, which limit Backup Administrators to configurations, snapshots and jobs of their own companies and of no website or their own website. Related reporting elsewhere:
* **Tenant Violation Reports:** For tracking frontend moderation workflow alerts, see `[@ANCHOR: user_websites:UX_REPORT_VIOLATION]`.
* **Automated Escalation:** Backup failures, stale backups (newest snapshot older than 26 hours) and snapshots smaller than a configuration's Minimum Size are escalated through `report_backup_failure()` to the `pager_duty` module, which opens a critical incident.
