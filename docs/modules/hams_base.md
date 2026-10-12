# HAMS Base Module

## Technical Specification

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

### 1. Public Email-Policy Pages
Public, unauthenticated compliance page at `/email-policy` listing the kinds of email the platform sends (anti-spam and legal disclosure). While outgoing email is limited (for example an Amazon SES sandbox), `/email-policy` and `/unsubscribe` also show the plain-text notice from the `hams_base.email_delivery_notice` system parameter (empty by default) [@ANCHOR: hams_base:website_email_delivery_notice].
* **Email Policy Disclosure Page:** `[@ANCHOR: hams_base:COMM_email_policy_route]`

### 2. Self-Service Unsubscribe / Account Lockout
Lets a member deactivate their own account: `/unsubscribe` offers a link to email preferences or, for a logged-in user, a total account lockout, and `POST /unsubscribe/lockout` (logged-in users only) deactivates the account, logs the session out and can be reversed only by an administrator (routed through a dedicated service account, since Odoo's own `res.users.write()` refuses to let a user deactivate their own live session).
* **Unsubscribe Landing Page:** `[@ANCHOR: hams_base:COMM_unsubscribe_page_route]`

* **Self-Lockout Action:** `[@ANCHOR: hams_base:COMM_unsubscribe_lockout_route]`

### 3. DMARC RUA Report Ingestion
Parses DMARC aggregate reports arriving as email attachments at the module's own mail alias -- raw XML, `.zip`, or `.gz`, all from an unauthenticated external sender by design (DMARC's own `rua=` address must be publicly published in DNS), so every step fails closed rather than raising into Odoo's mail gateway: a malformed or oversized attachment (decompressed size capped at 50 MB) is logged and skipped, and an email with no parseable attachment is still stored as a report named "Unparsed Email: <subject>". Mail arrives at the dedicated `dmarc-reports@` alias, the address in the suggested DMARC record's `rua=` tag.
* **Mail-Gateway Entry Point:** `[@ANCHOR: hams_base:COMM_dmarc_message_new]`

* **Attachment Extraction (zip/gzip/raw, with a decompression-size cap):** `[@ANCHOR: hams_base:COMM_process_dmarc_attachment]`

* **XML Parsing into `hams_base.dmarc.report`/`hams_base.dmarc.record`:** `[@ANCHOR: hams_base:COMM_parse_dmarc_xml]`

* **Safe Integer Coercion for Attacker-Supplied XML Fields:** `[@ANCHOR: hams_base:COMM_safe_int]`

### 4. Mail Routing Noise Filtering
For mail addressed to the bounce alias (`mail.bounce.alias`, default `auto-mail-failure`), `not-read@` or `postmaster@` (matched on the recipient's exact local part), drops any message whose subject or body mentions "unsubscribe" (logged only; no subscription is changed, the sender must use `/unsubscribe`) and any vacation reply (subject contains "out of office", "vacation" or "auto-reply"). A recognized bounce then goes on to Odoo's normal bounce processing. Any other message to `not-read@` is dropped, whereas any other message to the bounce alias or `postmaster@` continues to normal routing, so a genuine `postmaster@` inquiry still reaches its alias.
* **`mail.thread.message_route()` Override:** `[@ANCHOR: hams_base:COMM_message_route]`

### 5. Compliance Settings
* **SPF/DMARC TXT Record Preview (domain from `mail.catchall.domain`, else `web.base.url`; the SPF record is a template naming an example provider to be edited, the DMARC record is `p=quarantine` with reports to `dmarc-reports@<domain>` and is shown only when "Enable Custom DMARC" is on):** `[@ANCHOR: hams_base:COMM_compute_dns_records]`

### 6. Bounce and Account-Change Notifications
* **Club-Officer Bounce Notification (after Odoo's own bounce handling, posts a "Bounce Alert" chatter message on each club of the bouncing partner -- its `club_ids` when another module provides that field, otherwise its parent company -- skipping a club whose own email is the bouncing address; one club's failure does not stop the others):** `[@ANCHOR: hams_base:COMM_message_receive_bounce]`

* **Old-Address Security Alert on Email/Login Change (best effort: a send failure is logged and the change is still saved):** `[@ANCHOR: hams_base:COMM_res_users_write]`

### 7. Database-Manager and Error-Body Lockdown
Unless the server runs in developer mode (`--dev`), every `/web/database/*` route answers 404, and the JSON-RPC and JSON-2 dispatchers never put a Python traceback in a response; an unexpected exception's message is replaced by the generic "Odoo Server Error" (and is still logged in full on the server), while expected errors (user errors, access denied, validation, 404, expired session) keep their message. This is in code so that it holds for every installation, not only ours.
* **Developer-Mode Check:** `[@ANCHOR: hams_base:developer_mode_check]`

* **Error-Body Scrubbing:** `[@ANCHOR: hams_base:scrub_error_body]`

### 8. Session Validation Before Website Matching
`ir.http._match` runs Odoo's session check before the website module reads records, so a session whose user has since been deleted is logged out and served as the public user instead of getting a bare 403 on every page.
* **Validate Session Before Website Match:** `[@ANCHOR: hams_base_validate_session_before_website_match]`

### 9. Visitor-Tracking Deduplication
`website.visitor._handle_webpage_dispatch` first checks (a read, which does not conflict) whether this visitor already has a `website.track` row for the same URL within the last 30 minutes, and if so writes nothing. This avoids the concurrent write conflicts (serialization failures, retried requests) that sessions sharing one visitor row otherwise cause, and the unbounded growth of track rows.
* **Track Deduplication:** `[@ANCHOR: hams_base:visitor_track_dedup]`

## External Dependencies

* `zero_sudo` for the account-lockout and mail service accounts used by the unsubscribe/bounce flows.

## Cross-Module Interfaces

None beyond the shared `res.partner`/`res.users`/`mail.thread` core models this module extends.
