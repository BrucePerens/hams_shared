# 📟 Pager Duty & Generalized Monitoring (`pager_duty`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

[@ANCHOR: pager_duty_module_root]

The Pager Duty module is an enterprise-grade Site Reliability Engineering (SRE) suite designed to keep your Odoo infrastructure running smoothly. It provides active monitoring, intelligent alerting, and automated incident management.

## 🌟 What It Does

*   **Active System Monitoring:** Continuously checks the health of web workers, background daemons, databases, network connections, and hardware.
*   **Smart Alerting:** Routes alerts to the right person based on Odoo Calendar schedules, preventing alert fatigue: only High and Critical incidents page the on-call person immediately. Low and Medium incidents are recorded without paging, and page only when the same source repeats often enough to form a trend (see `report_incident` below).
*   **Automated Escalation:** Escalates unacknowledged incidents to the Pager Duty Admin group.
*   **Incident Analytics:** Tracks Mean Time to Acknowledge (MTTA) and Mean Time to Resolve (MTTR).
*   **Helpdesk Integration:** Automatically creates a ticket in the Helpdesk module for every new High or Critical incident, and for every incident created from inbound email.
*   **Multi-Website Support:** Partition monitoring checks and incidents by website to support multi-tenant Odoo deployments. This includes record-level security rules and optimized indices for strict data isolation.

## 🛠️ How to Set It Up

1.  **Dependencies:** Ensure `redis`, `psutil`, `ntplib`, `pymysql`, `psycopg2`, and `ldap3` are installed in your Python environment (plus `mcp` if you run `daemon/pager_mcp_server.py`).
2.  **Installation:** Install the `pager_duty` module from the Odoo Apps menu.
3.  **Daemon Configuration:**
    *   Navigate to **Pager Duty > Monitoring Checks**.
    *   Use the **Push to Daemon (JSON)** and **Pull from Daemon (JSON)** buttons at the top of the list to synchronize the database with the daemon's `pager_config.json`. Push writes every active check to the file. Pull deletes every existing check in the database and re-creates the checks from the file.
    *   Deploy and start the Python daemons located in the `daemon/` directory (see `DEPLOYMENT.md` for systemd service examples). The daemons read `pager_config.json` only when they start, so restart them after a Push.

## 🚀 Key Features and Operations

### Monitoring Checks
Create diverse checks for your infrastructure:

- **HTTP/HTTPS/HTTP3:** Verify website availability and content.
- **PostgreSQL/MySQL:** Ensure database connectivity and performance.
- **System Resources:** Monitor CPU, RAM, and Disk space.
- **Service Status:** Check systemd services and Docker containers. A timer-driven `Type=oneshot` service that is inactive because its last run succeeded counts as healthy; only a failed one does not.
- **Status of a check:** a check that no monitor has ever produced a result for is **Never Run** (not "Passing"), so a monitor that was never installed cannot make the board look healthy. The monitor writes Passing or Failing and the Last Run time after every run, including its first.
- **Failures Before Reporting:** a check is reported only after it has failed that many runs in a row (default 3), and after a failed run the monitor runs it again within about 20 seconds, so a transient failure is cleared quickly and a real one is reported after about a minute. Set 1 to report the first failure.
- **Hard Drive Health:** Proactive SMART (Self-Monitoring, Analysis and Reporting Technology) monitoring.
- **Custom Scripts:** Execute sandboxed Bash or Playwright scripts for synthetic journeys. Only the Playwright, Sandboxed Bash and Sandboxed Arbitrary Executable types run in the sandbox. The "Synthetic Journey (Script)" type runs its command directly inside the monitor daemon, unsandboxed, with a 60-second timeout.

### Incident Management
- **Dashboard:** The Network Operations Center (NOC) Dashboard provides a real-time overview of active and resolved incidents. It is powered by a high-performance PostgreSQL procedure to minimize latency and includes burn-in protection for long-term display (every 60 seconds the whole board drifts by up to 15 pixels in a random direction).
- **Acknowledgement:** Engineers can acknowledge incidents to stop further escalation.
- **Auto-Resolution:** The system automatically resolves incidents when the underlying check returns to a healthy state, which the monitor daemon takes to mean three consecutive passing runs of that check.
- **Escalation:** Unacknowledged incidents are automatically escalated after 15 minutes to ensure attention. A job that runs every 5 minutes notifies the Pager Duty Admin group about every high or critical incident that is still Open 15 minutes after it was created (low and medium incidents are tracked and become a paging "Trend:" incident if they recur; they are not escalated). Each incident is escalated once.

### On-Call Scheduling
Integrates with the Odoo Calendar. Mark calendar events as "Pager Duty Shift" to define the current on-call engineer. The person paged is the event's own user (its `user_id`), not its attendees. When no shift is in force, no on-call notification is sent; the incident still gets its Helpdesk ticket (if its severity qualifies) and is still escalated.

---

### Pagerduty maintenance (planned restarts without a page)

Restarting Odoo, upgrading a module or rebooting makes the monitors see a failure, and the daemon's SMTP fallback and any
site monitor will page for it. Tell them first. A one-file command sets a flag that every pagerduty monitor on the host
reads, with no Odoo, network or database involved (it works when Odoo is down, which is the point):

```
sudo pagerduty-maintenance start --minutes 20 --reason "module upgrade"   # default 20 minutes, at most 120
sudo pagerduty-maintenance end                                            # when done; optional
pagerduty-maintenance status                                              # exit 0 if active, 1 if not
```

- The flag is the file `/etc/pagerduty/maintenance` (change it with the environment variable
  `PAGERDUTY_MAINTENANCE_FILE`). It holds `set_at`, `until`, `reason` and `set_by` as JSON.
- It **expires by itself** at `until`, and `until` is never honoured beyond **2 hours** after `set_at`, so a forgotten flag
  cannot silence paging for long. A malformed or unreadable flag counts as *not* in maintenance: paging stays on.
- **Only root can set or clear it, on purpose**: the file is root-owned, mode 0644, in a root-owned 0755 directory, so an
  unprivileged user or a compromised service account cannot silence paging. `status` needs no privilege.
- While it is active the daemon's SMTP fallback logs at warning and sends no mail, and the monitor neither counts nor reports a failing check (the row still shows `Failing`); after it ends a check that is still failing needs its normal consecutive failures again. hams_shared's `site_monitor.py`
  (the optional host-level monitor) keeps running and logging every check but sends no page and no "recovered" notice,
  and failures during maintenance do not count: after it ends, a check still failing needs the normal consecutive failures
  before it pages. The monitors only read the file (their units run with a read-only `/etc`); `start` and `status` remove an
  expired file.
- Install the command on any host that runs the daemon: `sudo install -m 0755 pager_duty/daemon/pagerduty_maintenance.py
  /usr/local/sbin/pagerduty-maintenance` (it is one stdlib-only Python 3 file) and `sudo install -d -m 0755 /etc/pagerduty`.
  `hams_shared/tools/infrastructure.py` does both on hosts it provisions.
- From a deploy script, around anything that restarts a service:

```
sudo pagerduty-maintenance start --minutes 30 --reason "apt upgrade"
sudo apt-get -y upgrade        # or: docker compose up -d   /   odoo -u my_module ... && systemctl restart odoo
                               # (hand-run odoo -u: use distributed_redis_cache/scripts/odoo_upgrade.py -- it gives the upgrade the service's Redis credentials)
sudo pagerduty-maintenance end
```

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for Software Engineers, SREs, and Integrators.
</system_role>

## 1. Architecture Overview (CQRS)
The module follows a **Command Query Responsibility Segregation (CQRS)** pattern. Odoo serves as the configuration and reporting plane, while standalone Python daemons handle the high-frequency execution loops.

### Key Components:
*   **Control Plane:** Odoo records (`pager.check`) define what to monitor. [@ANCHOR: generalized_pager_config]
*   **Data Plane (Daemons):**
    *   `generalized_monitor.py`: Executes standard checks (HTTP, TCP, SQL, etc.) via a micro-privilege service account. It is designed to "fail fast" if system dependencies are missing. [@ANCHOR: daemon_execute_check]
        A missing binary is first requested from Odoo through `rpc_ensure_executable` (12 attempts, 10 seconds apart); if it still cannot be provisioned, the daemon reports a critical "Daemon Boot" incident and exits.

    *   `pager_log_analyzer.py`: Tails system logs for regex matches in real-time. It runs chrooted to `/var/log`, drops all kernel capabilities, and de-escalates to `nobody:adm`. [@ANCHOR: COMM_pd_log_api_i18n]
    *   `pager_smart_spooler.py`: Securely collects hardware health data (SMART).
    *   `pager_synthetic_spooler.py`: Executes sandboxed (Bubblewrap) Playwright/Bash tests. [@ANCHOR: synthetic_i18n]
    *   The two "spoolers" are named for how they hand results over: they run as root (reading SMART data and creating sandboxes need it), write their results to a JSON spool file under `/var/log` (`pager_smart_spool.json`, `pager_synthetic_spool.json`), and `generalized_monitor.py`, which runs as the unprivileged `odoo` user, reads that file. A missing or stale spool file makes the corresponding check fail.
*   **Inter-Process Communication (IPC):** Uses Redis Pub/Sub and Queues for high-speed communication between Odoo workers and background daemons.

### Security & Micro-Privileges:
*   **Zero-Sudo RPC (Remote Procedure Call):** Daemons authenticate via the `pager_service_internal` service account. No `sudo()` is used. All operations utilize `with_user()` for minimum privilege execution. High-privilege RPCs are protected by allow-lists. [@ANCHOR: rpc_ensure_executable_security]

*   **Config Isolation:** The location of the daemon configuration file is managed through system parameters, isolated by service accounts. [@ANCHOR: generalized_pager_config_path]
    Odoo writes `pager_config.json` into the directory named by the `pager_duty.config_dir` system parameter (default `/opt/hams/etc`) when that directory exists and is writable, and otherwise into `daemon/`. `generalized_monitor.py` looks for it in the order `PAGER_CONFIG_PATH`, then `PAGER_CONFIG_DIR` (default `/opt/hams/etc`), then `daemon/`. `pager_log_analyzer.py` and `pager_synthetic_spooler.py` read only the copy in `daemon/`.
*   **Sandboxing:** Synthetic checks run inside a strict **Bubblewrap (bwrap)** sandbox with optional network isolation. The default "Loop-back network only" setting starts the sandbox in its own network namespace (`--unshare-net`), cut off from the host's network. "Full network access" omits that flag, so the script can reach the internet and LAN.
*   **Service Accounts:** The module uses `zero_sudo.security.utils` to securely escalate privileges within Odoo's ACL framework.
*   **Multi-Website Isolation:** Data is partitioned by `website_id`. The NOC Dashboard, incident reporting, and on-duty scheduling all respect `website_id` for strict multi-tenant isolation.

---

## 2. Developer API & Integration

This section documents all public functions, models, and controllers in the `pager_duty` module and how developers can utilize them.

### `pager.check` (Models)
The core model defining monitoring checks.
*   `rpc_ensure_executable(self, cmd_name)`: Provisions a monitoring binary that the daemon host is missing, restricted to a strict security allow-list (`dig`, `curl`, `systemctl`, and similar). `generalized_monitor.py` calls it at startup for each binary its checks need; a new integration that needs a new binary must be added to that allow-list. For an allow-listed name it asks the `binary_downloader` module (if installed) to provision the binary and returns `{"status": "ok", "path": ...}`; otherwise it returns `{"status": "error", "message": ...}`. Only system administrators, Pager Duty Admins and the Pager Duty service account group may call it (others get an `AccessError`).
*   Freshness checks (`check_type = freshness`, evaluated by the monitor daemon's `check_freshness()`, [@ANCHOR: pager_duty:check_freshness]): fail when the newest record of a feed, or the last successful run of a timer-driven unit, is older than `max_age_seconds`. A liveness check (systemd `is-active`) passes for a daemon that is running but wedged; this one does not. The `target` names the source: `redis-zset:<key>` (newest score of a sorted set; an empty set fails), `redis-key:<key>` (a stored epoch or ISO time), `file:<path>` (modification time), `unit:<name>.service` (end of the last successful run of a Type=oneshot unit, or several units separated by commas that share the limit; a run in progress longer than the limit fails, a last run that did not succeed fails, and a unit that is not Type=oneshot is refused: use a data source for a daemon). The message names the source, the age and the limit. The check uses the monitor's `REDIS_*` environment, so it needs no credentials of its own, and it takes part in the normal maintenance and recovery handling: it is not counted while `pagerduty-maintenance` is active and resolves by itself once data flows again.
*   `check_heartbeat_rpc(self, hb_uuid, interval_sec)`: Reports whether the heartbeat check with this UUID received a heartbeat within the last `interval_sec` seconds (returns `True`/`False`). It does not record a heartbeat; the `heartbeat` HTTP endpoint below does that.
*   `action_pull_from_json(self)`: Syncs checks from `pager_config.json` into the database. Often mapped to a UI button. It first deletes every existing check (and, when the file has a `log_analyzer` section, every log file and log pattern) and then re-creates them from the file.
*   `action_push_to_json(self)`: Exports database checks to `pager_config.json` for daemon consumption. Only active checks are written; the file is given mode 0600 because it holds check passwords in plain text.
*   `action_autodiscover(self)`: Scans the system to automatically generate recommended monitoring checks for web services, databases, etc. Checks whose name already exists are skipped, and the JSON file is pushed afterwards.
*   `action_trigger_check(self)`: Does not run the check. It only shows a notice that the external daemon runs checks on its own polling schedule.
*   `update_lets_encrypt_domains(self, domains)`: Sets the target of the first `certbot` (Certbot Readiness) check to the comma-joined `domains`, creating that check if none exists, then pushes the JSON file.

### On-Call Scheduling (`calendar.event` extension)
*   `get_current_on_duty_admin(self)`: Retrieves the `res.users` record of the currently active responder based on calendar shifts. It returns the `user_id` of a shift (an event with "Is Pager Duty Shift" set) whose start/stop covers the current time, or `False` when no shift is in force.
    ```python
    on_duty_user = self.env["calendar.event"].get_current_on_duty_admin()
    ```
    When shifts overlap (a hand-over overlap is deliberate, and nothing forbids a double
    booking), the **most recently created** shift is the one that gets paged -- adding a new
    shift is how you override an existing one. A platform-wide shift (no website) and a
    website's own shift are ranked on that same one axis, with no precedence for either.
    [@ANCHOR: test_pager_notification]

### `pager.incident` (Models)
Handles the lifecycle of monitoring alerts.
*   `report_incident(self, vals)`: Programmatically reports a new incident. Automatically handles deduplication and rate-limiting.
    ```python
    self.env["pager.incident"].report_incident({
        "source": "Custom Script",
        "severity": "high",
        "description": "Critical failure detected"
    })
    ```
    An incident with no explicit `name` is named after its `source` (the check's name); nothing is called `INC-AUTO`.
    A check that names failing systemd units adds `"units": ["example.sync.service"]` (the systemd check and a
    `unit:` freshness check do, `generalized_monitor.CHECK_UNITS`). For a high or critical incident the units are
    reported to the same fingerprints the OnFailure exception report uses (`hams_helpdesk`
    `_report_unit_failures`), so **one failing unit is one ticket** ("Server exception: Daemon failure: <unit>") no
    matter how many checks and exception reports see it. The ticket carries an occurrence count, first and last seen
    and the list of reporters; every repeat counts, at most one ticket write a minute per unit, and a new reporter is
    named at once. The incident links to that ticket and the adapter opens no `[PAGER]` ticket for it. A closed
    or resolved ticket is reopened to New, with a note, when its unit fails again (always), when an exception recurs after a release or after 24 hours quiet (`QUIET_REOPEN`); the reopen pages the assignee like a new ticket, under the same 30 minute notify window (inside the window of an earlier page it is a plain note). If the ticket cannot be
    opened (the hourly new-ticket cap) the adapter still opens its own, so a page is never lost. An instance of a
    template unit (`name@x.service`) is not joined.
    [@ANCHOR: report_incident_rate_limit]
    The two mechanisms work as follows. Rate limit: Redis admits one report per `source` per website every 60 seconds; a report inside that window is dropped and the call returns `False`. Deduplication: if an Open or Acknowledged incident with the same `source` already exists, no new incident is created; its occurrence counters are increased and its id is returned. Otherwise a new incident is created and its id is returned.
    Paging depends on severity. A new `high` or `critical` incident notifies the on-call user at once through the incident's chatter, unless the `pager_duty.helpdesk_model` system parameter is set, in which case that notification is suppressed and the Helpdesk ticket is the page. A `low` or `medium` incident does not page; instead, when that incident's occurrence count within its current 60-minute window reaches 5, a separate `high` incident with source `Trend: <source>` is raised, and that one pages.
*   `action_escalate_unacknowledged(self)`: Checks all unacknowledged incidents and escalates them to administrators if the 15-minute SLA is breached. Automatically invoked by cron (every 5 minutes). It covers high and critical incidents still in the Open state (low and medium ones are tracked for a trend instead, never escalated), posts the escalation to the Pager Duty Admin group members for the incident's website (or to global admins), and marks each incident as escalated so it is escalated only once.
*   `auto_resolve_incidents(self, source, website_id=None)`: Resolves any active incidents matching the provided source. Call this when a system returns to a healthy state. `generalized_monitor.py` calls it after a check passes three times in a row, using the check name as the source.
*   `action_acknowledge(self)`: Acknowledges the current incident, halting its escalation timer.
*   `get_board_data(self)`: Generates real-time, aggregated JSON metrics for the NOC Dashboard UI.

### `pager.incident` Helpdesk adapter (`models/incident_ticket_adapter.py`, an extension of `pager.incident`, not a separate model)
*   `action_generate_helpdesk_ticket(self)`: Converts an existing Pager Duty incident into a Helpdesk ticket, assigning the active on-call responder. Used in automated flows (no button in this module's views calls it).
    [@ANCHOR: COMM_pd_helpdesk_adapter]
    The incident itself remains; the ticket is created in the model named by the `pager_duty.helpdesk_model` system parameter (default `hams_helpdesk.ticket`) and linked back to the incident, and incidents that already have a ticket are skipped. When there is an on-call assignee, a one-hour "Incident Response" calendar event is also created for them. If the ticket model is not installed, a fallback page addressed to the on-call user is posted on the incident's chatter instead. It is called automatically when a `high` or `critical` incident is created, and for every incident created from inbound email. Inbound-email incidents that look like spam or phishing get a ticket in the `spam` stage (when the ticket model has one) and no calendar event.

### `pager.log.search.job` (Models)
*   `rpc_update_state(self, uuid, state, result_payload)`: Updates the async task status of a real-time log search query. `pager_log_analyzer.py` returns its results through a Redis queue, and `generalized_monitor.py` relays them to Odoo by calling this method. Only the `pager_service_internal` service account or an administrator may call it.

### Controllers
Provides HTTP endpoints for daemon-to-Odoo communication.
*   `pager_board(**kw)`: `/pager/board`. Redirects legacy links to the NOC Dashboard client action in the Odoo backend; it does not render HTML itself.
*   `update_domains(domains=None, api_identity=None, **kwargs)`: Receives domain updates for automated SSL tracking (`/api/v1/pager_duty/update_domains`, passed to `update_lets_encrypt_domains`). It is public but requires `api_identity` to match the `pager_duty.domain_api_identity` system parameter; after 10 failed attempts from one IP address within 60 seconds it refuses further attempts until that window expires.
*   `search_logs(file_path, regex_query)`: Initiates a background log search task and returns its `job_id`. Pager Duty Admins only, and only for a file under `/var/log` that is registered as an active log file visible to the caller.
*   `search_logs_poll(job_id)`: Polls for the result of an active log search task. It returns at once with `pending`, `done` (with matches) or an error; the Log Viewer repeats the call once a second, up to 30 times.
*   `get_log_files()`: Retrieves the paths of the active registered log files (Log Analyzer > Target Files) that the calling Pager Duty Admin is allowed to see.
*   `ping(**kw)`: Returns a simple 200 OK for basic Odoo connectivity checks (`GET /api/v1/pager/ping`, body `{"status": "ok"}`).
*   `heartbeat(hb_uuid, **kw)`: HTTP endpoint for daemons to transmit their heartbeats without XML-RPC. A GET or POST to `/api/v1/pager/heartbeat/<uuid>` records the current time as the last heartbeat of the Heartbeat (Push Monitor) check with that UUID, or returns 404 when no check has it.

---

## 3. Extending the System
To add a new monitoring plugin:
1.  **Model:** Add the type to `check_type` in `pager_check.py`.
2.  **View:** Update `pager_check_views.xml` with relevant fields (visible only for the new type).
3.  **Daemon:** Implement the logic in `execute_check()` within `generalized_monitor.py`. If the check needs an external binary, also map the type to that binary in `verify_and_install_dependencies()` and add the binary to the allow-list in `rpc_ensure_executable()`.
4.  **Test:** Add an isolated test case in `test_generalized_monitor.py`.

---

<stories_and_journeys>
## 4. Architectural Stories & Journeys

*   [Story: Scaling the Watchtower](hams_shared/docs/stories/automated_monitoring_setup.md)
*   [Story: Finding the Needle in the Haystack](hams_shared/docs/stories/log_anomaly_detection.md)
*   [Story: The Midnight Guardian](hams_shared/docs/stories/on_call_alerting.md)
*   [Story: The Data-Driven Post-Mortem](hams_shared/docs/stories/performance_analytics.md)
*   [Journey: Daemon Execution Loop](hams_shared/docs/journeys/daemon_execution_loop.md)
*   [Journey: Escalation Pathway](hams_shared/docs/journeys/escalation_pathway.md)
*   [Journey: Incident Lifecycle](hams_shared/docs/journeys/incident_lifecycle.md)
*   [Journey: Synthetic Monitoring Flow](hams_shared/docs/journeys/synthetic_monitoring_flow.md)
</stories_and_journeys>

---

## 5. Testing & Maintenance
Run module tests using the unified test runner:
```bash
python3 hams_shared/tools/test.py -u pager_duty
```
Daemon tests (for example `test_generalized_monitor.py`, `test_pager_log_analyzer.py`, `test_synthetic_spooler.py`) live in `pager_duty/tests/` with the other tests and run under the same runner; they import the daemon scripts directly. **Do not import Odoo packages in the daemon scripts under `pager_duty/daemon/`** (or in any test placed in that directory); the daemons run outside Odoo.
