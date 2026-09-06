# Backup Management Module

## Technical Specification

### 1. Automated Volume Synchronization
Handles the execution loops for continuous file system snapshots and system storage mappings.
* **Core Sync Anchor:** `[@ANCHOR: backup_management:COMM_backup_sync_kopia]`

* **Database Target Sync Anchor:** `[@ANCHOR: backup_management:COMM_backup_sync_pgbackrest]`

* **Cron Routine Orchestration:** `[@ANCHOR: backup_management:COMM_cron_sync_all_backups]`

### 2. Retention & Purge Governance
Ensures structural space recovery processes comply with multi-website tenant data privacy mandates.
* **Policy Application Engine:** `[@ANCHOR: backup_management:COMM_backup_apply_policies]`

* **Interactive Dashboard Telemetry:** `[@ANCHOR: backup_management:COMM_backup_board_data]`

### 3. Encrypted Credential Storage
Kopia repository passwords and object-storage secret keys are never stored in plaintext -- a Fernet symmetric key from `ODOO_BACKUP_CRYPTO_KEY` (or `HAMS_CRYPTO_KEY`) encrypts them at rest, decrypting on read for admin users only.
* **Symmetric Encrypt/Decrypt Primitive:** `[@ANCHOR: backup_management:COMM_crypt_field]`

* **Generic Encrypted-Field Compute:** `[@ANCHOR: backup_management:COMM_compute_encrypted_field]`

* **Generic Encrypted-Field Inverse:** `[@ANCHOR: backup_management:COMM_inverse_encrypted_field]`

* **Kopia Password Compute:** `[@ANCHOR: backup_management:COMM_compute_kopia_password]`

* **Kopia Password Inverse:** `[@ANCHOR: backup_management:COMM_inverse_kopia_password]`

* **Object Storage Secret Key Compute:** `[@ANCHOR: backup_management:COMM_compute_secret_key]`

* **Object Storage Secret Key Inverse:** `[@ANCHOR: backup_management:COMM_inverse_secret_key]`

### 4. Path and Stanza Validation
Rejects path traversal, shell metacharacters, and symlink escapes on any filesystem-facing configuration field, and enforces a strict alphanumeric/underscore stanza name for pgBackRest.
* **Security Path Constraint:** `[@ANCHOR: backup_management:COMM_check_security_paths]`

### 5. Asynchronous Bastion Dispatch (ADR-0071)
All engine operations (sync, policy application, restore drills) are offloaded to the RabbitMQ-backed worker daemon rather than executed inline, so a slow or hanging backup engine never blocks the web request.
* **Worker Dispatch Gate:** `[@ANCHOR: backup_management:COMM_publish_to_worker]`

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

* None defined in `__manifest__.py`.

## Cross-Module Interfaces

### Compliance Monitoring
When multi-website context isolation checks detect data boundary leakage or cross-tenant contamination, logging structures communicate directly with the core website security system:
* **Tenant Violation Reports:** For tracking frontend moderation workflow alerts, see `[@ANCHOR: user_websites:UX_REPORT_VIOLATION]`.
* **Automated Escalation:** System telemetry monitors structural volume metrics and communicates alerts dynamically.
