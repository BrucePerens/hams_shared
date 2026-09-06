# HAMS Base Module

## Technical Specification

### 1. Public Email-Policy Pages
Public, unauthenticated compliance pages required for anti-spam/legal disclosure.
* **Email Policy Disclosure Page:** `[@ANCHOR: hams_base:COMM_email_policy_route]`

### 2. Self-Service Unsubscribe / Account Lockout
Lets a member deactivate their own account (routed through a dedicated service account, since Odoo's own `res.users.write()` refuses to let a user deactivate their own live session).
* **Unsubscribe Landing Page:** `[@ANCHOR: hams_base:COMM_unsubscribe_page_route]`

* **Self-Lockout Action:** `[@ANCHOR: hams_base:COMM_unsubscribe_lockout_route]`

### 3. DMARC RUA Report Ingestion
Parses DMARC aggregate reports arriving as email attachments at the module's own mail alias -- raw XML, `.zip`, or `.gz`, all from an unauthenticated external sender by design (DMARC's own `rua=` address must be publicly published in DNS), so every step fails closed rather than raising into Odoo's mail gateway.
* **Mail-Gateway Entry Point:** `[@ANCHOR: hams_base:COMM_dmarc_message_new]`

* **Attachment Extraction (zip/gzip/raw, with a decompression-size cap):** `[@ANCHOR: hams_base:COMM_process_dmarc_attachment]`

* **XML Parsing into `hams_base.dmarc.report`/`hams_base.dmarc.record`:** `[@ANCHOR: hams_base:COMM_parse_dmarc_xml]`

* **Safe Integer Coercion for Attacker-Supplied XML Fields:** `[@ANCHOR: hams_base:COMM_safe_int]`

### 4. Mail Routing Noise Filtering
Drops vacation replies, unsubscribe-intent replies, and generic garbage sent to the bounce/`not-read@`/`postmaster@` aliases, while still letting a genuine `postmaster@` inquiry fall through to normal routing.
* **`mail.thread.message_route()` Override:** `[@ANCHOR: hams_base:COMM_message_route]`

### 5. Compliance Settings
* **SPF/DMARC TXT Record Preview (derived from the configured catchall domain):** `[@ANCHOR: hams_base:COMM_compute_dns_records]`

### 6. Bounce and Account-Change Notifications
* **Club-Officer Bounce Notification:** `[@ANCHOR: hams_base:COMM_message_receive_bounce]`

* **Old-Address Security Alert on Email/Login Change:** `[@ANCHOR: hams_base:COMM_res_users_write]`

## External Dependencies

* `zero_sudo` for the account-lockout and mail service accounts used by the unsubscribe/bounce flows.

## Cross-Module Interfaces

None beyond the shared `res.partner`/`res.users`/`mail.thread` core models this module extends.
