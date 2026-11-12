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
*   **SQL Injection Prevention:** The Optimization Wizard builds its `ALTER SYSTEM` statements with the `psycopg2.sql` library (`sql.Identifier` for the parameter name, `sql.Literal` for the value). Other runtime values are passed as bound query parameters. `pg_optimize_wizard`

*   **Input Validation:** The HA wizard parses node IP addresses with Python's `ipaddress` module, requires cluster and user names to match `^[a-zA-Z0-9_]+$`, and requires a replication password of at least 8 characters. `pg_ha_wizard`

*   **Binary Safety:** Execution of external binaries (e.g., `vacuumdb`) is managed via `zero_sudo.security.utils._ensure_executable()`, which uses the binary found on `PATH` or, if there is none, asks `binary_downloader` to install it; `vacuumdb` runs without a shell and with a minimal environment. `vacuum_analyze`
*   **Access Control:** Model access rights are granted to the `database_management.group_database_management_manager` group ("Database Manager"). Odoo's `base.group_system` (Settings) implies that group, and the **Database & SRE** menu is shown only to `base.group_system`.

### Components
*   **Stat Views:** Native PostgreSQL statistics are exposed via Odoo models (`database.table.stat`, `database.index.stat`, `database.query.stat`, `database.activity`, `database.replication.stat`, `database.index.advisor`, `database.pg.setting`) using PostgreSQL views. `db_index_stats`

*   **Vacuum Automation:** Manual `VACUUM ANALYZE` is triggered via `subprocess` calling `vacuumdb`, bypassing Odoo's transaction blocks to allow physical cleanup. `vacuum_analyze`

*   **Configuration Management:** The Optimization Wizard `pg_optimize_wizard` writes to `postgresql.auto.conf` and reloads the configuration.

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
*   **Standard Tests:** Verify model logic, view rendering, and security constraints. `test_dba_view`

*   **Integration Tests:** Simulate `vacuumdb` execution and HA configuration generation. `test_dba_cron`

*   **Security Tests:** Verify that only authorized users can access sensitive DBA tools and that standard users are isolated. `test_db_security`

*   **UI Tours:** Automated browser tours verify the end-to-end user journeys for bloat management and slow query analysis. `test_db_bloat_tour`

---

## 🔄 Semantic Anchors (Internal Reference)

*   `db_index_stats`: Stats collection for tables and indexes.

*   `db_terminate_backend`: Logic for killing active sessions.

*   `vacuum_analyze`: Subprocess orchestration for `vacuumdb`.

*   `pg_optimize_wizard`: Hardware-based tuning calculations.

*   `pg_ha_wizard`: HA cluster configuration generation.

*   `db_slow_queries`: APM tracking via `pg_stat_statements`.

*   `bloat_alert_synergy`: PagerDuty integration logic.

*   `db_doc_injection`: Documentation bootstrap verification.

### Stage 1 Anchor-Coverage Sweep Additions

The `init()` hooks below are Odoo's own `_auto=False` view-model convention: each creates a real
PostgreSQL view at module install/update time, and every one is proven live by a test that
successfully queries or renders the resulting model (a broken `init()` would make module
installation itself fail before any such test could run).

*   `COMM_db_table_stat_init`: Creates the `database_table_stat` view (bloat/vacuum stats).

*   `COMM_db_table_stat_get_executable`: Resolves the `vacuumdb` binary via the service
    account for `action_vacuum_analyze()`.

*   `COMM_db_activity_init`: Creates the `database_activity` view (active sessions).

*   `COMM_db_index_stat_init`: Creates the `database_index_stat` view.

*   `COMM_db_replication_stat_init`: Creates the `database_replication_stat` view.

*   `COMM_db_index_advisor_init`: Creates the `database_index_advisor` view.

*   `COMM_pg_explain_wizard_close`: Closes the `pg.explain.wizard` transient form.

*   `COMM_db_pg_setting_init`: Creates the `database_pg_setting` view (`pg_settings`
    audit).

*   `COMM_pg_ha_wizard_get_executable`: Resolves `patroni`/`pgbouncer`/`etcd` binaries
    (deferring to `binary_downloader` when missing) for the HA Failover Wizard.

*   `COMM_pg_ha_wizard_validate_inputs`: Validates IP addresses, replication password
    strength, and alphanumeric-only cluster/user names before generating Patroni/PgBouncer configs.

## Anchor reference

One entry per anchor named in this document, read from the module code (`models/db_stats.py`, `models/pg_config.py`, `data/`, `views/`, `tests/`). Entries share their text with the other documents of the module, so the same anchor is described the same way everywhere.

### `pg_optimize_wizard` [@ANCHOR: pg_optimize_wizard]

`PgOptimizeWizard.action_apply_optimizations(self)` (`@public_rpc`) on the transient model `pg.optimize.wizard`, which computes PostgreSQL tuning values from the hardware and writes them with `ALTER SYSTEM`.

| Field | Type | Meaning |
|---|---|---|
| `ram_gb` | Integer | Server RAM in GB, default 8, must be above 0. |
| `cpu_cores` | Integer | Cores, default 4, above 0. |
| `storage_type` | Selection | `ssd` (default) or `hdd`. |
| `max_connections` | Integer | Default 200, above 0. |

**Formulas:** `shared_buffers` 25% of RAM; `effective_cache_size` 75%; `maintenance_work_mem` 5% capped at 1024 MB; `work_mem` = 25% of RAM / `max_connections`, at least 4 MB; `random_page_cost` 1.1 (ssd) or 4.0; `max_worker_processes` and `max_parallel_workers` = cores; `max_parallel_workers_per_gather` = max(2, cores/2). **Behaviour:** values go through `psycopg2.sql` (`Identifier` and `Literal`) on a separate registry cursor, followed by `pg_reload_conf()`. **Returns** a sticky warning notification that `shared_buffers` and `max_connections` need a PostgreSQL restart. **Errors:** `UserError` for a non-positive input. Writes `postgresql.auto.conf`, a persistent server-wide change; the database user must be a superuser. Tests: `test_01_optimization_wizard`, `test_01b_optimization_wizard_errors`.

In the code this anchor is spelled `COMM_pg_optimize_wizard`.

### `pg_ha_wizard` [@ANCHOR: pg_ha_wizard]

`PgHaWizard.action_generate(self)` (`@public_rpc`) on the transient model `pg.ha.wizard`, the High Availability orchestrator. It only produces text for the operator to copy; it deploys nothing.

| Field | Type | Meaning |
|---|---|---|
| `cluster_name` | Char | Patroni scope, default `hams_cluster`. |
| `primary_ip`, `secondary_ip` | Char | Node addresses, defaults 10.0.0.1 and 10.0.0.2. |
| `etcd_hosts` | Char | Comma-separated `host:port`, default `etcd:2379`. |
| `replication_user`, `superuser_user` | Char | Defaults `replicator`, `postgres`. |
| `replication_pass` | Char | Default `secrets.token_urlsafe(24)`; also used for the superuser entry. |
| `state` | Selection | `input` then `generated`. |
| `patroni_primary`, `patroni_secondary`, `pgbouncer_ini` | Text | Outputs, read-only. |

**Steps:** `_validate_inputs()` (`COMM_pg_ha_wizard_validate_inputs`), then `_get_executable` for `patroni`, `pgbouncer` and `etcd`, then it fills the three outputs (node1 includes an `initdb` bootstrap section; PgBouncer listens on 6432 in transaction pool mode) and sets `state = generated`. **Returns** an `ir.actions.act_window` reopening the wizard. **Errors:** `UserError` from validation or a missing binary. The outputs contain the replication password in clear text, so they are shown only to Database Managers. Tests: `test_02_ha_wizard`, `test_02a_ha_wizard_password_not_predictable`, the `test_tdd_pg_config_yaml_injection*` set.

In the code this anchor is spelled `COMM_pg_ha_wizard`.

### `vacuum_analyze` [@ANCHOR: vacuum_analyze]

`DatabaseTableStat.action_vacuum_analyze(self)` (`@public_rpc`), the "Vacuum Analyze" button on the table-statistics list. It runs `vacuumdb -z -t <table> ... <dbname>` once for all selected rows that have a `table_name`, in a subprocess so the vacuum is outside the ORM transaction (a vacuum cannot run inside one).

| Input | Detail |
|---|---|
| `self` | One or more `database.table.stat` records; rows without a table name are ignored. |
| environment | Built minimally (ADR 0044): `PATH`, `PGPORT` (default 5432), `PGUSER` (default `odoo`) and `PGHOST` only if set. No password is passed: with `PGHOST` unset `vacuumdb` uses the local socket and peer authentication. |

**Returns:** `True` (also when nothing was selected). **Errors:** `UserError` with the stderr text when `vacuumdb` exits non-zero, "timed out" after 600 seconds, or on `OSError`. **Safety:** `shell=False`, table names go as separate argv items, the binary comes from `COMM_db_table_stat_get_executable`. Blocking for up to ten minutes, so it holds a web worker. Idempotent and safe to repeat. Tests: `test_01_vacuum_analyze`, `test_01a_vacuum_uses_the_local_socket_and_never_a_password`, `test_01b_vacuum_analyze_failures`.

In the code this anchor is spelled `COMM_vacuum_analyze`.

### `db_index_stats` [@ANCHOR: db_index_stats]

The model `database.index.stat`, the index-health list, backed by the view of `COMM_db_index_stat_init`; global and read-only. Default order `idx_scan asc`, so unused indexes come first.

| Field | Type | Meaning |
|---|---|---|
| `table_name` | Char | Table the index belongs to. |
| `index_name` | Char | Index name. |
| `idx_scan` | Integer | Number of scans that used it since statistics were last reset. |
| `index_size_kb` | Float | Size in kilobytes. |

Unique indexes are excluded because they enforce constraints and cannot simply be dropped. Access: Database Manager group, read only. Tests: `test_04_views` and `test_03_db_index_stats` (which in fact exercises `cron_check_bloat`; to-do: point it at this model).

In the code this anchor is spelled `COMM_db_index_stats`.

### `test_dba_view` [@ANCHOR: test_dba_view]

Test id for the statistics views. `test_04_views` in `tests/test_db_management.py` calls `get_view(view_type="list")` on `database.table.stat`, `database.query.stat`, `database.activity`, `database.index.stat`, `database.replication.stat` and `database.index.advisor`, and `get_view(view_type="form")` on `pg.explain.wizard`, asserting that each compiled arch contains a known field. That proves the views in `views/db_stats_views.xml` compile against the models; they cite this anchor as their audit-ignore reason. It does not read rows. The model `database.table.stat` (`COMM_db_security_prefetch`) cites it too. Failure means a view or a field name changed.

In the code this anchor is spelled `COMM_test_dba_view`.

### `test_dba_cron` [@ANCHOR: test_dba_cron]

Test id for the scheduled bloat check and the stat models. `test_02_bloat_cron` patches `_get_service_env` with a mock and triggers the cron record `database_management.cron_check_bloat` (job "DBA: Check Autovacuum Bloat", model `database.table.stat`, run as `user_database_management_service`, daily, code `model.cron_check_bloat()`, with the manager group named on the job so Odoo allows the action). The same anchor is cited by `data/cron.xml` as the audit-ignore justification and on the replication and index-advisor models. Failure means the cron record or `cron_check_bloat` no longer runs. See `COMM_bloat_alert_synergy`.

In the code this anchor is spelled `COMM_test_dba_cron`.

### `test_db_security` [@ANCHOR: test_db_security]

Test id for access control. `tests/test_db_security.py` creates standard, website, ham, SWL and public personas and asserts that each gets `AccessError` reading `database.table.stat` and `database.pg.setting` or creating `pg.optimize.wizard` and `pg.ha.wizard` records (`test_01_security`); that every access row of the seven view-backed models has write, create and unlink switched off (`test_02_view_models_readonly`); and that the administrator can read the views and create the wizards (`test_03_admin_success`). This id has no separate tag in the code; the test method cites `COMM_db_security_prefetch`. Unknown whether a test method should carry this id directly (to-do).

In the code this anchor is spelled `COMM_test_db_security`.

### `test_db_bloat_tour` [@ANCHOR: test_db_bloat_tour]

Test id for the browser tour `db_management_bloat_tour` (`static/src/tours/db_bloat_tour.js`), started by `test_01_db_bloat_tour` in `tests/test_tours.py`. The tour opens the Apps menu, picks "Database & SRE", waits for the table-statistics list, selects the first row, clicks "Vacuum Analyze Selected" and waits for it to finish. It needs a browser and a populated `pg_stat_user_tables`, so it is slow. A sibling tour, `db_management_slow_query_tour`, is started by `test_02_db_slow_query_tour` (anchor `COMM_test_db_slow_query_tour`). Failure means the menu, the list or the vacuum button changed.

In the code this anchor is spelled `COMM_test_db_bloat_tour`.

### `db_terminate_backend` [@ANCHOR: db_terminate_backend]

`DatabaseActivity.action_terminate_backend(self)` (`@public_rpc`), the "Terminate Session" button. It collects the `pid` of every selected row, switches to the `user_database_management_service` environment and runs one statement, `SELECT pg_terminate_backend(pid) FROM unnest(%s) AS pid`, with the pid list as a bound array (one round trip for any number of sessions). **Returns** `True`, also when the list is empty. Errors from the database propagate. The service account is a PostgreSQL role with enough privilege to signal the target backends; a pid that no longer exists or belongs to a superuser is simply not terminated (`pg_terminate_backend` returns false and the result is not inspected). A terminated client loses its transaction. Not undoable. Tests: `test_03_terminate_backend`.

In the code this anchor is spelled `COMM_db_terminate_backend`.

### `db_slow_queries` [@ANCHOR: db_slow_queries]

The model `database.query.stat`, the slow-query list (APM). Read-only, backed by the view of `COMM_db_query_stat_init` over `pg_stat_statements` for the current database; global across companies. Default order `total_time desc`.

| Field | Type | Meaning |
|---|---|---|
| `query` | Text | Normalised SQL text. |
| `calls` | Integer | Execution count. |
| `total_time` | Float | Total execution time, milliseconds (`total_exec_time`). |
| `mean_time` | Float | Mean execution time, milliseconds. |

Without the extension the view holds one placeholder row whose `query` explains the missing setup. **Buttons:** `action_install_extension` (`COMM_db_install_extension`), `action_reset_stats` (`COMM_db_reset_stats`) and `action_explain_query` (`COMM_db_explain_query`). Access: Database Manager group, read only. Tests: `test_04_views`, `test_06_query_stats_ops`.

In the code this anchor is spelled `COMM_db_slow_queries`.

### `bloat_alert_synergy` [@ANCHOR: bloat_alert_synergy]

`DatabaseTableStat.cron_check_bloat(self)` (`@api.private`, `@api.model`), run once a day by the cron "DBA: Check Autovacuum Bloat" as the service account. It searches `database.table.stat` for tables with `dead_percent > 20` and `dead_tuples > 10000` (at most 1000 rows). If any exist it opens one incident through `pager.incident.report_incident` (the project's own `pager_duty` module) with source "DBA Autovacuum Monitor", severity `medium` and a description listing each table with its dead percentage and asking for a manual Vacuum Analyze. **Returns** nothing. **Errors:** `AccessError` and `UserError` from the pager service are logged as warnings and swallowed, so a misconfigured pager never fails the cron. Elevation uses `_get_service_env("pager_duty.user_pager_service_internal")`. Repeated runs open a new incident each day while the bloat persists. Tests: `test_02b_bloat_alert_synergy` and `COMM_test_dba_cron`.

In the code this anchor is spelled `COMM_bloat_alert_synergy`.

### `db_doc_injection` [@ANCHOR: db_doc_injection]

Documentation bootstrap. `__manifest__.py` lists `knowledge_docs` with one entry, "Database Management Guide" (`data/documentation.html`, icon, category `workspace`). When the Knowledge module is installed, `zero_sudo`'s knowledge-doc bootstrap reads that key and creates or updates the article, so this module has no code of its own for it and uses no elevated privilege. The HTML file itself carries the other anchors of the module as comments, one section per feature (bloat alerts, vacuum, index advisor, index stats, slow queries and explain, active sessions, replication, tuning, high availability). Test: `test_08_doc_injection`, which only checks that the file opens (to-do: assert the article exists once Knowledge is present).

In the code this anchor is spelled `COMM_db_doc_injection`.

### `COMM_db_table_stat_init` [@ANCHOR: COMM_db_table_stat_init]

`DatabaseTableStat.init(self)`, called by Odoo on module install and upgrade. It drops any existing view named `database_table_stat` (`drop_view_if_exists`) and runs `CREATE OR REPLACE VIEW` over `pg_stat_user_tables` joined to `pg_statio_user_tables` on `relid`. The view supplies `id` (`row_number() OVER ()`, so an id is not stable between refreshes), the constant name `'SQL View'`, and the columns of `COMM_db_security_prefetch` with the dead-percent and cache-hit divisions guarded against zero. Takes no arguments, returns nothing, and raises a database error if the catalog views are not readable. It runs in the install transaction, takes a brief lock for the view replacement and has no other side effects. Test: `test_04_views` (`COMM_test_dba_view`).

### `COMM_db_table_stat_get_executable` [@ANCHOR: COMM_db_table_stat_get_executable]

`DatabaseTableStat._get_executable(self, cmd_name)` resolves the full path of a command-line tool for the vacuum action. It calls `zero_sudo.security.utils._ensure_executable(cmd_name, svc_xml_id="database_management.user_database_management_service", pkg_name=...)`. The package name is `postgresql-client` when `cmd_name` is `vacuumdb`, otherwise the command name itself. The helper uses the binary found on `PATH` or, if none, asks `binary_downloader` to install it as the module's service account. Returns the path as a string; errors from the helper (the binary cannot be found or installed) propagate to the caller. No `.sudo()` is used. Test: `test_01_vacuum_analyze` patches it.

### `COMM_db_activity_init` [@ANCHOR: COMM_db_activity_init]

`DatabaseActivity.init(self)`. Drops and recreates the view `database_activity` over `pg_stat_activity`, keeping rows with `datname = current_database()` and `pid <> pg_backend_pid()`, so the viewer never lists (or terminates) its own connection. Columns: constant name `'SQL View'`, `id` and `pid` both equal to the process id, `usename`, `state`, `query`, and `duration` as `EXTRACT(EPOCH FROM now() - query_start)` seconds (null when there is no start time). No arguments, no return value; runs at install and upgrade. Note that PostgreSQL shows query text only for sessions the Odoo database user may see. Test: `test_04_views`.

### `COMM_db_index_stat_init` [@ANCHOR: COMM_db_index_stat_init]

`DatabaseIndexStat.init(self)`. Drops and recreates the view `database_index_stat` from `pg_stat_user_indexes` joined with `pg_index` on `indexrelid`, keeping only rows with `indisunique IS FALSE`. Columns: `id` (`row_number() OVER ()`), constant name `'SQL View'`, `table_name` (`relname`), `index_name` (`indexrelname`), `idx_scan`, and `index_size_kb` (`pg_relation_size(indexrelid) / 1024.0`). No arguments and no return value; it runs on install and upgrade and only reads catalog statistics. Test: `test_04_views`.

### `COMM_db_replication_stat_init` [@ANCHOR: COMM_db_replication_stat_init]

`DatabaseReplicationStat.init(self)`. Drops and recreates the view `database_replication_stat` selecting from `pg_stat_replication`: `id` from `row_number() OVER ()`, constant name `'SQL View'`, user, application, client address, state, the four LSN columns and three lag columns cast to `text` (so the Odoo model can hold them in Char fields), `sync_priority` and `sync_state`. No arguments, no return value, read-only on the catalog; runs at install and upgrade. Test: `test_04_views`.

### `COMM_db_index_advisor_init` [@ANCHOR: COMM_db_index_advisor_init]

`DatabaseIndexAdvisor.init(self)`. Drops and recreates the view `database_index_advisor` over `pg_stat_user_tables` with `id` set to `relid`, `table_name`, `seq_scan`, `seq_tup_read`, `idx_scan` and `table_size_mb` (`pg_total_relation_size(relid)` in MB), filtered by `seq_scan > 100 AND pg_total_relation_size(relid) > 10 * 1024 * 1024`. The thresholds are fixed in the SQL and not configurable. No arguments and no return value; runs on install and upgrade. Test: `test_04_views`.

### `COMM_pg_explain_wizard_close` [@ANCHOR: COMM_pg_explain_wizard_close]

`PgExplainWizard.action_close(self)` (`@public_rpc`) on the transient model `pg.explain.wizard`, which shows a query and its plan (read-only fields `query` and `explain_plan`, both Text, created by `COMM_db_explain_query`). The method takes no arguments and returns `{"type": "ir.actions.act_window_close"}`, which closes the dialog and returns the user to the list. No side effects; the wizard record is discarded later by Odoo's transient-model vacuum. Access rows give the Database Manager group full rights on the wizard. Test: `test_07b_explain_wizard_close`.

### `COMM_db_pg_setting_init` [@ANCHOR: COMM_db_pg_setting_init]

`DatabasePgSetting.init(self)`. Drops and recreates the view `database_pg_setting` selecting `name`, `setting`, `unit`, `category`, `short_desc`, `context` and `pending_restart` from `pg_settings`, with `id` from `row_number() OVER (ORDER BY name)`. No arguments, no return value, only reads the catalog; runs at install and upgrade. Test: `test_03_views` in `tests/test_pg_config.py`.

### `COMM_pg_ha_wizard_get_executable` [@ANCHOR: COMM_pg_ha_wizard_get_executable]

`PgHaWizard._get_executable(self, cmd_name)` resolves the full path of `patroni`, `pgbouncer` or `etcd` for the High Availability wizard. It calls `zero_sudo.security.utils._ensure_executable(cmd_name, svc_xml_id="database_management.user_database_management_service", pkg_name=...)` where the package name is the command name. A binary found on `PATH` is used; otherwise `binary_downloader` is asked to install it, as the service account (the test `test_02c_etcd_auto_download` covers etcd). Returns the path string. Errors from the helper propagate, so generating the configuration fails when a tool is neither present nor installable. The wizard only checks presence; it does not run these tools. Tests: `test_02c_etcd_auto_download`, `test_02b_ha_wizard_missing_binaries`.

### `COMM_pg_ha_wizard_validate_inputs` [@ANCHOR: COMM_pg_ha_wizard_validate_inputs]

`PgHaWizard._validate_inputs(self)`, called first by `COMM_pg_ha_wizard`. It raises `UserError` unless: the primary and secondary IPs parse with `ipaddress.ip_address`; the replication password has at least 8 characters and is free of YAML-breaking content (control characters, `:` followed by whitespace or the end, whitespace followed by `#`, trailing whitespace, or a leading character from `` `@%&*!|>'",[{]}# ``); `cluster_name`, `superuser_user` and `replication_user` match `^[a-zA-Z0-9_]+\Z` (so a trailing newline is refused); and `etcd_hosts` is comma-separated `host:port` pairs matching `^[a-zA-Z0-9_.-]+:[0-9]+(,...)*\Z`. The checks exist because these values are put unescaped into generated YAML. Returns nothing; no side effects. Tests: `test_02d_ha_wizard_validation_errors`, `test_tdd_pg_config_*` including the trailing-newline and password cases.
