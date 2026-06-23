# 📟 Pager Duty & Generalized Monitoring (`pager_duty`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

[@ANCHOR: pager_duty_module_root]

The Pager Duty module is an enterprise-grade Site Reliability Engineering (SRE) suite designed to keep your Odoo infrastructure running smoothly. It provides active monitoring, intelligent alerting, and automated incident management.

## 🌟 What It Does

*   **Active System Monitoring:** Continuously checks the health of web workers, background daemons, databases, network connections, and hardware.
*   **Smart Alerting:** Routes alerts to the right person based on Odoo Calendar schedules, preventing alert fatigue.
*   **Automated Escalation:** Escalates unacknowledged incidents to wider groups or management.
*   **Incident Analytics:** Tracks Mean Time to Acknowledge (MTTA) and Mean Time to Resolve (MTTR).
*   **Helpdesk Integration:** Automatically creates tickets in the Helpdesk module for incoming incidents.
*   **Multi-Website Support:** Partition monitoring checks and incidents by website to support multi-tenant Odoo deployments.

## 🛠️ How to Set It Up

1.  **Dependencies:** Ensure `redis`, `psutil`, `ntplib`, `pymysql`, and `ldap3` are installed in your Python environment.
2.  **Installation:** Install the `pager_duty` module from the Odoo Apps menu.
3.  **Daemon Configuration:**
    *   Navigate to **Pager Duty > Monitoring Checks**.
    *   Use the "JSON Configuration Tools" (Import/Export) to synchronize the database with the daemon's `pager_config.json`.
    *   Deploy and start the Python daemons located in the `daemon/` directory (see `DEPLOYMENT.md` for systemd service examples).

## 🚀 Key Features and Operations

### Monitoring Checks
Create diverse checks for your infrastructure:
- **HTTP/HTTPS/HTTP3:** Verify website availability and content.
- **PostgreSQL/MySQL:** Ensure database connectivity and performance.
- **System Resources:** Monitor CPU, RAM, and Disk space.
- **Service Status:** Check systemd services and Docker containers.
- **Hard Drive Health:** Proactive SMART monitoring.
- **Custom Scripts:** Execute sandboxed Bash or Playwright scripts for synthetic journeys.

### Incident Management
- **Dashboard:** The NOC Dashboard provides a real-time overview of active and resolved incidents. It includes burn-in protection for long-term display.
- **Acknowledgement:** Engineers can acknowledge incidents to stop further escalation.
- **Auto-Resolution:** The system automatically resolves incidents when the underlying check returns to a healthy state.
- **Escalation:** Unacknowledged incidents are automatically escalated after 15 minutes to ensure attention.

### On-Call Scheduling
Integrates with the Odoo Calendar. Mark calendar events as "Pager Duty Shift" to define the current on-call engineer.

---

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for Software Engineers, SREs, and Integrators.
</system_role>

## 1. Architecture Overview (CQRS)
The module follows a **Command Query Responsibility Segregation (CQRS)** pattern. Odoo serves as the configuration and reporting plane, while standalone Python daemons handle the high-frequency execution loops.

### Key Components:
*   **Control Plane:** Odoo records (`pager.check`) define what to monitor. [@ANCHOR: generalized_pager_config]
*   **Data Plane (Daemons):**
    *   `generalized_monitor.py`: Executes standard checks (HTTP, TCP, SQL, etc.). [@ANCHOR: daemon_execute_check]
    *   `pager_log_analyzer.py`: Tails system logs for regex matches in real-time. [@ANCHOR: pd_log_api_i18n]
    *   `pager_smart_spooler.py`: Securely collects hardware health data (SMART).
    *   `pager_synthetic_spooler.py`: Executes sandboxed (Bubblewrap) Playwright/Bash tests. [@ANCHOR: synthetic_i18n]
*   **Inter-Process Communication (IPC):** Uses Redis Pub/Sub and Queues for high-speed communication between Odoo workers and background daemons.

### Security & Micro-Privileges:
*   **Zero-Sudo RPC:** Daemons authenticate via the `pager_service_internal` service account. No `sudo()` is used.
*   **Sandboxing:** Synthetic checks run inside a strict **Bubblewrap (bwrap)** sandbox with optional network isolation.
*   **Service Accounts:** The module uses `zero_sudo.security.utils` to securely escalate privileges within Odoo's ACL framework.
*   **Multi-Website Isolation:** Data is partitioned by `website_id`. The NOC Dashboard respects `website_id` passed via query parameters or context.

---

## 2. Developer API & Integration

### On-Call Query API
Other modules can query the currently active responder:
```python
on_duty_user = self.env["calendar.event"].get_current_on_duty_admin()
# Returns res.users recordset or False
```
[@ANCHOR: test_pager_notification]

### Incident Reporting API
External scripts or modules can report incidents programmatically:
```python
self.env["pager.incident"].report_incident({
    "source": "Custom Script",
    "severity": "high",
    "description": "Critical failure detected"
})
```
[@ANCHOR: report_incident_rate_limit]

### Helpdesk Adapter
The module automatically bridges incidents to Helpdesk tickets using an adapter pattern. It respects the `pager_duty.helpdesk_model` system parameter.
[@ANCHOR: pd_helpdesk_adapter]

---

## 3. Extending the System
To add a new monitoring plugin:
1.  **Model:** Add the type to `check_type` in `pager_check.py`.
2.  **View:** Update `pager_check_views.xml` with relevant fields (visible only for the new type).
3.  **Daemon:** Implement the logic in `execute_check()` within `generalized_monitor.py`.
4.  **Test:** Add an isolated test case in `test_generalized_monitor.py`.

---

<stories_and_journeys>
## 4. Architectural Stories & Journeys

*   [Story: Scaling the Watchtower](docs/stories/automated_monitoring_setup.md)
*   [Story: Finding the Needle in the Haystack](docs/stories/log_anomaly_detection.md)
*   [Story: The Midnight Guardian](docs/stories/on_call_alerting.md)
*   [Story: The Data-Driven Post-Mortem](docs/stories/performance_analytics.md)
*   [Journey: Daemon Execution Loop](docs/journeys/daemon_execution_loop.md)
*   [Journey: Escalation Pathway](docs/journeys/escalation_pathway.md)
*   [Journey: Incident Lifecycle](docs/journeys/incident_lifecycle.md)
*   [Journey: Synthetic Monitoring Flow](docs/journeys/synthetic_monitoring_flow.md)
</stories_and_journeys>

---

## 5. Testing & Maintenance
Run module tests using the unified test runner:
```bash
python3 tools/test.py -u pager_duty --already-provisioned
```
Daemon tests are located in `pager_duty/daemon/` and run via pure `unittest`. **Do not import Odoo packages in daemon tests.**
