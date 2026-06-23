# MASTER 01: Security & Zero-Sudo Architecture

## Status
Accepted (Consolidates ADRs 0002, 0005, 0013, 0039, 0062, 0064, 0068, 0069, 0070)

## Context & Philosophy
Odoo's native `.sudo()` method grants absolute database rights, bypassing Access Control Lists (ACLs) and Record Rules. This is a dangerous anti-pattern that frequently leads to privilege escalation vulnerabilities. The platform strictly enforces a Zero-Sudo architecture to ensure least-privilege execution across all boundaries.

## Decisions & Mandates

### 1. The Service Account Pattern
When elevated privileges are required, the system MUST NOT use `.sudo()`. Instead:
1. Identify or create a specifically crafted Service Account (e.g., `user_dns_api_service`).
2. Retrieve its UID securely using the centralized security utility.
3. Execute the operation using the `with_user(svc_uid)` impersonation idiom.

**Separation of Privilege (Micro-Service Accounts):** Disparate permissions MUST NOT be bundled into monolithic, omnipotent service accounts, and you MUST NOT fall back to `base.user_admin` for automated tasks. When a brief privilege-raise is necessary to cross a domain boundary, the system MUST temporarily assume a highly specialized micro-service account (like `mail_service_internal` or `config_service_internal`) dedicated exclusively to that exact flow.

**The Framework ACL Tax (Micro-Service Caveat):** By deliberately removing the monolithic `base.group_user` from Service Accounts to secure them, you will expose hidden core framework dependencies. If your service account interacts with `res.users`, the ORM will silently attempt to cascade reads to underlying tables. You MUST explicitly grant your Service Account microscopic read/write ACLs in your `ir.model.access.csv` to prevent `AccessError` transaction crashes. For interactions requiring deep ERP facilities (like `mail.thread`), you must explicitly assume the special `odoo_facility_service_internal` account.

**The ORM Cascade Bypass (Headless Mutations):** When a Service Account performs automated background mutations on core identities (e.g., updating `res.users` during a sync), Odoo's native `mail.thread` tracking will cascade to related models (like `res.partner`), triggering implicit reads on protected fields (like `bank_ids`). Because Service Accounts are financially sandboxed (ADR-16), this causes fatal `AccessError`s. To prevent this, automated headless mutations on existing records MUST append `.with_context(mail_notrack=True)` to the execution.
**🚨 CRITICAL ODOO 17+ TRAP (`prefetch_fields=False`):** You MUST NEVER pass `prefetch_fields=False` to a `.create()` method or use it on models without `mail.thread`. In modern Odoo, forcing `prefetch_fields=False` during record creation breaks the ORM's internal caching loop during the `RETURNING id` phase, causing a fatal `KeyError: 'record'` inside `odoo/orm/models.py`.
* **The Poisoned Context Cascade:** If an upstream module applies `prefetch_fields=False` during a `.write()` operation (e.g., `user_websites`), that poisoned context will propagate down to any underlying models triggered by hooks (e.g., `cloudflare.purge.queue`). Downstream utility models MUST actively sanitize their context by stripping `prefetch_fields` (`clean_ctx.pop('prefetch_fields', None)`) before calling `.create()` to immunize themselves against upstream crashes. Use `.with_context(mail_notrack=True)` exclusively.

**The Mechanical God-Mode Block:** To ensure downstream Open Source compatibility without relying on centralized whitelists, the `_get_service_uid` method mathematically bans any Service Account from possessing `base.group_system` or `base.group_erp_manager`. Any module across any repository layer can define a Service Account, but they are strictly forced to explicitly map their Micro-Privileges in their `ir.model.access.csv`, fulfilling the Zero-Sudo mandate transparently.

### 2. Service Account Web Isolation
To prevent leaked daemon credentials from being used interactively:
* All Service Accounts MUST be flagged with `is_service_account=True`.
* The `web_login` controller intercepts logins and instantly destroys the session if a Service Account attempts to access the frontend UI.

### 3. Centralized Security Utility
All allowed privilege escalations (such as resolving XML IDs or fetching configuration parameters) MUST route through `zero_sudo.security.utils`:
* `_get_service_uid(xml_id)`: Safely resolves Service Account IDs.
* `_get_system_param(key)`: Fetches configuration parameters safely. Implements a **Mechanical Secret Block** that violently rejects any key containing restricted cryptographic substrings (`secret`, `key`, `password`, `token`, `auth`, `crypt`, `cert`) to mathematically prevent Server-Side Template Injection (SSTI) data exfiltration without impeding Open Source decentralization.

### 4. Persona Capability Limit & View Abstraction
We do not increase privilege beyond the capability of the persona requesting the data, unless there is absolutely no other way. Views (`_auto = False`) must be used preferentially rather than increasing privilege, when a view will work.

To present restricted, masked, or aggregated data to an unprivileged user (e.g., public directories, maps, or statistics), you MUST NOT use a Service Account to fetch the raw records and mask them in Python. Instead, create a PostgreSQL View (`_auto = False`) that strictly selects only the safe columns or applies SQL-level masking. Grant the public/portal persona read access exclusively to the View via `ir.model.access.csv`, and execute the read natively without privilege escalation. Sensitive data must never enter the WSGI worker's memory during an unprivileged request.

### 5. Strict Linter Bypass Confinement
Generic `# burn-ignore` tags are strictly prohibited. The bypass comment MUST specify the exact rule or pattern being bypassed (e.g., `# burn-ignore-financial`, `# audit-ignore-mail`, `# audit-ignore-search`). Furthermore, ANY bypassed line MUST include an inline comment cross-referencing the specific Semantic Anchor of the automated unit test that validates it.

### 6. OS-Level Daemon Restriction & Airgapped Spooling
To prevent Remote Code Execution (RCE) escalation from background tasks:
* **Chrooted Privilege De-escalation:** Daemons without network needs (e.g., Log Analyzers) MUST start as `root` to access `/var/log`, immediately `chroot`, drop all Linux kernel bounding capabilities, and de-escalate to `nobody:adm`.
* **Systemd Sandboxing:** Network daemons MUST be sandboxed via systemd (`ProtectSystem=strict`, `PrivateTmp=true`, `PrivateDevices=true`, `NoNewPrivileges=true`).
* **Airgapped Hardware Spooling:** If a sandboxed daemon needs hardware telemetry (like SMART disk health), a separate, highly privileged sidecar script MUST execute via a systemd `.timer` and write a serialized JSON file into a read-only spool directory for the unprivileged daemon to read.
