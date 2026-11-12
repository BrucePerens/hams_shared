# Helpdesk User Manual

This manual details the HAMS Helpdesk functionality, providing support pathways for users and operators.

## For the Operator

The backend system offers robust capabilities for support staff.

### Ticket Form
Operators manage individual issues using the detailed ticket form view.
`hams_helpdesk:COMM_helpdesk_ticket_form`
This interface provides access to the ticket lifecycle, customer details, and priority settings.

### Ticket List
To gain an overview of pending work, operators use the list view.
`hams_helpdesk:COMM_helpdesk_ticket_list`
This summarizes all active tickets, showing current assignees and statuses.

### Operator Workflow
A comprehensive workflow ensures timely resolution of tickets by operators.
`hams_helpdesk:COMM_helpdesk_operator_tour`
Operators can process tickets from submission to closure seamlessly.

## For the Portal User

End users have access to self-service portals (the signed-in area of the website) to track their requests.

### Portal Ticket Listing
Customers can view their historical and active requests.
`hams_helpdesk:COMM_helpdesk_portal_list`
This list displays the status of all submitted issues.

### Portal Ticket Details
Detailed progress on a specific issue can be tracked here.
`hams_helpdesk:COMM_helpdesk_portal_detail`
Users can read operator replies and view status updates.

### Creating a New Ticket
When a new issue arises, customers can quickly submit a request.
`hams_helpdesk:COMM_helpdesk_portal_new`
This form captures essential information like the user's callsign (an amateur radio operator's official identifier) and problem description.

### Portal Workflow
The end-to-end journey for a customer from reporting to resolution.
`hams_helpdesk:COMM_helpdesk_portal_tour`
Users can navigate easily between their submitted issues and detailed updates.

### Closing a Ticket
If a problem resolves itself, users can close their own tickets.
`hams_helpdesk:COMM_helpdesk_portal_close`
This empowers customers to manage their own support requests efficiently.

### Support Menu Entry
Signed-in members (portal and internal users) see a "Support" item in the website menu that opens `/my/tickets`, their own help-desk tickets, where they can start a new one. The item is restricted by group, so visitors who are not signed in do not see it.
`hams_helpdesk:member_menu_item`

## Anchor reference

Each entry below was read from the code in `hams_helpdesk` (views, tours, `controllers/portal.py`, `models/helpdesk_ticket.py`). The first three entries are the staff (backend) side and the next five are the signed-in member (portal) side; the last is the website menu entry that leads members to it. None of the screens is open to visitors who are not signed in.

### `hams_helpdesk:COMM_helpdesk_ticket_form` [@ANCHOR: hams_helpdesk:COMM_helpdesk_ticket_form]

**Purpose:** the backend form view `view_hams_helpdesk_ticket_form` (`views/helpdesk_ticket_views.xml`) of the `hams_helpdesk.ticket` model, where staff read and work one ticket. It is a view record, not a function: it has no call signature.

**Contents:** the header holds the "Close Ticket" button (`action_close`, hidden for an unsaved or already closed ticket), the "Shift Handoff" button (`action_shift_handoff`, hidden once resolved or closed) and the NCMEC report buttons, followed by the clickable `stage` status bar. The sheet shows subject, callsign, assignee, priority, description and the chatter.

**Security:** the model's access rules (`security/ir.model.access.csv`) limit the form to the helpdesk user and manager groups; the three NCMEC buttons are further guarded on the server by group, ticket type and report state, not merely hidden.

**See also:** the tour `hams_helpdesk:COMM_helpdesk_operator_tour` (the form is edited, saved and handed off in it), the render test in `tests/test_ui_tours.py` and `hams_helpdesk:COMM_helpdesk_action_close`.

### `hams_helpdesk:COMM_helpdesk_ticket_list` [@ANCHOR: hams_helpdesk:COMM_helpdesk_ticket_list]

**Purpose:** the backend list view `view_hams_helpdesk_ticket_list`, the default view of the Helpdesk > Tickets action `action_hams_helpdesk_ticket` (view modes list, then form). It is how an operator sees all pending work at a glance.

**Columns:** id (shown as `#`), name, callsign, assignee (`user_id`), `stage`, `priority` (priority widget), `suspicious` (optional, shown by default and highlighted red when set), and two optional columns hidden by default, `affected_member_count` and `latest_member_label`, which only the helpdesk user and system groups can see.

**Notes:** the separate "Spam / Phishing Review" action reuses this same list and form with the domain `stage = spam`, so quarantined tickets stay recoverable. The "Create" button of this view starts the operator tour.

**See also:** `hams_helpdesk:COMM_helpdesk_ticket_form`, `hams_helpdesk:COMM_helpdesk_operator_tour`, and the test in `tests/test_helpdesk_core.py` that cites this anchor.

### `hams_helpdesk:COMM_helpdesk_operator_tour` [@ANCHOR: hams_helpdesk:COMM_helpdesk_operator_tour]

**Purpose:** the browser tour `helpdesk_operator_tour` (`static/src/tours/helpdesk_operator_tour.js`), which is the UI test for the whole staff workflow and the test cited by the audit-ignore tags on the ticket form, list, dashboard and shift-handoff views.

**Steps:** it opens `hams_helpdesk.action_hams_helpdesk_ticket`, clicks Create, fills the subject, saves safely with `TourUtils.safeSave()` (from `zero_sudo`), clicks "Shift Handoff", picks the "Helpdesk Manager" user in the wizard, enters handoff notes and confirms. It runs as the login `hd_manager_tour`.

**Notes:** it is started from `test_helpdesk_operator_tour` in `tests/test_ui_tours.py` with `start_tour` and needs a browser, so it is slow. Failure means the staff screens no longer render or the handoff wizard changed.

**See also:** `hams_helpdesk:COMM_helpdesk_ticket_form`, `hams_helpdesk:COMM_helpdesk_ticket_list`.

### `hams_helpdesk:COMM_helpdesk_portal_list` [@ANCHOR: hams_helpdesk:COMM_helpdesk_portal_list]

**Purpose:** the QWeb template `portal_my_tickets` (`views/portal_templates.xml`) behind the member page `/my/tickets` (route `HelpdeskPortal.portal_my_tickets`, `auth="user"`, website pages). It lists the signed-in member's own tickets.

**Data and limits:** the controller searches `hams_helpdesk.ticket` as the module's service user (`hams_helpdesk.user_helpdesk_service`), filtered to the member's partner and to tickets with no website or the current website, newest first, 20 per page with a pager. The table shows ticket number, title, callsign, a priority badge (Critical, High, Medium, Low) and the stage. With no tickets it says so. A "New Ticket" button links to `/my/tickets/new`.

**See also:** `hams_helpdesk:portal_my_tickets` (the controller), `hams_helpdesk:COMM_helpdesk_portal_tour`.

### `hams_helpdesk:COMM_helpdesk_portal_detail` [@ANCHOR: hams_helpdesk:COMM_helpdesk_portal_detail]

**Purpose:** the QWeb template `portal_ticket_detail`, rendered by `HelpdeskPortal.portal_ticket_detail` at `/my/ticket/<int:ticket_id>`. It shows one of the member's tickets: title, status badge, callsign, priority, description and the message thread ("Communication History", `portal.message_thread`), where the member reads operator replies.

**Access control:** the controller redirects to `/my` when the ticket does not exist, belongs to another partner, or belongs to a different website than the current one, so a member can never read another member's ticket by guessing ids. A "Close Ticket" form (a POST with a CSRF token) appears only while the ticket is not closed.

**See also:** `hams_helpdesk:COMM_helpdesk_portal_close`, `hams_helpdesk:COMM_helpdesk_portal_tour`.

### `hams_helpdesk:COMM_helpdesk_portal_new` [@ANCHOR: hams_helpdesk:COMM_helpdesk_portal_new]

**Purpose:** the QWeb template `portal_ticket_new`, the "Submit New Ticket" form. `HelpdeskPortal.portal_ticket_new` renders it at `/my/tickets/new`, and the form posts (CSRF protected) to `/my/tickets/submit`.

**Fields:**

| Field | Meaning |
|---|---|
| `name` | Subject, required. A submit without it redirects back to the form. |
| `callsign` | Prefilled from the member's partner; optional, since someone studying for a licence has none yet. |
| `ticket_type` | Category. The list excludes `csam_enticement_trafficking` and `server_exception`, which are internal flags; the value is re-validated against the model's selection on both the GET and the POST, and an unknown value falls back to `general`. A link may preselect one with `?ticket_type=`. |
| `description` | Free text. |

**See also:** `hams_helpdesk:COMM_portal_ticket_new` (the controller entry), `hams_helpdesk:COMM_helpdesk_portal_tour`.

### `hams_helpdesk:COMM_helpdesk_portal_tour` [@ANCHOR: hams_helpdesk:COMM_helpdesk_portal_tour]

**Purpose:** the browser tour `helpdesk_portal_tour` (`static/src/tours/helpdesk_portal_tour.js`), the UI test of the member journey, and the test cited by the audit-ignore tags on the portal templates.

**Steps:** from `/my/tickets` it clicks New Ticket, fills subject `Tour Ticket`, callsign `K1AAA` and a description, submits, checks that the detail page shows callsign `K1AAA`, closes the ticket and checks the closed status badge. Its steps also carry the test citations for the list, new and detail anchors.

**Notes:** the matching Python test method in `tests/test_ui_tours.py` is currently a commented-out stub, so the tour itself is the only coverage; whether it is started anywhere else is unknown (to-do: confirm and re-enable the Python wrapper).

**See also:** `hams_helpdesk:COMM_helpdesk_portal_list`, `hams_helpdesk:COMM_helpdesk_portal_new`, `hams_helpdesk:COMM_helpdesk_portal_detail`.

### `hams_helpdesk:COMM_helpdesk_portal_close` [@ANCHOR: hams_helpdesk:COMM_helpdesk_portal_close]

**Purpose:** the model method `hams_helpdesk.ticket.action_portal_close(self)` (`models/helpdesk_ticket.py`, `@api.private`), which lets a member close their own ticket. The route `/my/ticket/<id>/close` (POST, CSRF, `auth="user"`) calls it and then redirects to the detail page.

**Arguments and returns:** a single-record recordset (`ensure_one`); it returns nothing.

**Errors:** `AccessError` ("You are not authorized to close this ticket.") unless the caller's partner is the ticket's partner or the caller is in `hams_helpdesk.group_helpdesk_user`. A ticket already closed is left alone, so repeating the call is harmless.

**Side effects:** it switches to the helpdesk service environment, writes `stage = closed` with `mail_notrack`, and posts "Ticket closed by customer." on the ticket. Portal users cannot write `stage` themselves, so the service account is the only path.

**See also:** `hams_helpdesk:COMM_helpdesk_action_close` (the staff button), `hams_helpdesk:COMM_portal_ticket_close` (the route), and the test `tests/test_helpdesk_core.py` citing `COMM_helpdesk_portal_close`.

### `hams_helpdesk:member_menu_item` [@ANCHOR: hams_helpdesk:member_menu_item]

**Purpose:** the "Support" entry in the website menu, a `website.menu` record `menu_hams_support` in `data/member_menu_data.xml`. It opens `/my/tickets`.

**Definition:** name `Support`, url `/my/tickets`, parent `website.main_menu`, sequence 93, and `group_ids` set to the portal and internal-user groups. The website menu record rule hides an item that has groups from everyone outside them, in both the top bar and the mobile menu, so visitors who are not signed in never see it.

**Notes:** the `ham_init` module places the item under Learn next to Help (its `HAMS_MENU_GROUPS`), and checks it in `ham_init/tests/test_member_menu_items.py`. The module's own test is `tests/test_member_menu_item.py` and the anchor `test_hams_helpdesk_support_menu_item`.

**See also:** `hams_helpdesk:COMM_helpdesk_portal_list`.
