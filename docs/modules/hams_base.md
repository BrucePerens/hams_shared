# HAMS Base Module

## Technical Specification

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

### 1. Public Email-Policy Pages
Public, unauthenticated compliance page at `/email-policy` listing the kinds of email the platform sends (anti-spam and legal disclosure). While outgoing email is limited (for example an Amazon SES sandbox), `/email-policy` and `/unsubscribe` also show the plain-text notice from the `hams_base.email_delivery_notice` system parameter (empty by default) [@ANCHOR: hams_base:website_email_delivery_notice].
* **Email Policy Disclosure Page:** [@ANCHOR: hams_base:COMM_email_policy_route]

### 2. Self-Service Unsubscribe / Account Lockout
Lets a member deactivate their own account: `/unsubscribe` offers a link to email preferences or, for a logged-in user, a total account lockout, and `POST /unsubscribe/lockout` (logged-in users only) deactivates the account, logs the session out and can be reversed only by an administrator (routed through a dedicated service account, since Odoo's own `res.users.write()` refuses to let a user deactivate their own live session).
* **Unsubscribe Landing Page:** `hams_base:COMM_unsubscribe_page_route`

* **Self-Lockout Action:** `hams_base:COMM_unsubscribe_lockout_route`

### 3. DMARC RUA Report Ingestion
Parses DMARC aggregate reports arriving as email attachments at the module's own mail alias -- raw XML, `.zip`, or `.gz`, all from an unauthenticated external sender by design (DMARC's own `rua=` address must be publicly published in DNS), so every step fails closed rather than raising into Odoo's mail gateway: a malformed or oversized attachment (decompressed size capped at 50 MB) is logged and skipped, and an email with no parseable attachment is still stored as a report named "Unparsed Email: <subject>". Mail arrives at the dedicated `dmarc-reports@` alias, the address in the suggested DMARC record's `rua=` tag.
* **Mail-Gateway Entry Point:** `hams_base:COMM_dmarc_message_new`

* **Attachment Extraction (zip/gzip/raw, with a decompression-size cap):** `hams_base:COMM_process_dmarc_attachment`

* **XML Parsing into `hams_base.dmarc.report`/`hams_base.dmarc.record`:** `hams_base:COMM_parse_dmarc_xml`

* **Safe Integer Coercion for Attacker-Supplied XML Fields:** `hams_base:COMM_safe_int`

### 4. Mail Routing Noise Filtering
For mail addressed to the bounce alias (`mail.bounce.alias`, default `auto-mail-failure`), `not-read@` or `postmaster@` (matched on the recipient's exact local part), drops any message whose subject or body mentions "unsubscribe" (logged only; no subscription is changed, the sender must use `/unsubscribe`) and any vacation reply (subject contains "out of office", "vacation" or "auto-reply"). A recognized bounce then goes on to Odoo's normal bounce processing. Any other message to `not-read@` is dropped, whereas any other message to the bounce alias or `postmaster@` continues to normal routing, so a genuine `postmaster@` inquiry still reaches its alias.
* **`mail.thread.message_route()` Override:** `hams_base:COMM_message_route`

### 5. Compliance Settings
* **SPF/DMARC TXT Record Preview (domain from `mail.catchall.domain`, else `web.base.url`; the SPF record is a template naming an example provider to be edited, the DMARC record is `p=quarantine` with reports to `dmarc-reports@<domain>` and is shown only when "Enable Custom DMARC" is on):** [@ANCHOR: hams_base:COMM_compute_dns_records]

### 6. Bounce and Account-Change Notifications
* **Club-Officer Bounce Notification (after Odoo's own bounce handling, posts a "Bounce Alert" chatter message on each club of the bouncing partner -- its `club_ids` when another module provides that field, otherwise its parent company -- skipping a club whose own email is the bouncing address; one club's failure does not stop the others):** [@ANCHOR: hams_base:COMM_message_receive_bounce]

* **Old-Address Security Alert on Email/Login Change (best effort: a send failure is logged and the change is still saved):** `hams_base:COMM_res_users_write`

### 7. Database-Manager and Error-Body Lockdown
Unless the server runs in developer mode (`--dev`), every `/web/database/*` route answers 404, and the JSON-RPC and JSON-2 dispatchers never put a Python traceback in a response; an unexpected exception's message is replaced by the generic "Odoo Server Error" (and is still logged in full on the server), while expected errors (user errors, access denied, validation, 404, expired session) keep their message. This is in code so that it holds for every installation, not only ours.
* **Developer-Mode Check:** `hams_base:developer_mode_check`

* **Error-Body Scrubbing:** `hams_base:scrub_error_body`

### 8. Session Validation Before Website Matching
`ir.http._match` runs Odoo's session check before the website module reads records, so a session whose user has since been deleted is logged out and served as the public user instead of getting a bare 403 on every page.
* **Validate Session Before Website Match:** `hams_base_validate_session_before_website_match`

### 9. Visitor-Tracking Deduplication
`website.visitor._handle_webpage_dispatch` first checks (a read, which does not conflict) whether this visitor already has a `website.track` row for the same URL within the last 30 minutes, and if so writes nothing. This avoids the concurrent write conflicts (serialization failures, retried requests) that sessions sharing one visitor row otherwise cause, and the unbounded growth of track rows.
* **Track Deduplication:** `hams_base:visitor_track_dedup`

## External Dependencies

* `zero_sudo` for the account-lockout and mail service accounts used by the unsubscribe/bounce flows.

## Cross-Module Interfaces

None beyond the shared `res.partner`/`res.users`/`mail.thread` core models this module extends.

## Anchor reference

### `hams_base:COMM_unsubscribe_page_route` [@ANCHOR: hams_base:COMM_unsubscribe_page_route]

**Purpose.** The public landing page for members who want to stop receiving email or close their account. It is the route `GET /unsubscribe`, method `unsubscribe_page(self, **kw)` of `UnsubscribeController` (`hams_base/controllers/unsubscribe.py`).

**Interface.** `type='http'`, `auth='public'`, `website=True`; extra query parameters are accepted and ignored. It returns the rendered QWeb template `hams_base.unsubscribe_page_template`, passing `is_public_user` (true when the visitor is the anonymous public user) so that a logged-in member is offered the total lockout and a visitor only the link to email preferences.

**Special concerns.** Read-only: it changes nothing and needs no CSRF token. It also shows the `hams_base.email_delivery_notice` system parameter when set.

**See also.** `hams_base:COMM_unsubscribe_lockout_route`; test `hams_base/tests/test_unsubscribe.py`.

### `hams_base:COMM_unsubscribe_lockout_route` [@ANCHOR: hams_base:COMM_unsubscribe_lockout_route]

**Purpose.** Lets a signed-in member deactivate their own account. It is `POST /unsubscribe/lockout`, method `unsubscribe_lockout(self, **kw)` in `hams_base/controllers/unsubscribe.py`.

**Interface.** `type='http'`, `auth='user'`, `website=True`, `methods=['POST']` (so Odoo's CSRF check applies). Returns the template `hams_base.unsubscribe_lockout_success`; for the public user it redirects to `/` and does nothing.

**Behaviour and side effects.** It resolves the service account `zero_sudo.user_lockout_service_internal` with `_get_service_uid`, writes `active = False` on the member through that account (`with_user`, no `sudo`), then ends the session with `session.logout(keep_db=True)`. The service account is needed because core `res.users.write()` refuses a user deactivating their own live uid, and `sudo()` does not change the uid. Only an administrator can reverse it.

**Errors.** A missing or disabled service account raises `AccessError` from `_get_service_uid`.

**See also.** `hams_base:COMM_unsubscribe_page_route`; test `test_unsubscribe.py`.

### `hams_base:COMM_dmarc_message_new` [@ANCHOR: hams_base:COMM_dmarc_message_new]

**Purpose.** The mail-gateway entry point for DMARC aggregate reports: Odoo calls it when mail arrives at the module's alias bound to `hams_base.dmarc.report`.

**Signature.** `@public_rpc @api.model message_new(self, msg_dict, custom_values=None)`. `msg_dict` is Odoo's parsed message (`attachments` as `(name, data)` pairs, `subject`, `body`, `author_id`, `email_from`); `custom_values` are passed to the default creation.

**Returns.** A `hams_base.dmarc.report` record. For each attachment it calls `process_dmarc_attachment`; the first one that parses becomes the report and the email is posted on it as a comment. With no parseable attachment it falls back to `super().message_new` and stores the mail as a report named "Unparsed Email: <subject>".

**Special concerns.** The sender is unauthenticated by design (DMARC `rua=` addresses are public), so nothing here raises on bad input: malformed data is logged and the mail is kept as an unparsed record.

**See also.** `hams_base:COMM_process_dmarc_attachment`, `hams_base:COMM_parse_dmarc_xml`; test `tests/test_dmarc_parser.py`.

### `hams_base:COMM_process_dmarc_attachment` [@ANCHOR: hams_base:COMM_process_dmarc_attachment]

**Purpose.** Turns one email attachment into a stored DMARC report, whether it is raw XML, a `.zip` or a `.gz`.

**Signature.** `@api.private @api.model process_dmarc_attachment(self, attachment_name, attachment_data)`. `attachment_name` (str) selects the handler by suffix (`.zip`, `.gz`, `.xml`); `attachment_data` is the already-decoded content from Odoo's mail gateway (`bytes`, or `str` which is UTF-8 encoded), not base64.

**Returns.** The `hams_base.dmarc.report` record, or `False` when nothing usable was found.

**Errors and limits.** It never raises: any extraction failure is logged and returns `False`. The decompressed size is capped at `_MAX_DECOMPRESSED_BYTES` (50 MB) by a bounded `read(cap + 1)`, for zip entries too, so a zip bomb that lies about its size cannot exhaust memory; an over-cap file is refused with a warning. For a zip only the first `.xml` entry is read.

**See also.** `hams_base:COMM_parse_dmarc_xml`, `hams_base:COMM_dmarc_message_new`; test `tests/test_dmarc_parser.py`.

### `hams_base:COMM_parse_dmarc_xml` [@ANCHOR: hams_base:COMM_parse_dmarc_xml]

**Purpose.** Parses the XML of a DMARC aggregate report into a `hams_base.dmarc.report` and its `hams_base.dmarc.record` rows (one per source IP).

**Signature.** `@api.model _parse_dmarc_xml(self, xml_content)`; `xml_content` is bytes or str of the report.

**Returns.** The report record, an existing one when a report with the same `report_id` is already stored, or `False` when the XML is malformed or lacks `report_metadata` or `policy_published`.

**Fields read.** `org_name`, `email`, `report_id`, `date_range` begin/end (epoch seconds, converted with `_epoch_to_naive_utc`), and from the policy `domain`, `adkim`, `aspf`, `p`, `sp`, `pct` (default 100). Per record: `source_ip`, `count`, `disposition`, and the evaluated DKIM and SPF results. Integer fields go through `_safe_int`.

**Special concerns.** Deduplication only applies when a `report_id` exists; a report without one is always stored (searching `IS NULL` would drop distinct reports). The XML comes from an unauthenticated sender, so a parse error returns `False` after a warning.

**See also.** `hams_base:COMM_safe_int`; test `tests/test_dmarc_parser.py`.

### `hams_base:COMM_safe_int` [@ANCHOR: hams_base:COMM_safe_int]

**Purpose.** A defensive integer conversion for numbers taken from attacker-supplied DMARC XML (`<begin>`, `<end>`, `<pct>`, `<count>`), so one bad value cannot abort a whole report.

**Signature.** `_safe_int(text, default=0)` in `hams_base/models/dmarc_report.py`, a plain module-level function. `text` is any value (usually `str` or `None` from `findtext`); `default` is the result on failure.

**Returns.** `int(text)`, or `default` when `int()` raises `TypeError` (for `None`) or `ValueError` (for non-numeric text). Callers pass 0 for counts and dates, and 100 for `pct`.

**Special concerns.** Pure and side-effect free; it does not clamp ranges, so large values still reach the database (dates are range-checked separately by `_epoch_to_naive_utc`, which drops years outside the supported range).

**See also.** `hams_base:COMM_parse_dmarc_xml`; test `tests/test_dmarc_parser.py`.

### `hams_base:COMM_message_route` [@ANCHOR: hams_base:COMM_message_route]

**Purpose.** Keeps automatic mail noise out of the system. It overrides `mail.thread.message_route()` to filter mail sent to the bounce alias, `not-read@` and `postmaster@`.

**Signature.** `@public_rpc @api.model message_route(self, message, message_dict, model=None, thread_id=None, custom_values=None)`; arguments as Odoo's mail gateway. Returns a list of routes; `[]` means the message is dropped.

**Behaviour.** The bounce alias is the system parameter `mail.bounce.alias` (default `auto-mail-failure`). The recipient's local part must match exactly, parsed with `email.utils.getaddresses`, so `postmaster-notify@...` does not match. On a match: a subject or body mentioning "unsubscribe" is logged and dropped; a subject with "out of office", "vacation" or "auto-reply" is dropped; a recognised bounce (`bounced_email` or `bounced_partner`) goes to normal processing; any other message to `not-read@` is dropped; anything else to `postmaster@` falls through to `super()` and becomes an incident.

**See also.** Test `tests/test_mail_thread.py`.

### `hams_base:COMM_res_users_write` [@ANCHOR: hams_base:COMM_res_users_write]

**Purpose.** Warns the old email address when an account's email or login changes, a defence against account takeover. It overrides `res.users.write()` in `hams_base/models/res_users.py`.

**Signature.** `write(self, vals)`; returns whatever `super().write` returns. A `login` value is first normalised (case-insensitive logins).

**Behaviour.** Before the write it records each user's old `email or login`. After a successful write, for each user whose address changed (case-insensitive compare) it sends an HTML "Security Alert" email to the old address, built with escaped name and addresses and sent through the facility service account `zero_sudo.odoo_facility_service_internal` as `mail.mail`.

**Errors and special concerns.** Best effort: each user is processed in its own savepoint (a missing service account would otherwise poison the transaction), and any send failure is logged and swallowed, so the change is still saved. The message goes only to the old address. Performance cost is one mail per changed user.

**See also.** Test `tests/test_res_users_email_change_alert.py`.

### `hams_base:developer_mode_check` [@ANCHOR: hams_base:developer_mode_check]

**Purpose.** Tells whether the server runs in developer mode, which is the only case in which the database-manager routes and full error bodies are allowed.

**Signature.** `developer_mode()` in `hams_base/controllers/database_lockdown.py`; no arguments; returns `bool(tools.config.get("dev_mode"))`, true when the server was started with `--dev`.

**Special concerns.** Reads the process configuration, not a request or database value, so a client cannot influence it and it cannot be changed without restarting the server. Production must never use `--dev`. It is used by `DatabaseManagerLockdown._refuse` and by `scrub_error_body`.

**See also.** `hams_base:scrub_error_body`; test `tests/test_database_lockdown.py`.

### `hams_base:scrub_error_body` [@ANCHOR: hams_base:scrub_error_body]

**Purpose.** Stops error responses from leaking internals. The JSON-RPC and JSON-2 dispatchers pass every error body through it before sending.

**Signature.** `scrub_error_body(data, exc)`: `data` is the dict from `odoo.http.serialize_exception`; `exc` is the exception. Returns a new dict.

**Behaviour.** In developer mode `data` is returned unchanged. Otherwise the `debug` field (the traceback) is always emptied. If `exc` is not one of the expected user-facing kinds (`UserError`, `AccessDenied`, `RedirectWarning`, `SessionExpiredException`, werkzeug `HTTPException`), the name, message, arguments and context are replaced by the generic "Odoo Server Error" because they may hold SQL, paths or secrets. The real error is still logged on the server.

**See also.** `hams_base:developer_mode_check`; test `tests/test_database_lockdown.py`.

### `hams_base_validate_session_before_website_match` [@ANCHOR: hams_base_validate_session_before_website_match]

**Purpose.** Fixes a bare 403 on every page for a visitor whose session belongs to a deleted user. It overrides the classmethod `ir.http._match(cls, path)` in `hams_base/models/ir_http.py`.

**Behaviour.** If the request has a session uid, it runs `security.check_session(request.session, request.env)` before website URL matching reads records. When the session is invalid it logs the session out (`logout(keep_db=True)`) and rebuilds `request.env` with no uid, so the visitor is served as the public user. It then calls `super()._match(path)` and returns its result.

**Special concerns.** `request` is deliberately not passed to `check_session` because the cursor is still read-only; the later check in `_authenticate_explicit` repeats it, as a cache hit. Seen in production as repeated "Access Denied by ACLs" for a nonexistent uid.

**See also.** Story `hams_base/docs/stories/stale_session_handling.md`; test `tests/test_stale_session_match.py`.

### `hams_base:visitor_track_dedup` [@ANCHOR: hams_base:visitor_track_dedup]

**Purpose.** Avoids database write conflicts and unbounded growth of `website.track` rows when many requests share one visitor session. It overrides `website.visitor._handle_webpage_dispatch(self, website_page)` (`hams_base/models/website_visitor.py`).

**Behaviour.** First it calls `_track_exists_for_this_request()`: a read-only SQL query for a `website_track` row of this visitor's access token with the same URL within `TRACK_DEDUP_MINUTES` (30). If one exists it returns `None` and writes nothing; otherwise it calls `super()`. A token error (`ValueError`) counts as no track.

**Why.** Core upserts the visitor row and inserts a track on every tracked page response; concurrent requests on one session caused PostgreSQL serialization failures and retried requests, ending in 500s under load.

**See also.** Test `tests/test_visitor_track_dedup.py`; the 30-minute window matches core's own `_add_tracking`.
