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
- **Child-safety mandatory reports**: the ticket type `csam_enticement_trafficking` drives the NCMEC (US National Center for Missing & Exploited Children) CyberTipline reporting workflow (18 U.S.C. Sec. 2258A). Such a ticket is forced to Critical priority, every Helpdesk Manager is notified, and a report packet is assembled on the ticket. Portal users cannot choose this type, and the external AI triage account's record rule does not include it, so that account cannot read it. See documentation.html.
- **External AI triage**: the `ai_triage_service_internal` account has read-only access to tickets of type `general` and `hams_local_relay` only (and never to one the untrusted-text filter marked suspicious), and posts its analysis through `mcp_post_internal_note()` as an internal note the customer does not see.
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

* **Ticket Lifecycle (``helpdesk_ticket_lifecycle``)**: Defines the stages and constraints of an issue, natively integrating with mail threads.

    - Verified by `test_01_ticket_creation_and_routing`

* **Ticket Creation (``helpdesk_ticket_creation``)**: Intercepts the ORM `create` method to automatically execute pre-shift CC logic and route to the currently on-duty personnel based on calendar availability. "Pre-shift CC" subscribes the users returned by `calendar.event.get_upcoming_duty_shifts()` (people whose shift starts soon) and sends them an internal "Shift CC" note. This module's own version of that method returns no shifts and no module in this repository overrides it, so the step currently does nothing.

    - Verified by `test_01_ticket_creation_and_routing`

* **Shift Handoff Initiation (``helpdesk_shift_handoff``)**: UI action triggering the secure transfer wizard, ensuring the leaving operator leaves context.

    - Verified by `test_02_shift_handoff_wizard`

* **Handoff Execution (``helpdesk_handoff_execution``)**: The backend transaction that officially modifies ownership and commits the transfer briefing to the unalterable chatter log, as an internal note that the customer does not see.

    - Verified by `test_02_shift_handoff_wizard`

* **Documentation Injection (``helpdesk_doc_injection``)**: Automated bootstrapping of user documentation into the central knowledge base via the `zero-sudo` documentation facility, which loads the pages listed under `knowledge_docs` in this module's `__manifest__.py`.

    - Verified by `test_05_doc_injection`

* **Multi-Website & Multi-Company Awareness (``helpdesk_multi_website``)**: Tickets are associated with specific websites and companies to ensure proper data isolation in multi-tenant environments.

    - Verified by `test_06_multi_website_awareness_logic`

* **Micro-Privilege Security (``helpdesk_micro_privilege``)**: Access is strictly controlled via record rules and explicit field-level security in the ORM: `write()` refuses a portal user's change to stage, assignee, priority, customer, ticket type, website, company, calendar event, the archive flag, or any `ncmec_*` field.

    - Verified by `test_05_portal_write_restrictions`
* **Helpdesk Operator Workflow**: Verification of the backend operator's ability to manage tickets and execute handoffs.
    - Verified by `helpdesk_operator_tour`
* **Helpdesk Portal Facility**: Verification of the portal customer's ability to submit and view tickets.
    - Verified by `helpdesk_portal_tour`

* **Portal Ticket Closure**: Customer ability to close their own tickets. (`helpdesk_portal_close`)

    - Verified by `test_portal_close_ticket`

### Function-level anchors

* **On-Duty Admin Resolution:** ``hams_helpdesk:COMM_get_current_on_duty_admin`` -- base implementation returning `False`; overridden by `pager_duty` when installed.

* **Upcoming Duty Shift Resolution:** ``hams_helpdesk:COMM_get_upcoming_duty_shifts`` -- base implementation returning an empty recordset; used for pre-shift-awareness notifications. No module in this repository overrides it, so the pre-shift CC step does nothing.

* **Callsign Auto-Population:** ``hams_helpdesk:COMM_onchange_partner_id`` -- onchange handler copying the reporting partner's callsign onto the ticket.

* **Automated Routing and Notification:** ``hams_helpdesk:COMM_automated_routing_and_notification`` -- post-create hook, run under the helpdesk service account (assignment itself happens in `create()`). It posts an internal "assigned to you" note and a desktop notification to the assigned user (skipped for repeat automatic server-exception tickets inside the notification window), does the pre-shift CC, and notifies every Helpdesk Manager of a child-safety report. It fails fast if the service account is misconfigured, while a failed shift lookup is only logged.

* **Portal Home Ticket Counter:** ``hams_helpdesk:COMM_prepare_home_portal_values`` -- computes the portal's own "Helpdesk Tickets" card counter, scoped to the visiting partner and website.

* **Portal Ticket Detail Route:** ``hams_helpdesk:COMM_portal_ticket_detail`` -- `/my/ticket/<id>`, renders a ticket only to its own reporting partner.

* **Portal Ticket Close Route:** ``hams_helpdesk:COMM_portal_ticket_close`` -- `/my/ticket/<id>/close`, lets a portal user close their own ticket.

* **Portal New Ticket Form Route:** ``hams_helpdesk:COMM_portal_ticket_new`` -- `/my/tickets/new`, renders the submission form, tolerating a reporter with no callsign yet.

    - Tested by `hams_helpdesk:COMM_portal_ticket_new`

## Stories and Journeys

### Ticket Lifecycle Management (`helpdesk_ticket_lifecycle`)
**Goal**: Efficiently track and resolve system issues while maintaining a clear audit trail.
1.  **Incoming Request**: A new ticket is created, either manually or via automated incident detection.
2.  **Automated Routing**: The system identifies the currently on-duty administrator (``helpdesk_ticket_creation``) and assigns the ticket.

3.  **Progression**: The operator manages tickets via the list (`helpdesk_ticket_list`) and form (`helpdesk_ticket_form`) views, moving through stages: New -> In Progress -> Resolved -> Closed. A fifth stage, Spam / Phishing (Quarantined), is a side lane for flagged inbound mail, not a normal step (see documentation.html).
4.  **Customer Communication**: Every stage change except a move into Spam / Phishing triggers an automated update to the reporter (the ticket's Customer), if the ticket has one.

### Incident Resolution Journey (`journey_incident_resolution`)
**Goal**: Complete the lifecycle of a critical incident from detection to resolution.
1.  **Detection**: System monitor detects a service failure.
2.  **Creation**: A Helpdesk ticket is created.
3.  **Assignment**: The system auto-assigns the ticket to the SRE currently "On-Duty".
4.  **Investigation**: SRE updates stage to "In Progress".
5.  **Resolution**: SRE fixes the issue, updates stage to "Resolved".
6.  **Closure**: After verification, the ticket is moved to "Closed".

### Shift Handoff Protocol (`journey_shift_handoff`)
**Goal**: Ensure seamless continuity of operations when operators rotate shifts.
1.  **Initiation**: The outgoing operator selects "Shift Handoff" on an active ticket (``helpdesk_shift_handoff``).
2.  **Context Capture**: A wizard appears requiring the selection of the next assignee and detailed handoff notes. The Shift Handoff button is hidden once a ticket is Resolved or Closed.
3.  **Execution**: Upon confirmation, the system atomically updates ownership and logs a briefing (``helpdesk_handoff_execution``).

4.  **Verification**: The handoff is recorded in the chatter and the operator workflow is validated by `helpdesk_operator_tour`.

### Portal Self-Service (`journey_portal_self_service`)
**Goal**: Empower customers to manage their own tickets.
1.  **Submission**: Customer creates a ticket via the portal (`helpdesk_portal_new`).

2.  **Review**: Customer views their tickets in the list (`helpdesk_portal_list`) and detail (`helpdesk_portal_detail`) views.

3.  **Closure**: Customer closes the ticket when resolved (`helpdesk_portal_close`).

4.  **Verification**: Validated by `helpdesk_portal_tour`.

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
For page, RPC and browser reports where the person was signed in, the ticket lists the member (name, callsign if any, email,
computed live from their account; staff only, at most 25 members per failure, the rest only counted) so the site team can write
to them: a link to the account, never a copy (section 17 of that story). A module that knows about guardianship can
substitute the supervising guardian, or no one; this module alone always lists the member. Scheduled jobs, mail and
external daemons have no member.

### Browser JavaScript errors

Uncaught JavaScript errors on website, portal and back-office pages are posted by
`static/src/js/browser_error_reporter.js` to `POST /hams_helpdesk/browser_exception` and become "Browser"
exception tickets through the same fingerprint pipeline. Not loaded by the relay-served console (a future
opt-in privacy decision). The browser sends its session cookie; the server links the signed-in member's account (a logged-out visitor stays anonymous). What is sent and stored, the checks and the reasons for them (no nonce; a current
asset bundle version instead): `docs/stories/server_exception_tickets.md`, section 16.

## Scanning for raw ticket text in AI readers

The static rule (`ai_reader_rule.py`, run by `test_ai_reader_sites`) scans only the files git tracks
(``hams_helpdesk:ai_reader_tracked_files``, ``hams_helpdesk:ai_reader_candidate_files``), so a stray
untracked directory (a leftover nested checkout) cannot fail it. `scan_tree(root)` is the whole-repository entry point: it
runs the rule over every non-test Python file the scan picks and returns one line per ticket-like text read that skips the
safe view, and the repository passes only when that list is empty `hams_helpdesk:ai_reader_sites_rule`. Outside a git checkout it walks the directory instead.
Outgoing mail that fails to send, and failures processing an incoming bounce, are reported the same way
(source "Outgoing / incoming mail"; counts and fixed labels only, never an address or content).
Failures of external daemons (RabbitMQ consumers, sync and ingest programs) reach the same tickets (source
"External daemon") through a systemd `OnFailure=` template unit and the `reporting_failures` helper; see
`daemon/README.md`.

## Anchor reference

One entry per anchor named above, read from `hams_helpdesk` (models, controllers, views, tests, `ai_reader_rule.py`). Entries marked "doc/test id" name a behaviour or a test and have no separate function in the code; where the code carries a differently spelled twin of the id (for example `COMM_helpdesk_ticket_creation` in `models/helpdesk_ticket.py`) the entry says so. The screen-level anchors (`helpdesk_ticket_form`, the portal list, detail and new templates, and the tours) are described in `docs/stories/helpdesk.md`.

### `helpdesk_ticket_lifecycle` [@ANCHOR: helpdesk_ticket_lifecycle]

The set of stages a `hams_helpdesk.ticket` passes through, held in its `stage` selection: New, In Progress, Resolved, Closed, plus the Spam / Phishing quarantine side lane that flagged inbound mail enters directly. A staff user moves a ticket by editing the stage or with the Close Ticket button; a portal user can only close their own. Every stage change except a move into Spam mails the Customer a short update through the thread. The model inherits `mail.thread`, so each ticket is also a conversation. Tested by `test_01_ticket_creation_and_routing` and `test_helpdesk_core.py` lines citing this id. See also `helpdesk_ticket_creation` and `hams_helpdesk:COMM_helpdesk_action_close`.

### `test_01_ticket_creation_and_routing` [@ANCHOR: test_01_ticket_creation_and_routing]

Test, in `tests/test_helpdesk_core.py`. It patches `get_current_on_duty_admin` to return the manager (only when `pager_duty` is installed), silences the bus toast, and creates a ticket for a portal partner. It asserts that the ticket is assigned to the on-duty manager when `pager_duty` is present and left unassigned when it is not, and that the reporting Customer is subscribed to the thread so mail-backs reach them. It cites `helpdesk_ticket_creation`, `helpdesk_ticket_lifecycle`, `COMM_get_current_on_duty_admin`, `COMM_get_upcoming_duty_shifts` and `COMM_automated_routing_and_notification`. In the code the test's own tag is spelled `COMM_test_01_ticket_creation_and_routing`.

### `helpdesk_ticket_creation` [@ANCHOR: helpdesk_ticket_creation]

The overridden `hams_helpdesk.ticket.create(vals_list)` (`@api.model_create_multi`); the code tags it `COMM_helpdesk_ticket_creation`. Before the batch is written it asks `calendar.event.get_current_on_duty_admin()` for the on-duty user (an exception is only logged), strips the triage verdict fields from the caller's values, and fills defaults per ticket: website from context, company from the website, callsign from the partner, assignee from the on-duty user, `notify_state` (suppressed for spam, held while an AI-triage hold window applies) and forced priority 3 for the child-safety type. It then calls `super().create()`, wakes the triage daemon after commit, runs `_automated_routing_and_notification()` and, for child-safety tickets, assembles the report packet. A batch mixing spam and real tickets is created in two calls so real tickets keep their assignment email. Returns the new records in the input order. Raises when the helpdesk service account cannot be resolved. Test: `test_01_ticket_creation_and_routing`.

### `helpdesk_shift_handoff` [@ANCHOR: helpdesk_shift_handoff]

The button method `hams_helpdesk.ticket.action_shift_handoff(self)` (`@public_rpc`; code tag `COMM_helpdesk_shift_handoff`). It requires exactly one ticket (`ensure_one`) and returns an `ir.actions.act_window` dictionary that opens the `hams_helpdesk.shift_handoff` wizard as a modal (`target: new`) with `default_ticket_id` set to the ticket and `default_old_user_id` set to the current assignee (or `False`). It changes nothing itself. The form hides the button for unsaved tickets and for stages Resolved and Closed. Tested by `test_02_shift_handoff_wizard` and the operator tour. See also `helpdesk_handoff_execution`.

### `test_02_shift_handoff_wizard` [@ANCHOR: test_02_shift_handoff_wizard]

Test in `tests/test_helpdesk_core.py` (code tag `COMM_test_02_shift_handoff_wizard` on the method that opens the wizard). It creates a ticket assigned to the manager and a second helpdesk user, creates the wizard with handoff notes, runs `action_confirm_handoff()`, then asserts that ownership moved to the new user and that the chatter holds "Official Shift Handoff Executed" and the briefing text. It covers `helpdesk_shift_handoff` and `helpdesk_handoff_execution`.

### `helpdesk_handoff_execution` [@ANCHOR: helpdesk_handoff_execution]

`hams_helpdesk.shift_handoff.action_confirm_handoff(self)` (`@public_rpc`, `models/shift_handoff.py`; code tag `COMM_helpdesk_handoff_execution`). The wizard is a transient model with fields `ticket_id`, `old_user_id` (read-only), `new_user_id` (required) and `handoff_notes` (required text). On confirmation it switches to the `hams_helpdesk.user_helpdesk_service` environment, writes the new assignee with `mail_notrack`, and posts a briefing (who relinquished, who accepted, the notes, escaped through `markupsafe`) with subtype `mail.mt_note` so it stays internal and never shows in the customer's portal thread; the new assignee is notified through `partner_ids`. Returns `{"type": "ir.actions.act_window_close"}`. Raises if the service account is missing (fail fast). It is not idempotent: each call posts another briefing.

### `helpdesk_doc_injection` [@ANCHOR: helpdesk_doc_injection]

Doc/test id, no function in this module. The module's `__manifest__.py` lists pages under `knowledge_docs` (the member manual "Getting help: Support tickets", the internal "Hams Helpdesk" page and the lifecycle, creation and incident stories); the `zero_sudo` documentation facility reads that key on install and publishes the pages to the knowledge base, so this module needs no privileged code of its own. Only the member manual is marked `public`. Tested for the member manual by `test_member_manual_is_a_published_member_article` (`tests/test_member_manual.py`).

### `test_05_doc_injection` [@ANCHOR: test_05_doc_injection]

Doc id only: there is no test method of this name in `hams_helpdesk/tests/`; the nearest real test is `test_member_manual_is_a_published_member_article`, which checks that the manifest's member manual becomes a published `knowledge.article`. To-do: either rename that coverage to this id or drop the id from the module documentation. Unknown whether the other manifest pages are covered by any test.

### `helpdesk_multi_website` [@ANCHOR: helpdesk_multi_website]

The segregation of tickets by `website_id` and `company_id` (the code tag on the test is `COMM_helpdesk_multi_website`). A ticket created with a website but no company takes that website's company in `create()`; a ticket created while the context carries `website_id` takes it as its website. Portal record rules show a member only tickets of their own partner whose website is the current one or empty (for example tickets made from email); the portal routes repeat the check and redirect to `/my` on a mismatch. Tested by `test_06_multi_website_awareness_logic`.

### `test_06_multi_website_awareness_logic` [@ANCHOR: test_06_multi_website_awareness_logic]

Test in `tests/test_helpdesk_multi_website.py` (the controller `portal_my_tickets` also cites it). It creates tickets for one portal partner on website 1, website 2 and no website, then searches as the portal user with each website in the context. It asserts that the user sees their own-website ticket and the no-website ticket and does not see the other website's ticket, in both directions. Covers `helpdesk_multi_website`. Its tag in code is `COMM_test_06_multi_website_awareness_logic`.

### `helpdesk_micro_privilege` [@ANCHOR: helpdesk_micro_privilege]

The write guard in `hams_helpdesk.ticket.write(vals)` (code tag `COMM_helpdesk_micro_privilege`). When the caller is in `base.group_portal`, a write that names any of the restricted fields (stage, the suspicion and triage fields, notify state, assignee, priority, calendar event, website, company, `partner_id`) or any field starting with `ncmec_` raises `AccessError("Portal users are not authorized to modify administrative fields.")`. Portal users may still edit permitted fields such as the description. The `partner_id` entry stops a member moving a ticket onto a stranger's account; the `ncmec_` prefix rule covers future child-safety report fields by default. Tests: `test_05_portal_write_restrictions` and `test_05b_portal_cannot_reassign_partner_id`.

### `test_05_portal_write_restrictions` [@ANCHOR: test_05_portal_write_restrictions]

Test in `tests/test_helpdesk_core.py`. It makes a ticket for a portal user, then as that user attempts to write `stage` and `user_id` and asserts `AccessError` for each, and finally writes `description` and asserts that this still works. Covers `helpdesk_micro_privilege`. Its companion `test_05b_portal_cannot_reassign_partner_id` checks the `partner_id` case.

### `helpdesk_operator_tour` [@ANCHOR: helpdesk_operator_tour]

Test id for the staff screens. The method `test_helpdesk_operator_tour` in `tests/test_ui_tours.py` starts the browser tour `helpdesk_operator_tour` as `hd_manager_tour` (create, save, shift handoff); `test_helpdesk_operator_rendering` additionally checks the backend form loads with HTTP 200. Details of the steps: see `hams_helpdesk:COMM_helpdesk_operator_tour` in `docs/stories/helpdesk.md`. Needs a browser, so it is slow.

### `helpdesk_portal_tour` [@ANCHOR: helpdesk_portal_tour]

Test id for the member screens. The browser tour `helpdesk_portal_tour` submits a ticket with callsign `K1AAA`, checks the detail page and closes the ticket; the controller `portal_ticket_submit` cites it as its verifier. `test_helpdesk_portal_rendering` in `tests/test_ui_tours.py` checks `/my/tickets` and `/my/tickets/new` return 200. The Python test method that was meant to launch the tour is a commented-out stub, so whether the tour is started by anything else is unknown (to-do). See `hams_helpdesk:COMM_helpdesk_portal_tour`.

### `helpdesk_portal_close` [@ANCHOR: helpdesk_portal_close]

A member closing their own ticket: the "Close Ticket" form on the detail page posts to `/my/ticket/<id>/close`, whose controller calls `hams_helpdesk.ticket.action_portal_close()`. That method raises `AccessError` unless the caller owns the ticket (or is a helpdesk user), then writes `stage = closed` as the helpdesk service account and posts "Ticket closed by customer." A closed ticket is left unchanged. Tested by `test_portal_close_ticket`. Code tag for the method: `COMM_helpdesk_portal_close`.

### `test_portal_close_ticket` [@ANCHOR: test_portal_close_ticket]

Test in `tests/test_ui_tours.py`. It creates a new ticket for the portal user, signs in over HTTP, reads the CSRF token from `/my/tickets`, posts to `/my/ticket/<id>/close` and asserts the ticket stage is `closed`. It cites `helpdesk_portal_close` and the close-route anchor. Failure means the route, its CSRF handling or `action_portal_close` regressed.

### `hams_helpdesk:COMM_get_current_on_duty_admin` [@ANCHOR: hams_helpdesk:COMM_get_current_on_duty_admin]

`calendar.event.get_current_on_duty_admin(self)` (`models/calendar_event.py`, `@api.private`). Takes no arguments and returns `False`: this module has no notion of a duty roster. The `pager_duty` module overrides it to return the user of the pager-duty shift in force now for the current website. `create()` calls it inside a try block, so a failing override is logged and the ticket is created unassigned. No locks, no side effects. Test: `test_01_ticket_creation_and_routing`.

### `hams_helpdesk:COMM_get_upcoming_duty_shifts` [@ANCHOR: hams_helpdesk:COMM_get_upcoming_duty_shifts]

`calendar.event.get_upcoming_duty_shifts(self)` (`@api.private`). Takes no arguments and returns an empty `calendar.event` recordset. It exists so `_automated_routing_and_notification()` can ask which users' shifts start soon and copy them ("Shift CC"). No module in this repository overrides it, so the pre-shift copy does nothing; the caller catches and logs any error from an override at info level. Test: `test_01_ticket_creation_and_routing`.

### `hams_helpdesk:COMM_onchange_partner_id` [@ANCHOR: hams_helpdesk:COMM_onchange_partner_id]

`hams_helpdesk.ticket._onchange_partner_id(self)` (`@api.onchange("partner_id")`). In the backend form, when the user picks a Customer who has a `callsign` on their partner record, the ticket's `callsign` field is filled with it; with no partner or no callsign it does nothing. It only runs in the form client, so `create()` has its own equivalent that copies the partner callsign when none is supplied. Returns nothing and has no side effects beyond the in-memory form values.

### `hams_helpdesk:COMM_automated_routing_and_notification` [@ANCHOR: hams_helpdesk:COMM_automated_routing_and_notification]

`hams_helpdesk.ticket._automated_routing_and_notification(self)`, called from `create()` on the new records. Asks for upcoming-shift users (failure only logged), then, as the `hams_helpdesk.user_helpdesk_service` account (a missing account raises; deliberately not caught), for each ticket: skips tickets whose `notify_state` is `held`; for an assigned ticket that is not quiet (a repeat automatic server-exception ticket in the notice window, or a spam ticket) posts an internal "assigned to you" note and sends a desktop toast via `bus.bus` as the facility service account; copies the next shift; and for the child-safety type subscribes and notifies every Helpdesk Manager. Notes use subtype `mail.mt_note` so customers never see them. Returns nothing. Test: `test_01_ticket_creation_and_routing`.

### `hams_helpdesk:COMM_prepare_home_portal_values` [@ANCHOR: hams_helpdesk:COMM_prepare_home_portal_values]

`HelpdeskPortal._prepare_home_portal_values(self, counters)` (`controllers/portal.py`), an override of the `/my` home page value builder. When `"ticket_count"` is in `counters` it adds the count of the visiting member's tickets, searched as the helpdesk service user and limited to the member's partner and to the current website or no website; it returns the values dictionary from `super()` extended with that count. It runs on every `/my` render, so it is one `search_count`.

### `hams_helpdesk:COMM_portal_ticket_detail` [@ANCHOR: hams_helpdesk:COMM_portal_ticket_detail]

`HelpdeskPortal.portal_ticket_detail(self, ticket_id, **kw)`, route `GET /my/ticket/<int:ticket_id>` (`auth="user"`, website). Loads the ticket as the helpdesk service user and redirects to `/my` when it does not exist, belongs to another partner, or belongs to another website. Otherwise renders `hams_helpdesk.portal_ticket_detail` with the ticket re-bound to the member's own user, so field reads obey the member's rights. Test: `test_portal_ticket_detail_shows_own_ticket_and_denies_others`.

### `hams_helpdesk:COMM_portal_ticket_close` [@ANCHOR: hams_helpdesk:COMM_portal_ticket_close]

`HelpdeskPortal.portal_ticket_close(self, ticket_id, **kw)`, route `POST /my/ticket/<int:ticket_id>/close` (`auth="user"`, `csrf=True`). Redirects to `/my` when the ticket is missing or not the member's; otherwise calls `action_portal_close()` as the member and redirects back to the detail page. GET is not allowed. Test: `test_portal_close_ticket`. See `helpdesk_portal_close`.

### `hams_helpdesk:COMM_portal_ticket_new` [@ANCHOR: hams_helpdesk:COMM_portal_ticket_new]

`HelpdeskPortal.portal_ticket_new(self, **kw)`, route `GET /my/tickets/new` (`auth="user"`, website). Renders the submission form with the member's callsign preset (an empty callsign is allowed: someone studying for a licence has none), the category list without the internal types `csam_enticement_trafficking` and `server_exception`, and a default category taken from `?ticket_type=` only when it is in that list, otherwise `general`. Test: `test_portal_ticket_new_renders_without_callsign`. See `hams_helpdesk:COMM_helpdesk_portal_new`.

### `hams_helpdesk:ai_reader_tracked_files` [@ANCHOR: hams_helpdesk:ai_reader_tracked_files]

`_tracked_files(root)` in `ai_reader_rule.py`. Runs `git -C root ls-files -z -- *.py` (with `safe.directory=*`, 60 second timeout) and returns the list of tracked relative paths, or `None` when `root` is not a git checkout, git fails or times out. The scan thereby covers only the repository's own code, not a stray nested checkout. Reads only; no side effects.

### `hams_helpdesk:ai_reader_candidate_files` [@ANCHOR: hams_helpdesk:ai_reader_candidate_files]

`_candidate_files(root)` in `ai_reader_rule.py`, a generator of absolute file paths for the scan. With a non-empty tracked list it yields the tracked files, sorted, skipping anything under a directory in `SKIP_DIRS` or a dot-directory and anything that is not a regular file. When `_tracked_files` returns `None` or an empty list it walks the directory tree with `os.walk`, pruning the same directories. It takes no arguments beyond `root` and has no side effects.

### `hams_helpdesk:ai_reader_sites_rule` [@ANCHOR: hams_helpdesk:ai_reader_sites_rule]

`scan_tree(root, daemon_only=False)` in `ai_reader_rule.py`, the whole-repository entry point of the static rule run by `test_ai_reader_sites`. For each non-test Python file chosen by `_py_files` (only files under `daemon` or `daemons` directories when `daemon_only` is true) it runs `check_source` and collects `"relative/path:line: message"` strings, one per read of ticket-like text (`read`, `search_read`, `search_fetch` of text fields) in an AI-reader function that shows no safe-view marker. The repository passes when the list is empty. A line can be exempted with the `ticket-ai-ignore:` tag. Files are read as UTF-8; a decoding error propagates.

### `journey_incident_resolution` [@ANCHOR: journey_incident_resolution]

Journey (a documented sequence, no code): a monitor detects a failure, a ticket is created, `create()` auto-assigns it to the on-duty user, the engineer moves it to In Progress, then Resolved, and after verification Closed. The same journey is in `docs/journeys/incident_resolution.md`, published to the knowledge base through the manifest. Steps map to `helpdesk_ticket_creation` and `helpdesk_ticket_lifecycle`; there is no test of its own beyond those.

### `journey_shift_handoff` [@ANCHOR: journey_shift_handoff]

Journey: the outgoing operator presses Shift Handoff on an active ticket (`helpdesk_shift_handoff`), the wizard demands the next assignee and handoff notes, confirmation runs `helpdesk_handoff_execution` which reassigns and posts the briefing as an internal note, and the operator tour verifies it. The button is hidden once the ticket is Resolved or Closed. Covered by `test_02_shift_handoff_wizard` and `helpdesk_operator_tour`.

### `journey_portal_self_service` [@ANCHOR: journey_portal_self_service]

Journey: a member submits a ticket from the portal (`hams_helpdesk:COMM_helpdesk_portal_new`), reviews their tickets in the list and detail pages, and closes the ticket when resolved (`helpdesk_portal_close`). Everything runs as the signed-in member; the service account does the reads and the close. Covered by `helpdesk_portal_tour`.

### `helpdesk_ticket_list` [@ANCHOR: helpdesk_ticket_list]

The backend list view of tickets (record `view_hams_helpdesk_ticket_list`, code tag `COMM_helpdesk_ticket_list`): id, name, callsign, assignee, stage, priority and the suspicious flag, with optional member-count columns for staff. It is the default view of Helpdesk > Tickets and the first step of the lifecycle story; the full column description is under `hams_helpdesk:COMM_helpdesk_ticket_list` in `docs/stories/helpdesk.md`. Tested by `test_helpdesk_operator_rendering`.

### `helpdesk_ticket_form` [@ANCHOR: helpdesk_ticket_form]

The backend form view `view_hams_helpdesk_ticket_form` (code tag `COMM_helpdesk_ticket_form`), where an operator works one ticket: Close Ticket and Shift Handoff buttons, the stage status bar, the child-safety report buttons (server-side guarded) and the chatter. Details are under `hams_helpdesk:COMM_helpdesk_ticket_form` in `docs/stories/helpdesk.md`. Tested by `test_helpdesk_operator_rendering`, which loads the form and expects HTTP 200.

### `helpdesk_portal_list` [@ANCHOR: helpdesk_portal_list]

The member's ticket list page `/my/tickets` (template `portal_my_tickets`, controller `portal_my_tickets`; code tag `COMM_helpdesk_portal_list`): up to 20 of the member's own tickets per page, newest first, with a pager, priority badges and a New Ticket button. Searched as the helpdesk service user but limited to the member's partner and website. Tested by `test_helpdesk_portal_rendering`; details under `hams_helpdesk:COMM_helpdesk_portal_list` in `docs/stories/helpdesk.md`.

### `helpdesk_portal_detail` [@ANCHOR: helpdesk_portal_detail]

The member's single-ticket page `/my/ticket/<id>` (template `portal_ticket_detail`, controller `COMM_portal_ticket_detail`; code tag `COMM_helpdesk_portal_detail`): status, callsign, priority, description and the public message thread, with a Close Ticket button while open. Other members' tickets redirect to `/my`. Tested by `test_helpdesk_portal_rendering` and the portal tour; details under `hams_helpdesk:COMM_helpdesk_portal_detail` in `docs/stories/helpdesk.md`.

### `helpdesk_portal_new` [@ANCHOR: helpdesk_portal_new]

The member's submission form `/my/tickets/new` (template `portal_ticket_new`, controller `COMM_portal_ticket_new`; code tag `COMM_helpdesk_portal_new`) which posts to `/my/tickets/submit` with a CSRF token: subject (required), callsign, category and description. The category list excludes the internal child-safety and server-exception types and the server re-validates a posted category, falling back to `general`. Tested by `test_helpdesk_portal_rendering` and `test_portal_ticket_new_renders_without_callsign`; details under `hams_helpdesk:COMM_helpdesk_portal_new`.
