# Database Management (`database_management`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

The `database_management` module provides a comprehensive suite of Database Administration (DBA) and Application Performance Monitoring (APM) tools directly within the Odoo interface. It is designed to empower Site Reliability Engineers (SREs) and administrators with the ability to monitor, tune, and scale the PostgreSQL database without requiring shell access.

---

## 🚀 Key Features

*   **Stat Tracking:** Real-time visibility into table bloat, index usage, and cache hit ratios.
*   **Slow Query Monitoring (APM):** Identifies the most resource-intensive SQL queries using `pg_stat_statements`.
*   **Active Session Management:** View and terminate runaway database sessions, using batch operations.
*   **Slow Query Explain:** Generate `EXPLAIN (ANALYZE, BUFFERS)` plans for slow queries; only `SELECT` and `WITH` queries are accepted.
*   **Index Advisor:** Lists candidate tables for a missing index: those with more than 100 sequential scans and larger than 10 MB. It does not propose specific index definitions.
*   **Replication Monitoring:** Tracks PostgreSQL replication lag and status across the cluster.
*   **Performance Tuning Wizard:** Automatically calculates optimal PostgreSQL parameters based on hardware specifications and applies them via `ALTER SYSTEM`.
*   **High Availability Orchestrator:** Generates production-ready configurations for a two-node cluster: a Patroni YAML file for each node (pointing at your existing etcd hosts) and a PgBouncer INI file. It displays them for copying; it does not deploy anything and does not generate etcd's own configuration.
*   **Automated Alerts:** Integrates with PagerDuty to notify SREs when table bloat exceeds critical thresholds. "PagerDuty" here is this project's own `pager_duty` Odoo module, not the commercial service. A daily cron job opens one incident (severity medium) listing every table with more than 20% dead tuples and more than 10,000 dead tuples.
*   **Zero-Sudo Architecture:** Ensures all operations are performed with minimum necessary privileges using dedicated service accounts.

---

## 🛠 Architecture & Security

### Micro-Privilege Architecture
This module strictly adheres to a Zero-Sudo policy (no `.sudo()`; see [zero_sudo.md](zero_sudo.md)). Session termination (`pg_terminate_backend`), query-statistics reset (`pg_stat_statements_reset`) and Explain run as the `user_database_management_service` service account. Privilege elevation is handled via `_get_service_env()` from the `zero_sudo` module, ensuring that no `sudo()` calls are used in the codebase. The Optimization Wizard's `ALTER SYSTEM` statements do not use the service account; they run on a separate database cursor opened from the Odoo registry. The service account is an Odoo user, not a PostgreSQL role: its SQL still runs over Odoo's own database connection, so PostgreSQL-level permissions are those of Odoo's database user.

### Global Scope
The models are logically global: they report PostgreSQL system statistics for the whole current database (for replication, the whole PostgreSQL server), not for any one Odoo company.

### Security Hardening
*   **SQL Injection Prevention:** The Optimization Wizard builds its `ALTER SYSTEM` statements with the `psycopg2.sql` library (`sql.Identifier` for the parameter name, `sql.Literal` for the value). Other runtime values are passed as bound query parameters. `[@ANCHOR: pg_optimize_wizard]`

*   **Input Validation:** The HA wizard parses node IP addresses with Python's `ipaddress` module, requires cluster and user names to match `^[a-zA-Z0-9_]+$`, and requires a replication password of at least 8 characters. `[@ANCHOR: pg_ha_wizard]`

*   **Binary Safety:** Execution of external binaries (e.g., `vacuumdb`) is managed via `zero_sudo.security.utils._ensure_executable()`, which uses the binary found on `PATH` or, if there is none, asks `binary_downloader` to install it; `vacuumdb` runs without a shell and with a minimal environment. `[@ANCHOR: vacuum_analyze]`
*   **Access Control:** Model access rights are granted to the `database_management.group_database_management_manager` group ("Database Manager"). Odoo's `base.group_system` (Settings) implies that group, and the **Database & SRE** menu is shown only to `base.group_system`.

### Components
*   **Stat Views:** Native PostgreSQL statistics are exposed via Odoo models (`database.table.stat`, `database.index.stat`, `database.query.stat`, `database.activity`, `database.replication.stat`, `database.index.advisor`, `database.pg.setting`) using PostgreSQL views. `[@ANCHOR: db_index_stats]`

*   **Vacuum Automation:** Manual `VACUUM ANALYZE` is triggered via `subprocess` calling `vacuumdb`, bypassing Odoo's transaction blocks to allow physical cleanup. `[@ANCHOR: vacuum_analyze]`

*   **Configuration Management:** The Optimization Wizard `[@ANCHOR: pg_optimize_wizard]` writes to `postgresql.auto.conf` and reloads the configuration.

---

## 📚 Documentation & Help

User-facing documentation is available within the Knowledge module when it is installed (this repository's open-source `knowledge` module or Odoo Enterprise's Knowledge app): `zero_sudo`'s knowledge-doc bootstrap creates the article from this module's `knowledge_docs` manifest entry.
*   **Guide:** `Database Management Guide` (installed from `data/documentation.html`).

---

## 📦 External Dependencies

Only `vacuumdb` is declared in `__manifest__.py`. `patroni`, `pgbouncer` and `etcd` are needed only by the High Availability Orchestrator, which checks for them on the Odoo host before generating configuration.

---

## 🧪 Testing & Verification

The module includes an exhaustive test suite covering standard and integration scenarios:
*   **Standard Tests:** Verify model logic, view rendering, and security constraints. `[@ANCHOR: test_dba_view]`

*   **Integration Tests:** Simulate `vacuumdb` execution and HA configuration generation. `[@ANCHOR: test_dba_cron]`

*   **Security Tests:** Verify that only authorized users can access sensitive DBA tools and that standard users are isolated. `[@ANCHOR: test_db_security]`

*   **UI Tours:** Automated browser tours verify the end-to-end user journeys for bloat management and slow query analysis. `[@ANCHOR: test_db_bloat_tour]`

---

## 🔄 Semantic Anchors (Internal Reference)

*   `[@ANCHOR: db_index_stats]`: Stats collection for tables and indexes.

*   `[@ANCHOR: db_terminate_backend]`: Logic for killing active sessions.

*   `[@ANCHOR: vacuum_analyze]`: Subprocess orchestration for `vacuumdb`.

*   `[@ANCHOR: pg_optimize_wizard]`: Hardware-based tuning calculations.

*   `[@ANCHOR: pg_ha_wizard]`: HA cluster configuration generation.

*   `[@ANCHOR: db_slow_queries]`: APM tracking via `pg_stat_statements`.

*   `[@ANCHOR: bloat_alert_synergy]`: PagerDuty integration logic.

*   `[@ANCHOR: db_doc_injection]`: Documentation bootstrap verification.

### Stage 1 Anchor-Coverage Sweep Additions

The `init()` hooks below are Odoo's own `_auto=False` view-model convention: each creates a real
PostgreSQL view at module install/update time, and every one is proven live by a test that
successfully queries or renders the resulting model (a broken `init()` would make module
installation itself fail before any such test could run).

*   `[@ANCHOR: COMM_db_table_stat_init]`: Creates the `database_table_stat` view (bloat/vacuum stats).

*   `[@ANCHOR: COMM_db_table_stat_get_executable]`: Resolves the `vacuumdb` binary via the service
    account for `action_vacuum_analyze()`.

*   `[@ANCHOR: COMM_db_activity_init]`: Creates the `database_activity` view (active sessions).

*   `[@ANCHOR: COMM_db_index_stat_init]`: Creates the `database_index_stat` view.

*   `[@ANCHOR: COMM_db_replication_stat_init]`: Creates the `database_replication_stat` view.

*   `[@ANCHOR: COMM_db_index_advisor_init]`: Creates the `database_index_advisor` view.

*   `[@ANCHOR: COMM_pg_explain_wizard_close]`: Closes the `pg.explain.wizard` transient form.

*   `[@ANCHOR: COMM_db_pg_setting_init]`: Creates the `database_pg_setting` view (`pg_settings`
    audit).

*   `[@ANCHOR: COMM_pg_ha_wizard_get_executable]`: Resolves `patroni`/`pgbouncer`/`etcd` binaries
    (deferring to `binary_downloader` when missing) for the HA Failover Wizard.

*   `[@ANCHOR: COMM_pg_ha_wizard_validate_inputs]`: Validates IP addresses, replication password
    strength, and alphanumeric-only cluster/user names before generating Patroni/PgBouncer configs.
