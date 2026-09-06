# SES Webhook Module

## Technical Specification

### 1. Amazon SNS/SES Webhook Ingestion
Receives Amazon SNS webhooks for incoming SES emails and event notifications (bounce/complaint), validating a per-domain secret token before doing anything else.
* **SES Event Notification Handling (Bounce/Complaint Suppression):** `[@ANCHOR: ses_webhook:COMM_handle_ses_event_notification]`

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
Per `SES_WEBHOOK_SENDER_REGISTRATION.md`: an inbound email from a sender that doesn't match a registered user is never silently misattributed nor silently dropped -- it's held as a pending submission and the sender is nudged toward registering.
* **Pending Submission Display Name:** `[@ANCHOR: ses_webhook:COMM_pending_submission_compute_name]`

* **Pending Submission Creation and Registration Nudge:** `[@ANCHOR: ses_webhook:COMM_create_and_notify]`

## External Dependencies

* `zero_sudo` for the cross-tenant service account and system-parameter access.

## Cross-Module Interfaces

None beyond the shared `mail.thread`/`mail.blacklist` core models this module's webhook handler drives.
