# Hams Helpdesk

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

Zero-Sudo compliant, lightweight helpdesk management designed for deep SRE integration.

<system_role>
Zero-Sudo compliant, lightweight helpdesk management system designed for SRE (Site Reliability Engineering) workflows. It prioritizes traceability, minimal privilege, and seamless integration with calendar-based on-duty rotations.
</system_role>

<architecture>
The module implements a reactive ticketing system where assignment is driven by on-duty status. It uses a wizard-based handoff mechanism to ensure context is preserved during operator shifts.

- **Models**:
    - `hams_helpdesk.ticket`: Main ticket entity, inherits `mail.thread` for communication.
    - `hams_helpdesk.shift_handoff`: Transient wizard for secure ownership transfer.
- **Security**: Strict IR rules enforce that portal users only see their own tickets, and helpdesk users see tickets assigned to them or unassigned.
- **Multi-Website**: Supports multiple websites by segregating tickets via `website_id`.
</architecture>

<security_design>
- **Zero-Sudo Compliance**: No `.sudo()` calls are allowed. All privilege elevations must use service accounts (`hams_helpdesk.user_helpdesk_service` or `pager_duty.user_pager_service_internal`) via `zero_sudo`.
- **Micro-Privilege**: Uses `res.groups.privilege` to define granular access.
- **Portal Isolation**: Portal users are strictly limited via record rules to their own `partner_id` and authorized `website_id`.
- **Fail-Fast Integrity**: The module is designed to fail fast if required service accounts are missing or misconfigured, preventing silent failures and ensuring operational reliability.
</security_design>

## Technical Architecture & Anchors

This module operates within strict DevSecOps parameters, ensuring all actions are traceable and privileged escalations are explicitly avoided.

* **Ticket Lifecycle (`[@ANCHOR: helpdesk_ticket_lifecycle]`)**: Defines the stages and constraints of an issue, natively integrating with mail threads.
    - Verified by [@ANCHOR: test_01_ticket_creation_and_routing]
* **Ticket Creation (`[@ANCHOR: helpdesk_ticket_creation]`)**: Intercepts the ORM `create` method to automatically execute pre-shift CC logic and route to the currently on-duty personnel based on calendar availability.
    - Verified by [@ANCHOR: test_01_ticket_creation_and_routing]
* **Shift Handoff Initiation (`[@ANCHOR: helpdesk_shift_handoff]`)**: UI action triggering the secure transfer wizard, ensuring the leaving operator leaves context.
    - Verified by [@ANCHOR: test_02_shift_handoff_wizard]
* **Handoff Execution (`[@ANCHOR: helpdesk_handoff_execution]`)**: The backend transaction that officially modifies ownership and commits the transfer briefing to the unalterable chatter log.
    - Verified by [@ANCHOR: test_02_shift_handoff_wizard]
* **Documentation Injection (`[@ANCHOR: helpdesk_doc_injection]`)**: Automated bootstrapping of user documentation into the central knowledge base via the `zero-sudo` documentation facility.
    - Verified by [@ANCHOR: test_05_doc_injection]
* **Multi-Website Awareness (`[@ANCHOR: helpdesk_multi_website]`)**: Tickets are associated with specific websites to ensure proper data isolation in multi-tenant environments.
    - Verified by [@ANCHOR: test_06_multi_website_awareness_logic]
* **Micro-Privilege Security (`[@ANCHOR: helpdesk_micro_privilege]`)**: Access is strictly controlled via record rules and explicit field-level security in the ORM.
    - Verified by [@ANCHOR: test_05_portal_write_restrictions]

## Stories and Journeys

### Ticket Lifecycle Management ([@ANCHOR: helpdesk_ticket_lifecycle])
**Goal**: Efficiently track and resolve system issues while maintaining a clear audit trail.
1.  **Incoming Request**: A new ticket is created, either manually or via automated incident detection.
2.  **Automated Routing**: The system identifies the currently on-duty administrator (`[@ANCHOR: helpdesk_ticket_creation]`) and assigns the ticket.
3.  **Progression**: The operator moves the ticket through stages: New -> In Progress -> Resolved -> Closed.
4.  **Customer Communication**: Every stage change triggers an automated update to the reporter.

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
2.  **Context Capture**: A wizard appears requiring the selection of the next assignee and detailed handoff notes.
3.  **Execution**: Upon confirmation, the system atomically updates ownership and logs a briefing (`[@ANCHOR: helpdesk_handoff_execution]`).

## 🧪 Specialized Test Environment

This repository uses a specialized test environment with the following characteristics:
- **PostgreSQL Socket**: The database cluster listens on a Unix socket located at `/opt/hams/pgsock`.
- **Test Runner Flags**: You must use the `--already-provisioned` flag with `tools/test.py` if the environment is already bootstrapped.
- **Python Execution**: Use `/usr/bin/python3` to ensure access to system-installed Odoo dependencies.
- **Linter Overrides**: Use a custom ignore file (`-c <file>`) to bypass fragile tours in other modules if they block testing of this module.
