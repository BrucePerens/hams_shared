# SES Webhook Module

## Technical Specification

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

### 1. Amazon SNS/SES Webhook Ingestion
Receives Amazon SNS (Simple Notification Service) webhooks for incoming SES (Simple Email Service) emails and event notifications (bounce/complaint) at the public endpoint `POST /mail/webhook/sns?token=<secret_token>`. It first checks the per-domain secret token (a missing or unknown token gets 403 and is not logged, since no domain exists to attach a log to), then the body (empty or non-JSON or non-object gets 400), then a real AWS SNS message signature (two independent layers, both required; a valid token with a bad signature is logged as `rejected_signature` and refused). Only then does it dispatch: it confirms an SNS subscription only for an `https://sns.<region>.amazonaws.com/` address, treats a Notification with a raw email as inbound mail passed to Odoo's `mail.thread.message_process` for the domain's company, and logs a Notification with no email content as `ignored`. A processing error is logged as `failed` yet answered 200, because SNS retries any non-2xx response. Each request leaves a `ses.webhook.log` row with status `success`, `failed`, `ignored`, `rejected_subscribe_url` or `rejected_signature`.
* **SES Event Notification Handling (Bounce/Complaint Suppression; every complaint, and a bounce only when SES marks it `Permanent`, adds the address to Odoo's `mail.blacklist` so later outbound mail is suppressed -- a transient bounce such as a full mailbox is not suppressed):** `[@ANCHOR: ses_webhook:COMM_handle_ses_event_notification]`
* **AWS SNS Signature Verification (fails closed on a missing field, a certificate host that is not a real `sns.<region>.amazonaws.com` host, an unusable certificate, an unsupported `SignatureVersion`, or a signed `Timestamp` more than 15 minutes from now):** `[@ANCHOR: ses_webhook:COMM_verify_sns_signature]`
* **Signing Certificate Fetch/Cache:** `[@ANCHOR: ses_webhook:COMM_fetch_sns_signing_cert]`

### 2. Multi-Tenant Domain Configuration
`ses.webhook.domain` maps an inbound domain to a tenant company and a secret webhook token, keeping a dedicated cross-tenant service account's `company_ids` in sync so `with_company()` never raises for a newly-configured tenant.
* **Webhook URL Compute:** `[@ANCHOR: ses_webhook:COMM_compute_webhook_url]`

* **Domain Create:** `[@ANCHOR: ses_webhook:COMM_domain_create]`

* **Domain Write (Company Reassignment):** `[@ANCHOR: ses_webhook:COMM_domain_write]`

* **Service Account Company Sync:** `[@ANCHOR: ses_webhook:COMM_sync_service_account_companies]`

### 3. Log and Pending-Submission Retention
Both `ses.webhook.log` (30-day window) and `ses.webhook.pending_submission` (7-day window, since it holds actual unauthenticated email content) are truncated by their own scheduled actions.
* **Webhook Log Truncation Cron:** `[@ANCHOR: ses_webhook:COMM_cron_truncate_logs]`

* **Pending Submission Truncation Cron:** `[@ANCHOR: ses_webhook:COMM_cron_truncate_pending_submissions]`

### 4. Registration-Gated Sender Handling
Before `message_process` is called, the sender address is matched to a user. An inbound email from a sender that doesn't match a registered user is never silently misattributed nor silently dropped -- it is stored as a `ses.webhook.pending_submission`, the sender is emailed a link to `/web/signup` asking them to register with the same address and resend, and the log status is `ignored`. The pending record's `consumed` flag is reserved for a future flow that links a newly registered user to the held email; nothing sets it yet.
* **Pending Submission Display Name:** `[@ANCHOR: ses_webhook:COMM_pending_submission_compute_name]`

* **Pending Submission Creation and Registration Nudge:** `[@ANCHOR: ses_webhook:COMM_create_and_notify]`

## External Dependencies

* `zero_sudo` for the cross-tenant service account and system-parameter access.

## Cross-Module Interfaces

None beyond the shared `mail.thread`/`mail.blacklist` core models this module's webhook handler drives.
