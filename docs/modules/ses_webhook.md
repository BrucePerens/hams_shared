# SES Webhook Module

## Technical Specification

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

### 1. Amazon SNS/SES Webhook Ingestion
Receives Amazon SNS (Simple Notification Service) webhooks for incoming SES (Simple Email Service) emails and event notifications (bounce/complaint) at the public endpoint `POST /mail/webhook/sns?token=<secret_token>`. It first checks the per-domain secret token (a missing or unknown token gets 403 and is not logged, since no domain exists to attach a log to), then the body (empty or non-JSON or non-object gets 400), then a real AWS SNS message signature (two independent layers, both required; a valid token with a bad signature is logged as `rejected_signature` and refused). Only then does it dispatch: it confirms an SNS subscription only for an `https://sns.<region>.amazonaws.com/` address, treats a Notification with a raw email as inbound mail passed to Odoo's `mail.thread.message_process` for the domain's company, and logs a Notification with no email content as `ignored`. A processing error is logged as `failed` yet answered 200, because SNS retries any non-2xx response. Each request leaves a `ses.webhook.log` row with status `success`, `failed`, `ignored`, `rejected_subscribe_url` or `rejected_signature`.
* **SES Event Notification Handling (Bounce/Complaint Suppression; every complaint, and a bounce only when SES marks it `Permanent`, adds the address to Odoo's `mail.blacklist` so later outbound mail is suppressed -- a transient bounce such as a full mailbox is not suppressed):** `ses_webhook:COMM_handle_ses_event_notification`
* **AWS SNS Signature Verification (fails closed on a missing field, a certificate host that is not a real `sns.<region>.amazonaws.com` host, an unusable certificate, an unsupported `SignatureVersion`, or a signed `Timestamp` more than 15 minutes from now):** `ses_webhook:COMM_verify_sns_signature`
* **Signing Certificate Fetch/Cache:** `ses_webhook:COMM_fetch_sns_signing_cert`
* Each of the three has its own entry under "SNS anchor reference" at the end of this file.

### 2. Multi-Tenant Domain Configuration
`ses.webhook.domain` maps an inbound domain to a tenant company and a secret webhook token, keeping a dedicated cross-tenant service account's `company_ids` in sync so `with_company()` never raises for a newly-configured tenant.
* **Webhook URL Compute:** `ses_webhook:COMM_compute_webhook_url`

* **Domain Create:** `ses_webhook:COMM_domain_create`

* **Domain Write (Company Reassignment):** `ses_webhook:COMM_domain_write`

* **Service Account Company Sync:** `ses_webhook:COMM_sync_service_account_companies`

### 3. Log and Pending-Submission Retention
Version 1.0 truncated `ses.webhook.log` (30-day window) and `ses.webhook.pending_submission` (7-day window, since it holds actual unauthenticated email content) with their own scheduled actions. Version 1.1 retired both truncation jobs and keeps the rows forever (the platform's "retain data forever" policy); the anchors below remain as the record of those two jobs, and the 1.1 post-migration removes the cron records from upgraded databases.
* **Webhook Log Truncation Cron:** `ses_webhook:COMM_cron_truncate_logs`

* **Pending Submission Truncation Cron:** `ses_webhook:COMM_cron_truncate_pending_submissions`

### 4. Registration-Gated Sender Handling
Before `message_process` is called, the sender address is matched to a user. An inbound email from a sender that doesn't match a registered user is never silently misattributed nor silently dropped -- it is stored as a `ses.webhook.pending_submission`, the sender is emailed a link to `/web/signup` asking them to register with the same address and resend, and the log status is `ignored`. The pending record's `consumed` flag is reserved for a future flow that links a newly registered user to the held email; nothing sets it yet.
* **Pending Submission Display Name:** `ses_webhook:COMM_pending_submission_compute_name`

* **Pending Submission Creation and Registration Nudge:** `ses_webhook:COMM_create_and_notify`

## External Dependencies

* `zero_sudo` for the cross-tenant service account and system-parameter access.

## Cross-Module Interfaces

None beyond the shared `mail.thread`/`mail.blacklist` core models this module's webhook handler drives.

## Anchor reference

### `ses_webhook:COMM_compute_webhook_url` [@ANCHOR: ses_webhook:COMM_compute_webhook_url]

**Purpose.** Computes the URL an operator pastes into the Amazon SNS subscription for a tenant domain, so that notifications reach the right company's token.

**Where.** `_compute_webhook_url` on `ses.webhook.domain` (`ses_webhook/models/ses_webhook_domain.py`), `@api.depends('secret_token')`, a non-stored field `webhook_url`.

**Behaviour.** It reads the system parameter `web.base.url` through `zero_sudo.security.utils._get_system_param` (no `sudo()`, because ordinary internal users may view the record but have no access to `ir.config_parameter`), then sets `webhook_url = <base>/mail/webhook/sns?token=<secret_token>`. A record with no token gets an empty string.

**Special concerns.** The URL carries the secret token, so anyone who can view the domain record can see it; the token is the per-domain credential checked by the endpoint, and is unique (`UNIQUE(secret_token)`).

**See also.** `ses_webhook:COMM_domain_create`; test `ses_webhook/tests/test_ses_webhook.py`.

### `ses_webhook:COMM_domain_create` [@ANCHOR: ses_webhook:COMM_domain_create]

**Purpose.** Creates tenant domain configurations and makes sure the webhook's service account can act for the new tenant company.

**Signature.** `@api.model_create_multi create(self, vals_list)` on `ses.webhook.domain`. Each dict needs `name` (unique domain) and `company_id` (default: current company); `secret_token` defaults to `secrets.token_urlsafe(24)`. Returns the new records.

**Behaviour.** After `super().create` it calls `_sync_service_account_companies()` on the new records so `company_ids` of the `ses_webhook.user_ses_webhook_service_internal` account include every tenant company.

**Errors.** Unique-constraint errors for a duplicate name or token; `AccessError` from `_get_service_uid` if the service account is missing.

**See also.** `ses_webhook:COMM_sync_service_account_companies`, `ses_webhook:COMM_domain_write`; test `test_ses_webhook.py`.

### `ses_webhook:COMM_domain_write` [@ANCHOR: ses_webhook:COMM_domain_write]

**Purpose.** Keeps the service account's company membership correct when a domain is reassigned to another tenant.

**Signature.** `write(self, vals)` on `ses.webhook.domain`; returns `True`.

**Behaviour.** Calls `super().write(vals)`; only when `company_id` is in `vals` does it call `_sync_service_account_companies()`. Changing the name or rotating the secret token needs no sync. The sync only adds companies; it never removes a company from the service account, so a former tenant stays a member.

**Errors.** `AccessError` from the service-account lookup if the account is missing.

**Special concerns.** Without the membership, `with_company(domain.company_id)` inside `message_process()` raises `AccessError` for that tenant.

**See also.** `ses_webhook:COMM_sync_service_account_companies`; test `test_ses_webhook.py`.

### `ses_webhook:COMM_sync_service_account_companies` [@ANCHOR: ses_webhook:COMM_sync_service_account_companies]

**Purpose.** Grants the webhook's cross-tenant service account membership of every company that has a configured domain, so the webhook can process mail on behalf of any tenant.

**Signature.** `_sync_service_account_companies(self)` on `ses.webhook.domain`; no arguments, returns nothing.

**Behaviour.** It resolves `ses_webhook.user_ses_webhook_service_internal` with `_get_service_uid`, computes `missing = self.company_id - svc_user.company_ids` and, if any, writes `(4, id)` link commands onto the account's `company_ids`. It is idempotent and only grows the set: no count is hardcoded, so it follows tenant onboarding.

**Why.** Odoo checks `allowed_company_ids` against the acting user's `company_ids` for a non-superuser environment, so a missing company makes `with_company()` raise.

**Errors.** `AccessError` if the service account does not exist.

**See also.** `ses_webhook:COMM_domain_create`, `ses_webhook:COMM_domain_write`; test `test_ses_webhook.py`.

### `ses_webhook:COMM_cron_truncate_logs`

**Status.** Retired. In ses_webhook 1.0 this was `ses.webhook.log._cron_truncate_logs`, a daily scheduled action that deleted webhook log rows older than 30 days. In 1.1 the method and its cron `ir_cron_truncate_ses_webhook_logs` were removed under the "retain data forever" policy, so no code carries this anchor today.

**Upgrade path.** `ses_webhook/migrations/1.1/post-remove-truncate-crons.py` deletes the cron, its server action and its xmlids from upgraded databases with plain SQL; it is idempotent and skipped on a fresh install (`version` empty). It deletes no log rows.

**Guarantee.** The service account cannot `unlink` log rows.

**Verified by.** `ses_webhook/tests/test_purge_cron_migration.py` (the cron is gone, the method is not in `dir(log)`, the service account has no unlink access).

**TODO.** Remove this anchor from the doc and any citation list when the retired-anchor bookkeeping allows.

### `ses_webhook:COMM_cron_truncate_pending_submissions`

**Status.** Retired. In ses_webhook 1.0 this was `ses.webhook.pending_submission._cron_truncate_pending_submissions`, a daily action deleting pending submissions older than 7 days, chosen because those rows hold unauthenticated email content. In 1.1 it was removed under the "retain data forever" policy; no code carries the anchor today.

**Upgrade path.** `migrations/1.1/post-remove-truncate-crons.py` removes the cron `ir_cron_truncate_ses_webhook_pending_submissions`, its server action and xmlids; it deletes no rows and is idempotent.

**Consequence.** Pending submissions, including raw email content, are kept indefinitely, and the service account cannot delete them.

**Verified by.** `ses_webhook/tests/test_purge_cron_migration.py`.

**See also.** `ses_webhook:COMM_cron_truncate_logs`, `ses_webhook:COMM_create_and_notify`.

### `ses_webhook:COMM_pending_submission_compute_name` [@ANCHOR: ses_webhook:COMM_pending_submission_compute_name]

**Purpose.** Gives each pending submission a readable display name for list views and chatter.

**Where.** `_compute_name` on `ses.webhook.pending_submission` (`ses_webhook/models/ses_webhook_pending_submission.py`), `@api.depends('sender_email')`, stored in the `name` field.

**Behaviour.** For each record it sets `name` to "Pending submission from <sender_email>" (translated), or "New" when there is no sender address. No database access beyond the stored write, no side effects.

**Special concerns.** `sender_email` is required and read-only, so "New" appears only for an unsaved record. The email address is shown to anyone who can read the model.

**See also.** `ses_webhook:COMM_create_and_notify`; test `test_ses_webhook.py`.

### `ses_webhook:COMM_create_and_notify` [@ANCHOR: ses_webhook:COMM_create_and_notify]

**Purpose.** Handles an inbound email whose sender matches no registered user: it stores the message and sends the sender a registration link, so mail is neither misattributed nor silently dropped.

**Signature.** `@api.private @api.model create_and_notify(self, sender_email, raw_content, domain)`. `sender_email` (str): the sender address. `raw_content` (str): the original email text. `domain`: the `ses.webhook.domain` record that received it. Returns the new `ses.webhook.pending_submission`.

**Behaviour.** Creates the submission (with a random `token`, `consumed = False`), reads `web.base.url`, and sends a `mail.mail` "Registration required to file a request" to the sender with a link to `/web/signup`, from the tenant company's catchall or email address.

**Special concerns.** Runs as the already-elevated ses_webhook service account, no `sudo()`. The mail goes to an unauthenticated address, so the notice names only the domain. `consumed` is reserved for a future flow and is not yet set by any code.

**See also.** `ses_webhook:COMM_pending_submission_compute_name`; test `test_ses_webhook.py`.

## SNS anchor reference

### `ses_webhook:COMM_handle_ses_event_notification` [@ANCHOR: ses_webhook:COMM_handle_ses_event_notification]
`SesWebhookController._handle_ses_event_notification(self, notification_type, ses_message)` in `ses_webhook/controllers/webhook_api.py` suppresses future mail to addresses SES reports as bounced or complained about, by adding them to Odoo's own `mail.blacklist`, the list that `message_process()` and every outbound send already consult, so no new send-time check is needed. `receive_sns_webhook` calls it for a `Notification` whose inner `notificationType` is `Bounce` or `Complaint`, before the raw-email branch (such payloads carry no `content` field).

| Argument | Type | Meaning |
|---|---|---|
| `notification_type` | str | `Complaint` or `Bounce`; any other value suppresses nothing. |
| `ses_message` | dict | The parsed SES message from the SNS `Message` field: `complaint.complainedRecipients[].emailAddress` and `complaint.feedbackId`, or `bounce.bounceType`, `bounce.bouncedRecipients[].emailAddress` and `.diagnosticCode`. |

**Returns:** a `list` of the addresses suppressed (possibly empty); the caller logs its length. **Rules:** every complaint is suppressed at once, because even one is a real signal and repeat complaints trigger SES reputation action. A bounce is suppressed only when `bounceType == "Permanent"`; a `Transient` bounce (full mailbox, temporary block) is not, so a reachable recipient is not silenced. Entries without an `emailAddress` are skipped, and the blacklist note records the feedback id or diagnostic code.

**Security and side effects:** runs as the `ses_webhook.user_ses_webhook_service_internal` service account (`with_user`, never `sudo()`), writing `mail.blacklist` through `_add`. It is reached only after the token and signature checks have passed (see `ses_webhook:COMM_verify_sns_signature`). No explicit locks; adding an address already listed is harmless, so a retried delivery is safe.

**See also:** tests `test_24_webhook_bounce_permanent_blacklists_transient_does_not` and `test_34_forged_complaint_without_signature_does_not_blacklist`.

### `ses_webhook:COMM_verify_sns_signature` [@ANCHOR: ses_webhook:COMM_verify_sns_signature]
`_verify_sns_signature(payload)` is a module-level function in `ses_webhook/controllers/webhook_api.py`. It checks an AWS SNS message's `Signature` against the certificate named by its `SigningCertURL`, following AWS's HTTP(S) signing scheme: `SignatureVersion` 1 is SHA1withRSA and 2 is SHA256withRSA, both with PKCS#1 v1.5 padding. It is the second of two required layers, after the per-domain secret token.

| Argument | Type | Meaning |
|---|---|---|
| `payload` | dict | The parsed SNS JSON body; uses `Signature` (base64), `SigningCertURL`, `SignatureVersion` (default `"1"`), `Timestamp` and the fields that `_build_string_to_sign` signs for its `Type`. |

**Returns:** `True` only when everything checks out, otherwise `False`. It never raises: it fails closed on a missing `Signature` or `SigningCertURL`, a URL that does not match `_SNS_SIGNING_CERT_URL_RE` (`https://sns.<region>.amazonaws.com/SimpleNotificationService-<hex>.pem`), a `Type` it cannot build a string to sign for, an unparseable `Timestamp`, a `Timestamp` more than `_MAX_SNS_MESSAGE_AGE` (15 minutes) from now in either direction (replay defense), an unsupported `SignatureVersion`, a certificate that is unreachable, malformed, expired or not yet valid, and a bad signature. Any other exception is logged as a warning and treated as "not verified".

**Order and cost:** cheap checks run first, so a stale replay costs no network fetch; the certificate comes from `_fetch_sns_signing_cert`. `Timestamp` is itself signed, so an attacker cannot move it forward without breaking the signature. Called synchronously inside the request, with no locks. Retry-safe.

**See also:** tests `test_25b_signature_with_subject_field_accepted` and `test_26_signature_version_2_sha256_accepted`, `ses_webhook:build_string_to_sign`, and `ses_webhook:COMM_fetch_sns_signing_cert`.

### `ses_webhook:COMM_fetch_sns_signing_cert` [@ANCHOR: ses_webhook:COMM_fetch_sns_signing_cert]
`_fetch_sns_signing_cert(signing_cert_url)` in `ses_webhook/controllers/webhook_api.py` downloads an AWS SNS signing certificate and returns its raw PEM bytes. It is wrapped in `functools.lru_cache(maxsize=16)`, so each URL is fetched once per process. It is a separate small function so tests can patch it alone.

| Argument | Type | Meaning |
|---|---|---|
| `signing_cert_url` | str | The `SigningCertURL`; the caller must already have matched it against `_SNS_SIGNING_CERT_URL_RE`. |

**Returns:** `bytes`, the PEM of a certificate that parses. **Errors:** the SSRF-safe opener raises for a private, loopback or link-local address or a failed redirect hop; a network failure raises; `x509.load_pem_x509_certificate` raises `ValueError` on a body that is not a certificate. `lru_cache` never caches a raised exception, so a transient failure, or a 200 response carrying an error page, is retried on the next message rather than sticking for the life of the process.

**Side effects and security:** one HTTPS GET with a 10 second timeout through `urlopen_ssrf_safe(..., "ses_webhook_sns_signing_cert", https_only=True)`; the regex fixes the host name and the opener checks the address. Blocking network I/O inside the request. The cache is per worker process and holds no secrets.

**See also:** tests `test_33b_fetch_sns_signing_cert_does_not_cache_a_malformed_response` and `test_33c_fetch_sns_signing_cert_refuses_a_link_local_address`, and `ses_webhook:COMM_verify_sns_signature`.
