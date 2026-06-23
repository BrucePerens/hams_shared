# Database Management (`database_management`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

The `database_management` module provides a comprehensive suite of Database Administration (DBA) and Application Performance Monitoring (APM) tools directly within the Odoo interface. It is designed to empower Site Reliability Engineers (SREs) and administrators with the ability to monitor, tune, and scale the PostgreSQL database without requiring shell access.

---

## 🚀 Key Features

*   **Stat Tracking:** Real-time visibility into table bloat, index usage, and cache hit ratios.
*   **Slow Query Monitoring (APM):** Identifies the most resource-intensive SQL queries using `pg_stat_statements`.
*   **Active Session Management:** View and terminate runaway database sessions.
*   **Performance Tuning Wizard:** Automatically calculates optimal PostgreSQL parameters based on hardware specifications and applies them via `ALTER SYSTEM`.
*   **High Availability Orchestrator:** Generates production-ready configurations for Patroni, etcd, and PgBouncer clusters.
*   **Automated Alerts:** Integrates with PagerDuty to notify SREs when table bloat exceeds critical thresholds.
*   **Zero-Sudo Architecture:** Ensures all operations are performed with minimum necessary privileges using dedicated service accounts.

---

## 🛠 Architecture & Security

### Micro-Privilege Architecture
This module strictly adheres to a Zero-Sudo policy. Sensitive operations are delegated to the `user_database_management_service` service account. Privilege elevation is handled via `_get_service_env()` from the `zero_sudo` module, ensuring that no `sudo()` calls are used in the codebase.

### Security Hardening
*   **SQL Injection Prevention:** All raw SQL queries utilize the `psycopg2.sql` library for AST-compliant parameterization. `[@ANCHOR: pg_optimize_wizard]`
*   **Input Validation:** Strict regex validation for IP addresses and complexity requirements for replication passwords. `[@ANCHOR: pg_ha_wizard]`
*   **Binary Safety:** Execution of external binaries (e.g., `vacuumdb`) is restricted to authorized paths and managed via `zero_sudo.security.utils`. `[@ANCHOR: vacuum_analyze]`
*   **Access Control:** All DBA functionality is restricted to the `base.group_system` role, with additional granular privileges defined in `res.groups.privilege`. Managers have the `database_management.group_database_management_manager` group.

### Components
*   **Stat Views:** Native PostgreSQL statistics are exposed via Odoo models (`database.table.stat`, `database.index.stat`, `database.query.stat`, `database.activity`) using PostgreSQL views. `[@ANCHOR: db_index_stats]`
*   **Vacuum Automation:** Manual `VACUUM ANALYZE` is triggered via `subprocess` calling `vacuumdb`, bypassing Odoo's transaction blocks to allow physical cleanup. `[@ANCHOR: vacuum_analyze]`
*   **Configuration Management:** The Optimization Wizard `[@ANCHOR: pg_optimize_wizard]` writes to `postgresql.auto.conf` and reloads the configuration.

---

## 📚 Documentation & Help

User-facing documentation is available directly within the Odoo Knowledge or Knowledge modules.
*   **Guide:** `Database Management Guide` (installed from `data/documentation.html`).

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
