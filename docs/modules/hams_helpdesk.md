# Hams Helpdesk

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

Zero-Sudo compliant, lightweight helpdesk management designed for deep SRE (Site Reliability Engineering) integration.

<system_role>
Zero-Sudo compliant, lightweight helpdesk management system designed for SRE (Site Reliability Engineering) workflows. It prioritizes traceability, minimal privilege, and seamless integration with calendar-based on-duty rotations.
</system_role>

<architecture>
The module implements a reactive ticketing system where assignment is driven by on-duty status. It uses a wizard-based handoff mechanism to ensure context is preserved during operator shifts.

"On-duty" means the user that `calendar.event.get_current_on_duty_admin()` returns. This module's own version of that method returns nobody; the `pager_duty` module overrides it to return the user of the pager-duty calendar shift in force right now for the current website (a shift with no website counts for every website; if several overlap, the most recently created wins). This module does not depend on `pager_duty`, so without it, or when no shift is in force, new tickets are left unassigned. A failure in that lookup is logged and the ticket is still created, unassigned.

- **Models**:
    - `hams_helpdesk.ticket`: Main ticket entity, inherits `mail.thread` for communication.
    - `hams_helpdesk.shift_handoff`: Transient wizard for secure ownership transfer.
- **Other ticket sources** (besides the backend form and the portal):
    - Inbound email to the `admin` and `support` mail aliases becomes a ticket; `message_new()` links it to the sender's existing contact as the Customer. `ingest_inbound_email()` is the entry point for the inbound-mail polling daemon and refuses any caller except the `mail_ingest_service_internal` account. It returns `{"status": "ingested"}`, or `{"status": "unroutable", ...}` for mail addressed to no alias (a deterministic miss, so the daemon moves the object to `failed/` without retrying instead of getting a 500).
    - `POST /webhook/github` accepts GitHub `dependabot_alert` (created or reopened) and `workflow_run` (completed with a failure) events whose `X-Hub-Signature-256` HMAC matches the `hams_helpdesk.github_webhook_secret` system parameter, and opens one ticket per alert or run URL. A repeat delivery for a URL whose ticket is still open adds nothing; once that ticket is Resolved or Closed, a new delivery opens a new ticket. With no secret configured, every request is refused with 403.
    - The `pager_duty` module opens tickets for its incidents (see documentation.html).
- **Child-safety mandatory reports**: the ticket type `csam_enticement_trafficking` drives the NCMEC CyberTipline reporting workflow (18 U.S.C. Sec. 2258A). Such a ticket is forced to Critical priority, every Helpdesk Manager is notified, and a report packet is assembled on the ticket. Portal users cannot choose this type, and the external AI triage account's record rule does not include it, so that account cannot read it. See documentation.html.
- **External AI triage**: the `ai_triage_service_internal` account has read-only access to tickets of type `general` and `hams_local_relay` only, and posts its analysis through `mcp_post_internal_note()` as an internal note the customer does not see.
- **Security**: Strict record rules (Odoo `ir.rule`) enforce that portal users only see their own tickets, helpdesk users see tickets assigned to them or unassigned, and helpdesk managers see every ticket. All three are limited to the user's own companies.
- **Multi-Website**: Supports multiple websites by segregating tickets via `website_id`.
- **Multi-Company**: Full multi-company support with strict record rules and automatic company inference: a ticket created with a website but no explicit company takes that website's company.
</architecture>

<security_design>
- **Zero-Sudo Compliance**: No `.sudo()` calls are allowed. All privilege elevations must use service accounts (`hams_helpdesk.user_helpdesk_service` or `zero_sudo.odoo_facility_service_internal`) via `zero_sudo`. The one exception is `ingest_inbound_email()`, which calls `message_process()` with `.sudo()` under an explicit `burn-ignore-sudo` tag, because `message_process()` elevates internally on any alias match anyway. (`pager_duty.user_pager_service_internal` is used by the `pager_duty` module, not by this one.)
- **Micro-Privilege**: Uses `res.groups.privilege` to define granular access.
- **Portal Isolation**: Portal users are strictly limited via record rules to their own `partner_id` and authorized `website_id`.
- **Fail-Fast Integrity**: The module is designed to fail fast if required service accounts are missing or misconfigured, preventing silent failures and ensuring operational reliability. Concretely, ticket creation raises an error if `hams_helpdesk.user_helpdesk_service` cannot be resolved, while a failure looking up the on-duty or upcoming-shift users is only logged.
</security_design>

## Technical Architecture & Anchors

This module operates within strict DevSecOps parameters, ensuring all actions are traceable and privileged escalations are explicitly avoided.

* **Ticket Lifecycle (`[@ANCHOR: helpdesk_ticket_lifecycle]`)**: Defines the stages and constraints of an issue, natively integrating with mail threads.

    - Verified by [@ANCHOR: test_01_ticket_creation_and_routing]

* **Ticket Creation (`[@ANCHOR: helpdesk_ticket_creation]`)**: Intercepts the ORM `create` method to automatically execute pre-shift CC logic and route to the currently on-duty personnel based on calendar availability. "Pre-shift CC" subscribes the users returned by `calendar.event.get_upcoming_duty_shifts()` (people whose shift starts soon) and sends them an internal "Shift CC" note. This module's own version of that method returns no shifts and no module in this repository overrides it, so the step currently does nothing.

    - Verified by [@ANCHOR: test_01_ticket_creation_and_routing]

* **Shift Handoff Initiation (`[@ANCHOR: helpdesk_shift_handoff]`)**: UI action triggering the secure transfer wizard, ensuring the leaving operator leaves context.

    - Verified by [@ANCHOR: test_02_shift_handoff_wizard]

* **Handoff Execution (`[@ANCHOR: helpdesk_handoff_execution]`)**: The backend transaction that officially modifies ownership and commits the transfer briefing to the unalterable chatter log, as an internal note that the customer does not see.

    - Verified by [@ANCHOR: test_02_shift_handoff_wizard]

* **Documentation Injection (`[@ANCHOR: helpdesk_doc_injection]`)**: Automated bootstrapping of user documentation into the central knowledge base via the `zero-sudo` documentation facility, which loads the pages listed under `knowledge_docs` in this module's `__manifest__.py`.

    - Verified by [@ANCHOR: test_05_doc_injection]

* **Multi-Website & Multi-Company Awareness (`[@ANCHOR: helpdesk_multi_website]`)**: Tickets are associated with specific websites and companies to ensure proper data isolation in multi-tenant environments.

    - Verified by [@ANCHOR: test_06_multi_website_awareness_logic]

* **Micro-Privilege Security (`[@ANCHOR: helpdesk_micro_privilege]`)**: Access is strictly controlled via record rules and explicit field-level security in the ORM: `write()` refuses a portal user's change to stage, assignee, priority, customer, ticket type, website, company, calendar event, the archive flag, or any `ncmec_*` field.

    - Verified by [@ANCHOR: test_05_portal_write_restrictions]
* **Helpdesk Operator Workflow**: Verification of the backend operator's ability to manage tickets and execute handoffs.
    - Verified by [@ANCHOR: helpdesk_operator_tour]
* **Helpdesk Portal Facility**: Verification of the portal customer's ability to submit and view tickets.
    - Verified by [@ANCHOR: helpdesk_portal_tour]

* **Portal Ticket Closure**: Customer ability to close their own tickets. ([@ANCHOR: helpdesk_portal_close])

    - Verified by [@ANCHOR: test_portal_close_ticket]

### Function-level anchors

* **On-Duty Admin Resolution:** `[@ANCHOR: hams_helpdesk:COMM_get_current_on_duty_admin]` -- base implementation returning `False`; overridden by `pager_duty` when installed.

* **Upcoming Duty Shift Resolution:** `[@ANCHOR: hams_helpdesk:COMM_get_upcoming_duty_shifts]` -- base implementation returning an empty recordset; used for pre-shift-awareness notifications. No module in this repository overrides it, so the pre-shift CC step does nothing.

* **Callsign Auto-Population:** `[@ANCHOR: hams_helpdesk:COMM_onchange_partner_id]` -- onchange handler copying the reporting partner's callsign onto the ticket.

* **Automated Routing and Notification:** `[@ANCHOR: hams_helpdesk:COMM_automated_routing_and_notification]` -- post-create hook, run under the helpdesk service account (assignment itself happens in `create()`). It posts an internal "assigned to you" note and a desktop notification to the assigned user (skipped for repeat automatic server-exception tickets inside the notification window), does the pre-shift CC, and notifies every Helpdesk Manager of a child-safety report. It fails fast if the service account is misconfigured, while a failed shift lookup is only logged.

* **Portal Home Ticket Counter:** `[@ANCHOR: hams_helpdesk:COMM_prepare_home_portal_values]` -- computes the portal's own "Helpdesk Tickets" card counter, scoped to the visiting partner and website.

* **Portal Ticket Detail Route:** `[@ANCHOR: hams_helpdesk:COMM_portal_ticket_detail]` -- `/my/ticket/<id>`, renders a ticket only to its own reporting partner.

* **Portal Ticket Close Route:** `[@ANCHOR: hams_helpdesk:COMM_portal_ticket_close]` -- `/my/ticket/<id>/close`, lets a portal user close their own ticket.

* **Portal New Ticket Form Route:** `[@ANCHOR: hams_helpdesk:COMM_portal_ticket_new]` -- `/my/tickets/new`, renders the submission form, tolerating a reporter with no callsign yet.

    - Tested by [@ANCHOR: hams_helpdesk:COMM_portal_ticket_new]

## Stories and Journeys

### Ticket Lifecycle Management ([@ANCHOR: helpdesk_ticket_lifecycle])
**Goal**: Efficiently track and resolve system issues while maintaining a clear audit trail.
1.  **Incoming Request**: A new ticket is created, either manually or via automated incident detection.
2.  **Automated Routing**: The system identifies the currently on-duty administrator (`[@ANCHOR: helpdesk_ticket_creation]`) and assigns the ticket.

3.  **Progression**: The operator manages tickets via the list ([@ANCHOR: helpdesk_ticket_list]) and form ([@ANCHOR: helpdesk_ticket_form]) views, moving through stages: New -> In Progress -> Resolved -> Closed. A fifth stage, Spam / Phishing (Quarantined), is a side lane for flagged inbound mail, not a normal step (see documentation.html).
4.  **Customer Communication**: Every stage change except a move into Spam / Phishing triggers an automated update to the reporter (the ticket's Customer), if the ticket has one.

### Incident Resolution Journey ([@ANCHOR: journey_incident_resolution])
**Goal**: Complete the lifecycle of a critical incident from detection to resolution.
1.  **Detection**: System monitor detects a service failure.
2.  **Creation**: A Helpdesk ticket is created.
3.  **Assignment**: The system auto-assigns the ticket to the SRE currently "On-Duty".
4.  **Investigation**: SRE updates stage to "In Progress".
5.  **Resolution**: SRE fixes the issue, updates stage to "Resolved".
6.  **Closure**: After verification, the ticket is moved to "Closed".

### Shift Handoff Protocol ([@ANCHOR: journey_shift_handoff])
**Goal**: Ensure seamless continuity of operations when operators rotate shifts.
1.  **Initiation**: The outgoing operator selects "Shift Handoff" on an active ticket (`[@ANCHOR: helpdesk_shift_handoff]`).
2.  **Context Capture**: A wizard appears requiring the selection of the next assignee and detailed handoff notes. The Shift Handoff button is hidden once a ticket is Resolved or Closed.
3.  **Execution**: Upon confirmation, the system atomically updates ownership and logs a briefing (`[@ANCHOR: helpdesk_handoff_execution]`).

4.  **Verification**: The handoff is recorded in the chatter and the operator workflow is validated by [@ANCHOR: helpdesk_operator_tour].

### Portal Self-Service ([@ANCHOR: journey_portal_self_service])
**Goal**: Empower customers to manage their own tickets.
1.  **Submission**: Customer creates a ticket via the portal ([@ANCHOR: helpdesk_portal_new]).

2.  **Review**: Customer views their tickets in the list ([@ANCHOR: helpdesk_portal_list]) and detail ([@ANCHOR: helpdesk_portal_detail]) views.

3.  **Closure**: Customer closes the ticket when resolved ([@ANCHOR: helpdesk_portal_close]).

4.  **Verification**: Validated by [@ANCHOR: helpdesk_portal_tour].

## 🧪 Specialized Test Environment

This repository uses a specialized test environment with the following characteristics:
- **PostgreSQL Socket**: The database cluster listens on a Unix socket located at `/opt/hams/pgsock`.
- **Test Runner Flags**: Run `tools/test.py -u hams_helpdesk` to test this module. `tools/test.py` has no `--already-provisioned` option; its argument parser rejects unknown options.
- **Python Execution**: Use `/usr/bin/python3` to ensure access to system-installed Odoo dependencies.
- **Linter Overrides**: Use a custom ignore file (`-c <file>`) to bypass fragile tours in other modules if they block testing of this module.

## External Dependencies

None.

## Security & Multi-tenant Isolation
Hams Helpdesk implements strict isolation:
- Record rules ensure users only see tickets for their company.
- Website-level filtering ensures portal users only see tickets created on that specific website, plus tickets not tied to any website (for example, ones created from email).
- Controllers strictly validate ownership and website context before rendering ticket details.

## Automatic server-exception tickets
Unhandled exceptions from pages, RPC/ORM calls and scheduled jobs become tickets of the staff-only
category "Automatic: server exception", one per fingerprint (exception type, innermost addon frame,
module, normalized route), counted on `hams_helpdesk.exception.fingerprint` and listed under
Helpdesk > Server Exceptions. See `docs/stories/server_exception_tickets.md`.
When the person was signed in, the ticket lists the member (name, callsign if any, email, computed live from their account;
staff only) so the site team can write to them: a link to the account, never a copy (section 17 of that story).

### Browser JavaScript errors

Uncaught JavaScript errors on website, portal and back-office pages are posted by
`static/src/js/browser_error_reporter.js` to `POST /hams_helpdesk/browser_exception` and become "Browser"
exception tickets through the same fingerprint pipeline. Not loaded by the relay-served console (a future
opt-in privacy decision). The browser sends its session cookie; the server links the signed-in member's account (a logged-out visitor stays anonymous). What is sent and stored, the checks and the reasons for them (no nonce; a current
asset bundle version instead): `docs/stories/server_exception_tickets.md`, section 16.

## Scanning for raw ticket text in AI readers

The static rule (`ai_reader_rule.py`, run by `test_ai_reader_sites`) scans only the files git tracks
(`[@ANCHOR: hams_helpdesk:ai_reader_tracked_files]`, `[@ANCHOR: hams_helpdesk:ai_reader_candidate_files]`), so a stray
untracked directory (a leftover nested checkout) cannot fail it. `scan_tree(root)` is the whole-repository entry point: it
runs the rule over every non-test Python file the scan picks and returns one line per ticket-like text read that skips the
safe view, and the repository passes only when that list is empty [@ANCHOR: hams_helpdesk:ai_reader_sites_rule]. Outside a git checkout it walks the directory instead.
Outgoing mail that fails to send, and failures processing an incoming bounce, are reported the same way
(source "Outgoing / incoming mail"; counts and fixed labels only, never an address or content).
Failures of external daemons (RabbitMQ consumers, sync and ingest programs) reach the same tickets (source
"External daemon") through a systemd `OnFailure=` template unit and the `reporting_failures` helper; see
`daemon/README.md`.
