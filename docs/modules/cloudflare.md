# Cloudflare Edge Orchestration (`cloudflare`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

This module acts as the command center for your Cloudflare CDN (Content Delivery Network, Cloudflare's worldwide edge cache) and Web Application Firewall (WAF). It automates edge caching, security, and IP bans across multiple websites, eliminating the need to manually manage these settings in the Cloudflare dashboard.

## 🌟 What It Does

* **Multi-Website Support:** Seamlessly manage multiple domains/zones from a single Odoo instance. Credentials and settings are isolated per website.
* **DNS & Routing:** Keep a list of DNS records in Odoo (`cloudflare.dns.record`; types A, AAAA, CNAME, TXT, NS). Editing a row changes nothing at Cloudflare until an administrator pushes it: a push shows a plan first, and applies exactly that plan. It creates and updates only the records Odoo has a row for, and deletes nothing except a record whose row is marked **Retire** (see Technical Documentation). Supports multi-tenant Custom Hostname SSL provisioning, allowing tenants to bring their own domains (`CloudflareRoutingDomain`, this module's extension of `edge.routing.domain`): when a domain mapping is created whose name equals a website's domain and that website has an API token and Zone ID, the module asks Cloudflare to create a custom hostname with a domain-validated certificate; with no matching website, provisioning is skipped. The SSL status stored in Odoo is set from Cloudflare's reply at creation and is refreshed only by calling `action_sync_ssl_status()` (administrators and service accounts only); no cron or button calls it. If Cloudflare refuses to delete a hostname, the local mapping is still deleted and a retry record appears under **Pending Hostname Deletes**.
* **Automated Static Caching & Purging:** It tells Cloudflare to cache Odoo's asset bundles (the combined CSS and JS served under `/web/assets`) for a full year, tagged `odoo-static-assets`. Images and downloads served by `/web/image` and `/web/content` are never edge-cached, because they can be private attachments; per-module `/static/` files do not pass through this hook and keep Odoo's own `Cache-Control`. A static-file change detector (`cloudflare.config.manager._trigger_edge_purge_static_assets`) queues an `odoo-static-assets` purge for every website with credentials when a static file is newer than the last recorded change, but no code path currently calls it at server start, so it does not run on boot by itself. The tag can be purged by hand with the Manual Cache Purge wizard.
* **Intelligent Content Invalidation:** Automatically enqueues purges for specific URLs when website pages, blog posts, or products are edited or deleted. A website menu change enqueues a purge of that website's whole cache, since menus appear on every page.
* **Advanced Caching & Rate Limiting:** Record Cloudflare Cache Rules and Rate Limits in Odoo. These are stored in Odoo only: the module has no Cloudflare API call that deploys them, so they have no effect on the edge until configured in Cloudflare itself.
* **WAF Management:** Build and deploy Cloudflare custom firewall rules from the Odoo backend. **Pull** replaces this website's rules in Odoo with the zone's current custom firewall ruleset; **Push** replaces the zone's whole custom firewall ruleset with the rules in Odoo, so a rule that exists only in Cloudflare is removed. No backup is taken on Push (see Configuration Backups).
* **Honeypot IP Banning:** Ban malicious IPs at the network edge. This module contains no honeypot trap itself; it provides `cloudflare.waf.ban_ip()` for honeypot features in other modules to call. Each ban becomes a Cloudflare IP Access Rule on the website's zone and has no expiry: it stays until an administrator lifts it.
* **Zero Trust & Access:** Provision and manage `cloudflared` tunnels directly from Odoo to secure internal resources. Zero Trust Policies can be recorded in Odoo, but like Cache Rules they are stored in Odoo only and are not pushed to Cloudflare.
* **Turnstile Integration:** Backend validator for Cloudflare's invisible Turnstile CAPTCHA. Without a Turnstile Secret configured for the website, every token is rejected.
* **Analytics Dashboard:** Client action dashboard intended for traffic, security, and performance analytics. It is currently a placeholder: it shows fixed sample numbers, not data fetched from Cloudflare.
* **Configuration Backups:** "Pre-Odoo" backups: when the module is installed, each website whose zone already has custom firewall rules gets a read-only snapshot of that ruleset's raw JSON, taken before those rules are imported into Odoo (the zone's state before Odoo took the rules over). Zones without existing rules instead receive the module's default rules. Backups are taken only at install, not before later pushes, and there is no restore action.
* **Zone Settings Control:** Read and adjust security level, development mode, and browser cache TTL per website, applied to Cloudflare immediately.

## 🚀 Odoo 19 & Zero-Trust Ready

This module is fully optimized for Odoo 19 and adheres to a strict Zero-Sudo architecture: it never uses Odoo's `.sudo()`; work that needs more rights than the current user has runs as a dedicated service-account user (`with_user()`), each holding only the rights its job needs. All Cloudflare edge operations are delegated to specific service accounts, ensuring that even in a multi-tenant environment, credentials and operations remain isolated and secure.

## 🛠️ How to Set It Up

1. Ensure the `cloudflare` module is in your Odoo `addons` directory.
2. Configure credentials per website in **Settings > Cloudflare** (the **Cloudflare Edge Orchestration** section):
   * `CF API Token` (Requires `Zone.Cache Purge`, `Zone.Firewall Services`, and `Account.Cloudflare Tunnel` permissions)
   * `CF Zone ID`
   * `CF Account ID` (Required for Zero Trust Tunnels)
   * `Turnstile Secret` (Optional; required for Turnstile verification)

---

# Technical Documentation

## 1. Overview
Control plane for the CDN edge. Manages Cache-Tags (labels sent in a response's `Cache-Tag` header so Cloudflare can later purge every cached object carrying that label; this module sets `odoo-static-assets` and `odoo-website-<id>`), WAF bans, and Turnstile CAPTCHA verification to offload processing to Cloudflare's edge.

## 2. API Interfaces
* **DNS & Hostnames:** `cloudflare.dns.record` rows (A, AAAA, CNAME, TXT, NS) are pushed to Cloudflare by `dns_push_plan()` and `dns_push_apply(hash)` (list view, Action, "Push DNS records to Cloudflare"; administrators only). Odoo is the source of truth. `CloudflareRoutingDomain` provisions Custom Hostnames separately; never add one for a name that is already a zone of the account.
  * **Plan first.** The plan is computed from reads only and shows, per row: `create`, `update`, `delete`, `adopt`, `conflict`, `problem`, `drift`, `unchanged`, with the reason. Apply takes the plan's hash and refuses if Cloudflare or the rows changed since. A row that cannot be planned (no token, no zone, Cloudflare unreachable) is reported and does not hold back the others; an unreachable Cloudflare plans nothing, never "create everything".
  * **What it never does.** It never overwrites a record it did not create: a record that already exists with the same content (and proxied flag) is *adopted*, which stores its id in the row and writes nothing at Cloudflare; one with other content is a `conflict`. Pasting that record's id into the row is the deliberate way to take it over. It never touches a record Odoo has no row for. Deleting or archiving a row removes nothing at Cloudflare; the only delete is the **Retire** flag on a row linked to its record by id. An update keeps the record's TTL (a new record gets automatic TTL).
  * **Rows.** `name` is the full lower-case name without a trailing dot; the zone is found from it (the longest suffix that is a zone the token sees). Credentials: the row's website, else the first website with an API token. `manage` off means observe only (rows that existed before 1.7 were switched to it by the migration). NS and TXT rows, and any name an NS row points at (glue), are never proxied.
  * **DNSSEC.** A delegated sub-zone that is not signed needs no DS (delegation signer) record, and Odoo has no DS type: an NS delegation with no DS is an ordinary insecure delegation, even when the parent zone is signed.
  * **Capacity and wildcard guard (1.8).** A push that creates more records than it deletes in a zone reads the zone's plan and record count (GET only) and is refused past 90% of the plan's limit (Free 200, or 1,000 for zones created before 2024-09-01; Pro and Business 3,500; Enterprise has an account quota and is not stopped here). If Cloudflare cannot report either number the growing push is refused, not guessed. A member name delegation (an NS row for one label directly under the zone) is also refused while a `*.<zone>` record exists, or is being created in the same push, because the wildcard would answer for names that have no delegation. [@ANCHOR: cloudflare:COMM_dns_capacity_guard]
  * **Plan vocabulary and pure planner.** `utils/dns_plan.py` computes a push as plain data with no Odoo and no network. The eight outcomes a row can have are `create`, `update`, `delete`, `adopt`, `unchanged`, `drift`, `conflict` and `problem`; only `create`, `update` and `delete` write to Cloudflare. [@ANCHOR: cloudflare:COMM_dns_plan]
  * **Record-limit arithmetic.** `record_limit()` maps a zone's plan to its record limit (Free 200, or 1,000 for a zone created before 2024-09-01; Pro and Business 3,500; no per-zone limit for Enterprise or an unknown plan). `capacity_problem()` refuses a push that creates more records than it deletes when the projected total passes the 90% hard stop `CAPACITY_HARD_STOP_PERCENT`. `is_member_delegation()` recognises an NS row for one label directly under the zone, which is what a verified member's name looks like. [@ANCHOR: cloudflare:COMM_dns_capacity]
  * **Planning a push.** `dns_push_plan()` reads Cloudflare (GET only) for each selected row and returns the entries, any whole-push problems, a hash of what would be written and a text rendering. It is for administrators only, and a row it cannot plan becomes a `problem` entry instead of stopping the others. [@ANCHOR: cloudflare:COMM_dns_push_plan]
  * **Whole-push refusals.** `_dns_problems()` is the hook that returns reasons the entire push must be refused; the base implementation returns the capacity and wildcard problems, and other modules can extend it the way tunnel pushes extend `_ingress_problems`. A refused plan is shown as `REFUSED: ...` and cannot be applied. [@ANCHOR: cloudflare:COMM_dns_problems_hook]
  * **Applying a push.** `dns_push_apply(hash)` recomputes the plan and applies it only when it is not refused and its hash equals the one the administrator saw; it creates, updates, adopts or (for a Retire row linked by id) deletes, records the outcome in each row's `cf_last_result`, and returns one result line per row. A failed write is reported on that row and does not stop the others. [@ANCHOR: cloudflare:COMM_dns_push_apply]
  * **Who may push.** `_check_dns_caller_authorized()` raises an access error unless the caller is the superuser or in the Settings administrator group, and both planning and applying call it first. [@ANCHOR: cloudflare:COMM_dns_check_caller_authorized]
  * **Push wizard.** "Push DNS records to Cloudflare" opens `cloudflare.dns.push.wizard` for the selected rows (or all rows when none are selected). Its first step, `action_plan()`, stores the plan text, its hash and the number of things Apply would act on (writes, adoptions and already-gone retirements), prefixing the text with `REFUSED:` when the whole push is refused. [@ANCHOR: cloudflare:COMM_dns_push_wizard_plan] The second step, `action_apply()`, refuses until a plan exists and then applies exactly the plan whose hash it stored, showing the per-row results. [@ANCHOR: cloudflare:COMM_dns_push_wizard_apply]
  * **Row validation.** Saving a `cloudflare.dns.record` checks that the name is a lower-case full name of at least two labels with no trailing dot; that A and AAAA content is an address of that kind, NS and CNAME content is a host name and TXT content is 1 to 2048 characters; that NS and TXT rows are not proxied; that a stored Cloudflare id is 32 lower-case hex characters; and that no other active row has the same type, name and content. [@ANCHOR: cloudflare:COMM_dns_record_constraints]
  * **Nameserver glue stays DNS-only.** A name that an NS row points at cannot be a proxied A or AAAA row, and an NS row cannot point at a name that has a proxied address row, whichever is saved first, because a proxied row would hand resolvers Cloudflare's addresses instead of the nameserver's. [@ANCHOR: cloudflare:COMM_dns_record_glue_unproxied]
  * **Pending pushes.** Cloudflare, "Push pending DNS changes" opens the push wizard with the plan already made for the managed rows not yet at Cloudflare and the rows marked Retire; one more click applies exactly that plan. [@ANCHOR: cloudflare:COMM_dns_push_pending]
  * **Extension.** `cloudflare.dns.record._dns_problems(entries)` returns strings that refuse the whole push, as `cloudflare.tunnel._ingress_problems` does for tunnels.

* **Traffic Control:** Management of `cloudflare.rate.limit` and `cloudflare.cache.rule` (local records only, not synchronized to Cloudflare).

* **Zero Trust Policies:** Management of `cloudflare.zero.trust.policy` models (local records only, not synchronized to Cloudflare).

* **Configuration Backups:** Management of `cloudflare.config.backup` for pre-Odoo state snapshots (created at install by `initialize_cloudflare_state()`).

* **Zone settings wizard.** "Always Use HTTPS" is sent to Cloudflare for the zone when the administrator picks a value, and left alone when none is picked. `cloudflare:COMM_zone_settings_always_use_https` The HSTS choice is sent as Cloudflare's `security_header` setting: on carries the max age (86400 seconds when blank), include-subdomains, nosniff and, if asked, preload; off sends a disabled header with a zero max age. `cloudflare:COMM_zone_settings_security_header`

* **Tunnel entry points are gated.** `action_sync_tunnels` is a public model method that any logged-in session, including the public website user, could dispatch; `_check_tunnel_caller_authorized()` refuses it unless the caller is in the Cloudflare Tunnel group, a Settings administrator or a service account, before any Cloudflare call is made. [@ANCHOR: cloudflare:COMM_check_tunnel_caller_authorized]

* **Per-company encryption keys.** Website secrets (the Turnstile secret and the API token) are encrypted with a random per-company key, one `cloudflare.tenant.key` row per company, rather than one key shared by every tenant. The row holds that key wrapped by the deployment-wide crypto secret, and only one row is allowed per company. [@ANCHOR: cloudflare:COMM_tenant_key_fields] `_get_or_create_fernet(company)` returns the company's cipher, creating and wrapping a key on first use (two concurrent first reads end up on the same row), and returns `None` when the deployment secret is missing or malformed or the stored key cannot be unwrapped. [@ANCHOR: cloudflare:COMM_tenant_key_get_or_create_fernet]

* **WAF IP Banning:** `env['cloudflare.waf'].ban_ip(...)` creates a Cloudflare IP Access Rule (block or challenge) for the address on the website's zone and records it in `cloudflare.ip.ban` `[@ANCHOR: cf_execute_ban]`. Supports multiple websites. Callers must be in the Cloudflare WAF group, be Settings administrators, or be service accounts. Repeat calls for an already-active ban do nothing; a ban that could not be sent (no credentials, API error) is recorded with state `failed` and retried on the next call.

* **WAF Management:** Pull `cf_action_pull_waf_rules` and push `cf_action_push_waf_rules` firewall rules.

* **Cache Purging:** `env['cloudflare.purge.queue'].enqueue_urls(...)` and `enqueue_tags(...)`. Processes an asynchronous queue grouped by website to prevent credential mixing `[@ANCHOR: cf_process_queue_logic]`. The "Process Cache Purge Queue" cron runs it every minute as the purge service account, up to 10 batches of 30 entries per run. A successful purge deletes its entries; an "everything" purge also drops that website's other pending entries. Entries for a website with no API token or Zone ID, or whose API call fails, are marked `failed` and are not retried.

* **Turnstile API:** `env['cloudflare.turnstile'].verify_token(...)` evaluates tokens against the Cloudflare API `cf_turnstile_verify`.

* **Edge Context:** `env['cloudflare.utils'].get_request_context()` extracts geographic and threat data from trusted headers `[@ANCHOR: cf_get_request_context]`. "Trusted" means the `CF-*` headers are used only when the connecting peer is loopback (the Tunnel case) or inside the trusted Cloudflare IP range allow-list (empty unless an administrator turns on **Trust Cloudflare's Published IP Ranges** in Settings, for a deployment without a Tunnel); from any other peer every geographic and threat field is returned as `None`, so a direct request to the origin cannot forge them.

* **Tunnel Management:** Wizard generates installation commands `cf_tunnel_setup`. Sync and delete tunnels across accounts `cf_sync_tunnels`, `cf_delete_tunnel`.

* **Multi-website tunnels:** one server fronting several websites is the ordinary case here. The "Ensure Tunnel Daemon Running" cron keeps **every** tunnel that has credentials up `ensure_tunnel_running`, one `cloudflared` daemon per tunnel, each tracked under its own Cloudflare tunnel id: a running tunnel is never started twice, a dead one is restarted, and one website's Cloudflare failure never stops another's tunnel `ensure_one_tunnel_running`. "Routes pushed yet?" lives on the tunnel record (`routes_provisioned`); installs carrying the old single `cloudflare.tunnel.provisioned` system parameter have it folded onto the one tunnel it was really about, once `migrate_global_provisioned_flag`.

## 3. Automated Subsystems
* **Header Injection:** Injects `Cloudflare-CDN-Cache-Control` headers via `ir.http._post_dispatch` `edge_cache:COMM_ir_http_post_dispatch_headers`. This code, the first-visit cookie rules and the CSRF refresh script live in the `edge_cache` module (a dependency of this one, usable on its own by an instance with no Redis). Dynamic and sensitive routes `cf_nocache_routes` are excluded.

* **Request Context Safety:** Safely extracts edge headers even in non-HTTP or unbound request contexts `[@ANCHOR: cf_get_request_context]`.
* **Boot-time Sync:** `_trigger_edge_purge_static_assets()` scans installed modules' `static/` folders and queues an `odoo-static-assets` tag purge for every website with credentials when the newest file is newer than the `cloudflare.last_static_mtime` system parameter. Nothing currently calls it at boot (only its test does), so this runs only when invoked explicitly.
* **Content Hooks:** Automatically enqueues purges for `website.page`, `blog.post`, `product.template`, and `website.menu` modifications and deletions. A record with no website is purged on every website, since it is served on all of them.

## 4. Zero-Sudo & Micro-Privilege Architecture
Strictly adheres to Zero-Sudo architecture using dedicated service accounts:
* `cloudflare.user_cloudflare_purge`: Cache purging.
* `cloudflare.user_cloudflare_waf`: WAF and IP banning, the install-time WAF sync, and Turnstile verification.
* `cloudflare.user_cloudflare_tunnel`: Tunnel management, custom hostname provisioning, and pending hostname delete retries.
* `cloudflare.user_cloudflare_trusted_ip`: Daily refresh of the trusted Cloudflare IP ranges.

## 🔐 Security & Multi-Tenancy
The module enforces website-level isolation through the website's company: record rules let a user see purge-queue, WAF-rule, IP-ban, configuration-backup and tunnel records only for websites whose company is among the user's allowed companies (or that have no company). The Cloudflare Edge menu is visible only to Settings administrators. API tokens and Zone IDs are never exposed to the frontend and are handled exclusively by backend service accounts; the API token and Turnstile secret are stored encrypted with a per-company key, and the credential fields are readable only by administrators and the Cloudflare service groups.

---

## Function-Level Anchors by Area

Each anchor below has its own entry, in the template of ADR 0113: purpose, call interface, special concerns, see-also.

### `cloudflare:COMM_zone_settings_always_use_https` [@ANCHOR: cloudflare:COMM_zone_settings_always_use_https]

**Purpose:** the "Always Use HTTPS" step of the zone settings wizard (`cloudflare.zone.settings.wizard`, `models/zone_settings_wizard.py`), inside `action_apply_settings()`. It makes Cloudflare redirect every plain-HTTP request for the zone to HTTPS at the edge, before the origin sees it.

| Field | Type | Meaning |
|---|---|---|
| `always_use_https` | Selection `on` / `off` | The wizard field. Empty means "leave the zone's current value alone". |

**Behaviour:** only when the field is set, it calls `update_zone_setting("always_use_https", value, token, zone_id)`, which PATCHes `/zones/<zone_id>/settings/always_use_https` with `{"value": "on"|"off"}`. A failure appends `Always Use HTTPS: <message>` to a list; after every setting has been tried, the list is raised together as one `UserError`, so one bad setting does not hide the others.

**Security and side effects:** a network call to Cloudflare with the website's API token, taken from `_get_cloudflare_credentials()`. Missing token or zone raises `UserError` before any call.

**See also:** `cloudflare:COMM_zone_settings_action_apply`, `cloudflare:COMM_zone_settings_default_get` (which reads the current value back into the form); tests in `cloudflare/tests/test_zone_settings_always_https.py`.


### `cloudflare:COMM_zone_settings_security_header` [@ANCHOR: cloudflare:COMM_zone_settings_security_header]

**Purpose:** the HSTS (HTTP Strict Transport Security) step of `action_apply_settings()` in `models/zone_settings_wizard.py`. Cloudflare exposes HSTS as the zone setting `security_header`, and this step builds that setting's value from the wizard's fields.

| Wizard field | Type | Meaning |
|---|---|---|
| `hsts_status` | Selection `on` / `off` | Empty skips the step. |
| `hsts_max_age` | Integer seconds | Used when status is `on`; default 86400, and 0 or blank is sent as 86400. |
| `hsts_include_subdomains` | Boolean | Sent only when status is `on`. |
| `hsts_nosniff` | Boolean, default true | Sent only when status is `on`. |
| `hsts_preload` | Boolean | When status is `on`, adds `"preload": true`. |

**Sent value:** `{"strict_transport_security": {"enabled", "max_age", "include_subdomains", "nosniff"[, "preload"]}}`. Status `off` sends `enabled: false`, `max_age: 0` and both flags false. The PATCH goes through `update_zone_setting("security_header", ...)`; a failure is recorded as `Security Header (HSTS): <message>` and raised with the other failures at the end.

**Security:** preload can make browsers refuse plain HTTP for the domain for a long time, so it is sent only when asked for. See also `cloudflare:COMM_zone_settings_action_apply`.


### `cf_action_pull_waf_rules` [@ANCHOR: cf_action_pull_waf_rules]

**Purpose:** replaces this website's WAF rules in Odoo (`cloudflare.waf.rule`) with the zone's current custom firewall ruleset at Cloudflare. It is the "Pull" direction of WAF management; `cloudflare:COMM_action_pull_cf_waf` is the Settings button that calls it.

**Call interface:** `cloudflare.config.manager.action_pull_waf_rules(website_id=None) -> (bool, str)`, a `@public_rpc`-style model method.

| Argument | Type | Meaning |
|---|---|---|
| `website_id` | int or None | The website to pull for; `None` uses the current website. |

**Returns:** `(True, "Successfully pulled rules ...")`, or `(False, "No custom firewall ruleset found in Cloudflare for <site>.")` when the zone has no ruleset (nothing is deleted then).

**Errors:** `AccessError` from `_check_waf_caller_authorized()` unless the caller is in the Cloudflare WAF group, a Settings administrator or a service account.

**Side effects:** one GET of the `http_request_firewall_custom` phase entrypoint; it then deletes up to 1000 of the website's existing rules and creates one row per Cloudflare rule (sequence 10, 20, ...; name is the description text before the first colon cut to 50 characters; action, expression, description and enabled copied). The delete is destructive and no backup is taken, so unpushed local edits are lost.

**See also:** `cf_action_push_waf_rules`, `cloudflare:COMM_check_waf_caller_authorized`; tests in `cloudflare/tests/test_waf_management.py`.


### `cf_action_push_waf_rules` [@ANCHOR: cf_action_push_waf_rules]

**Purpose:** replaces the zone's whole custom firewall ruleset at Cloudflare with this website's WAF rules from Odoo. It is the "Push" direction; a rule that exists only at Cloudflare is removed.

**Call interface:** `cloudflare.config.manager.action_push_waf_rules(website_id=None) -> (bool, str)`.

| Argument | Type | Meaning |
|---|---|---|
| `website_id` | int or None | The website to push for; `None` uses the current website. |

**Returns:** `(False, "Missing API credentials for <site>.")` when there is no token or zone id; otherwise the `(success, message)` pair returned by `update_zone_ruleset()` when a ruleset with an id exists, or by `create_zone_ruleset()` when none does.

**Errors:** `AccessError` from `_check_waf_caller_authorized()` for an unauthorized caller.

**Side effects:** reads up to 1000 `cloudflare.waf.rule` rows, builds a payload named `Odoo WAF Rules - <site>` (kind `zone`, phase `http_request_firewall_custom`; each rule carries action, expression, description-or-name and enabled), reads the current ruleset, then writes it. No backup of the old ruleset is taken (backups happen only at install).

**See also:** `cf_action_pull_waf_rules`, `cloudflare:COMM_action_deploy_cf_waf`, `cloudflare:COMM_initialize_cloudflare_state`; tests in `cloudflare/tests/test_waf_management.py`.


### `cf_turnstile_verify` [@ANCHOR: cf_turnstile_verify]

**Purpose:** backend check of a Cloudflare Turnstile (CAPTCHA) token, so a form controller can decide whether a submitter passed the challenge. Anonymous visitors call it, which is why it reads the secret as a service account.

**Call interface:** `env["cloudflare.turnstile"].verify_token(token, remote_ip=None, website_id=None) -> bool` (`models/turnstile.py`).

| Argument | Type | Meaning |
|---|---|---|
| `token` | str | The `cf-turnstile-response` value from the browser. |
| `remote_ip` | str or None | The visitor's address; sent to Cloudflare as `remoteip` when present. |
| `website_id` | int or None | Whose Turnstile secret to use; `None` resolves the current website through `get_current_website_id()`. |

**Returns:** `True` only when Cloudflare's `siteverify` answers `success: true`. `False` for an empty token, a website with no Turnstile secret, or a network failure. It never raises for a Cloudflare problem and never returns the secret.

**Special concerns:** the secret field is group-restricted, so the website is browsed `with_user()` the `cloudflare.user_cloudflare_waf` service account (no `.sudo()`). One outbound POST to `challenges.cloudflare.com` with a short timeout, no locks.

**See also:** `cloudflare:COMM_get_current_website_id`; tests `cloudflare/tests/test_cloudflare_apis.py`.


### `cf_tunnel_setup` [@ANCHOR: cf_tunnel_setup]

**Purpose:** the "Generate tunnel command" button in Settings (`res.config.settings.action_generate_tunnel_command()` in `models/res_config_settings.py`). It creates a new Cloudflare tunnel and shows the one-line `cloudflared service install <token>` command the administrator runs on the server.

**Call interface:** `action_generate_tunnel_command(self) -> dict`, an Odoo window action opening a `cloudflare.tunnel.wizard` form. It uses the selected website, or the current one.

**Errors:** `UserError` when the website lacks the API token or Account ID, when `create_cfd_tunnel()` fails (`Failed to create tunnel: ...`) or when `get_cfd_tunnel_token()` fails.

**Special concerns:** the tunnel is named `odoo-edge-tunnel-<unix seconds>`. The install token is secret: it is deliberately not stored in any column. The wizard row is created empty and its `command` field is computed from the action's context, so the token exists only in that one response. Two Cloudflare API calls, blocking, in the request worker.

**See also:** `cf_sync_tunnels` (brings the new tunnel into Odoo), `ensure_tunnel_running`; tests `cloudflare/tests/test_tunnel_provisioning_security.py`.


### `cf_sync_tunnels` [@ANCHOR: cf_sync_tunnels]

**Purpose:** makes Odoo's `cloudflare.tunnel` records match the tunnels that exist at Cloudflare for every website. It is the cron and button entry point; the per-website work is `cloudflare:COMM_sync_tunnels_for_website`.

**Call interface:** `cloudflare.tunnel.action_sync_tunnels() -> dict`, an `ir.actions.client` `display_notification` ("Tunnels sync queued successfully.").

**Errors:** `AccessError` from `_check_tunnel_caller_authorized()` unless the caller is in the Cloudflare Tunnel group, a Settings administrator or a service account. Per-website failures are logged and do not propagate.

**Special concerns:** loops over up to 1000 websites synchronously (no queue). Each website runs in its own database savepoint, so a Cloudflare error or constraint failure on one website rolls back only that website's writes and the loop continues; a bare try/except would leave PostgreSQL's aborted-transaction state in force for every later website. It does not delete records for tunnels that disappeared at Cloudflare.

**See also:** `cloudflare:COMM_check_tunnel_caller_authorized`; tests `cloudflare/tests/test_tunnel_multi_website.py`.


### `cf_delete_tunnel` [@ANCHOR: cf_delete_tunnel]

**Purpose:** deletes selected tunnels at Cloudflare and then in Odoo, stopping each tunnel's local `cloudflared` unit so it is not orphaned.

**Call interface:** `cloudflare.tunnel.action_delete_tunnel()` on a recordset; returns `None` on full success, a notification action when some failed, and raises when all failed.

**Behaviour per tunnel:** reads the website's token and Account ID (a missing one adds a failure line and skips); calls `delete_cfd_tunnel(account_id, token, cf_tunnel_id)`; on success calls `stop_tunnel_daemon(cf_tunnel_id)` (a persistent systemd user unit that would otherwise run forever against a deleted tunnel) and unlinks the record; on failure records `<tunnel>: <message>`.

**Errors:** `UserError` listing every failure when all selected tunnels failed. Failures are collected rather than raised mid-loop because one Odoo request is a single transaction: raising would roll back the unlinks of tunnels that were already deleted remotely, leaving Odoo out of step with Cloudflare.

**See also:** `cloudflare:COMM_stop_tunnel_daemon`; tests `cloudflare/tests/test_tunnel_daemon.py`.


### `ensure_tunnel_running` [@ANCHOR: ensure_tunnel_running]

**Purpose:** the "Ensure Tunnel Daemon Running" cron body. It keeps one `cloudflared` daemon running for every `cloudflare.tunnel` that has credentials, so a server fronting several websites keeps all of them reachable.

**Call interface:** `cloudflare.tunnel.action_ensure_tunnel_running() -> dict` with counts `started`, `already_running`, `skipped`, `failed` (also logged). An empty tunnel table returns all zeros.

**Behaviour:** first calls `_migrate_global_provisioned_flag()` once, then for each tunnel (up to 10000) runs `_ensure_one_tunnel_running()` inside its own savepoint. An exception on one tunnel increments `failed` and the loop goes on.

**Special concerns:** runs as the tunnel service account from cron. It starts system-level processes through `cloudflare_systemd` (see `cloudflare:COMM_stop_tunnel_daemon`), so it has real side effects on the host; it never starts a running tunnel twice and restarts a dead one. Safe to run repeatedly (idempotent).

**See also:** `ensure_one_tunnel_running`, `migrate_global_provisioned_flag`; tests `cloudflare/tests/test_tunnel_daemon.py`, `test_tunnel_multi_website.py`.


### `ensure_one_tunnel_running` [@ANCHOR: ensure_one_tunnel_running]

**Purpose:** brings exactly one tunnel up and reports what happened, so the cron summary can count outcomes. Called only from `action_ensure_tunnel_running`.

**Call interface:** `cloudflare.tunnel._ensure_one_tunnel_running(tunnel) -> str`, one of `"already_running"`, `"skipped"`, `"started"`, `"failed"`.

**Decision order:** no `cf_tunnel_id` gives `skipped`; routes already provisioned and the unit running gives `already_running`; a website without token or Account ID gives `skipped`; a failed or empty run-token fetch (`get_cfd_tunnel_token`) gives `failed`; if routes were never pushed it calls `action_push_configuration()` and sets `routes_provisioned` (a failure there is logged and does not stop the start); finally, if the unit is still not running, `start_tunnel_daemon(token, tunnel_key=cf_tunnel_id)` gives `started`.

**Errors:** ordinary Cloudflare problems return a word and never raise, so one website's bad day cannot take the others down.

**Security:** the run token is passed to the daemon layer and not logged.

**See also:** `ensure_tunnel_running`, `cloudflare:COMM_tunnel_action_push_configuration`.


### `migrate_global_provisioned_flag` [@ANCHOR: migrate_global_provisioned_flag]

**Purpose:** a one-time, idempotent migration of the old single system parameter `cloudflare.tunnel.provisioned` ("routes were pushed") onto the per-tunnel field `routes_provisioned`.

**Call interface:** `cloudflare.tunnel._migrate_global_provisioned_flag() -> bool`, `True` when it migrated, `False` when there was nothing to do.

**Behaviour:** reads the parameter bypassing the cache; if it is empty or already the sentinel value, returns `False`. Otherwise it sets `routes_provisioned` on only the lowest-id tunnel (the one the old `search([], limit=1)` code operated on; marking every tunnel would skip first route pushes for tunnels never provisioned) and then rewrites the parameter to a sentinel instead of deleting it, so a rollback to the older code still sees a truthy value.

**Concurrency:** safe on every cron tick; no explicit locks.

**See also:** `ensure_tunnel_running` (calls it once per run).


### `edge_cache:COMM_ir_http_post_dispatch_headers` [@ANCHOR: edge_cache:COMM_ir_http_post_dispatch_headers]

**Purpose:** sets the CDN caching headers on every outgoing response, in the `edge_cache` module (`models/ir_http.py`, class `IrHttp`, a classmethod override of `ir.http._post_dispatch(response)`). It returns the parent's result and does nothing outside a request.

**Rules, in order:** (1) a page that carried the marker header `X-Cloudflare-Edge-Cacheable` is noted and the marker is removed so it never reaches the client; (2) a path under `/web/assets` gets `Cloudflare-CDN-Cache-Control: max-age=31536000` and `Cache-Tag: odoo-static-assets`, but only for a 200 or 304 with no `Set-Cookie`; otherwise `no-cache, no-store`; (3) the dynamic prefixes in `cf_nocache_routes` get `no-cache, no-store`; (4) public HTML is cached (`max-age=86400`) only when the page opted in and nothing in the exchange is per-visitor, and is tagged `odoo-website-<id>` when the request has a website; anything else is `no-cache, no-store`. Error pages are marked never-stored.

**Concerns:** edge-cached responses are replayed to every visitor, so a `Set-Cookie` and cacheable must never coexist. Pure header manipulation; no database writes, no locks.

**See also:** `cf_nocache_routes`; tests in `edge_cache/tests/test_edge_cache_headers.py` and `test_edge_cache_cookies.py`.


### `cf_nocache_routes` [@ANCHOR: cf_nocache_routes]

**Purpose:** the list of URL prefixes that must never be stored at Cloudflare's edge, inside `ir.http._post_dispatch` in the `edge_cache` module.

**The prefixes:** `/my/`, `/odoo`, `/web/` (this deliberately includes `/web/image` and `/web/content`, which can serve private attachments and would otherwise allow an object-reference leak across visitors), `/api/`, `/shop/cart`, `/shop/checkout`, `/shop/confirm_order` and `/helpdesk/`. A match sets `Cloudflare-CDN-Cache-Control: no-cache, no-store` and returns at once. `/web/assets` is handled before this check, so asset bundles are the one `/web/` exception.

**Concerns:** a plain `startswith` test on the request path; a new private or per-user route family must be added here or it falls through to the public-HTML rule. No side effects.

**See also:** `edge_cache:COMM_ir_http_post_dispatch_headers`; tests `edge_cache/tests/test_edge_cache_headers.py`.


### `cloudflare:COMM_post_init_hook` [@ANCHOR: cloudflare:COMM_post_init_hook]

**Purpose:** the module's install hook, `post_init_hook(env)` in `cloudflare/hooks.py`, run once by Odoo after installation.

**Behaviour:** looks up the service account `cloudflare.user_cloudflare_waf` through `zero_sudo.security.utils._get_service_uid()`, then as that user runs `cloudflare.config.manager.initialize_cloudflare_state()` and `ir.module.module._bootstrap_knowledge_docs()` (installs the module's knowledge articles).

| Argument | Type | Meaning |
|---|---|---|
| `env` | `odoo.api.Environment` | Supplied by Odoo; it is not retained. |

**Returns:** nothing. **Errors:** a missing service account or an authorization failure propagates and fails the install. Network calls to Cloudflare occur (only for websites that already have credentials).

**See also:** `cloudflare:COMM_initialize_cloudflare_state`; tests `cloudflare/tests/test_hooks.py`.


### `cloudflare:COMM_initialize_cloudflare_state` [@ANCHOR: cloudflare:COMM_initialize_cloudflare_state]

**Purpose:** the install-time sync of firewall rules for every website, `cloudflare.config.manager.initialize_cloudflare_state()`.

**Behaviour:** after `_check_waf_caller_authorized()`, pages through websites 1000 at a time by id. A website without token or zone is skipped. For each other website it reads the zone's custom ruleset: if rules exist, it writes a `cloudflare.config.backup` row (`Pre-Odoo Backup (<site>)`, raw JSON) and queues the website for a pull; if none, the website is queued for a default-rule push. Pulls delete the website's existing `cloudflare.waf.rule` rows and recreate them from Cloudflare; pushes create the `DEFAULT_WAF_RULES` rows and call `action_push_waf_rules()` for each.

**Returns:** nothing. **Errors:** `AccessError` for an unauthorized caller.

**Concerns:** many Cloudflare reads and writes in one transaction; run it as the WAF service account. The backup is taken only here, never before a later push.

**See also:** `cf_action_pull_waf_rules`, `cf_action_push_waf_rules`, `cloudflare:COMM_post_init_hook`; tests `cloudflare/tests/test_waf_management.py`.


### `cloudflare:COMM_check_waf_caller_authorized` [@ANCHOR: cloudflare:COMM_check_waf_caller_authorized]

**Purpose:** the access gate for WAF operations, `cloudflare.config.manager._check_waf_caller_authorized()`. Both the pull and push actions and the install sync call it first, on every call, so the check does not depend on the distributed cache.

**Rule:** passes when the user is in `cloudflare.group_cloudflare_waf`, in `base.group_system`, or is a service account (`is_service_account`).

**Returns:** nothing. **Errors:** `AccessError("You are not authorized to manage the Cloudflare WAF.")` otherwise.

**Security:** the public website user and ordinary employees are refused; the rule is checked before any credential read or Cloudflare call. No side effects.

**See also:** `cf_action_pull_waf_rules`, `cf_action_push_waf_rules`, `cloudflare:COMM_check_tunnel_caller_authorized`.


### `cloudflare:COMM_domain_create` [@ANCHOR: cloudflare:COMM_domain_create]

**Purpose:** when a routing domain is created, provision the matching Cloudflare custom hostname (`CloudflareRoutingDomain.create(vals_list)` in `models/domain.py`, extending `edge.routing.domain`).

**Call interface:** the ordinary Odoo batch `create(vals_list)`; it creates the records through the parent, then calls `_create_cloudflare_custom_hostname_batch()` on them and returns the records.

**Concerns:** a domain whose name matches no website's `domain` is skipped (nothing is sent). Provisioning failures do not roll back the domain record; `ssl_status` and `cloudflare_hostname_id` are only filled on success. Network calls happen inside `create()`.

**See also:** `cloudflare:COMM_create_custom_hostname_batch`, `cloudflare:COMM_get_website_mapping`; tests `cloudflare/tests/test_domain_custom_hostname.py`.


### `cloudflare:COMM_get_website_mapping` [@ANCHOR: cloudflare:COMM_get_website_mapping]

**Purpose:** maps routing-domain names to their websites, in one query, for the custom-hostname methods in `models/domain.py`.

**Call interface:** `_get_website_mapping(self) -> dict[str, website]` on a recordset of `edge.routing.domain`; keys are `website.domain` values and only websites with a domain are included.

**Special concerns:** the search runs `with_user()` the `cloudflare.user_cloudflare_tunnel` service account (no `.sudo()`), restricted to websites whose `domain` is among the record names and limited to that many rows. No writes.

**See also:** `cloudflare:COMM_create_custom_hostname_batch`, `cloudflare:COMM_delete_custom_hostname_batch`, `cloudflare:COMM_action_sync_ssl_status`.


### `cloudflare:COMM_create_custom_hostname_batch` [@ANCHOR: cloudflare:COMM_create_custom_hostname_batch]

**Purpose:** asks Cloudflare to create a custom hostname (SSL for a tenant's own domain) for each routing domain in the recordset.

**Call interface:** `_create_cloudflare_custom_hostname_batch(self)` in `models/domain.py`; returns nothing.

**Behaviour:** per record, finds the website from `_get_website_mapping()` (none: logged and skipped), reads the token and zone, and calls `cf_utils.create_custom_hostname(name, token, zone_id)`. On success it stores `cloudflare_hostname_id` and `ssl_status` (the answer's SSL status, default `pending_validation`). Missing credentials or a failed call write nothing.

**Concerns:** one blocking API call per domain; ordering follows the recordset. Do not use for a name that is already a zone in the account. Names of verified members never reach Cloudflare through this path (see `cloudflare/tests/test_member_names_never_reach_cloudflare.py`).

**See also:** `cloudflare:COMM_domain_create`, `cloudflare:COMM_action_sync_ssl_status`.


### `cloudflare:COMM_delete_custom_hostname_batch` [@ANCHOR: cloudflare:COMM_delete_custom_hostname_batch]

**Purpose:** removes the Cloudflare custom hostnames of routing domains that are being deleted.

**Call interface:** `_delete_cloudflare_custom_hostname_batch(self)` in `models/domain.py`; returns nothing.

**Behaviour:** per record that has a `cloudflare_hostname_id` and a matching website with credentials, calls `cf_utils.delete_custom_hostname(hostname_id, token, zone_id)`. A failure is logged as a warning saying the hostname may still be active and must be removed manually; it does not raise, so the Odoo delete goes ahead. The `cloudflare.hostname.pending.delete` queue retries failed deletes (service account `cloudflare.user_cloudflare_tunnel`).

**See also:** `cloudflare:COMM_create_custom_hostname_batch`; tests `cloudflare/tests/test_hostname_pending_delete.py`.


### `cloudflare:COMM_action_sync_ssl_status` [@ANCHOR: cloudflare:COMM_action_sync_ssl_status]

**Purpose:** refreshes `ssl_status` on routing domains from Cloudflare's current view of their custom hostnames.

**Call interface:** `action_sync_ssl_status(self)` in `models/domain.py`, a button and cron method.

**Errors:** `AccessError` unless the caller is a Settings administrator or a service account.

**Behaviour:** per record with a `cloudflare_hostname_id`, a mapped website and credentials, calls `cf_utils.get_custom_hostname(...)` and copies the SSL status into the record. Records with missing pieces are skipped silently.

**Concerns:** one blocking API call per domain; read-only at Cloudflare.

**See also:** `cloudflare:COMM_get_website_mapping`; tests `cloudflare/tests/test_domain_custom_hostname.py`.


### `cloudflare:COMM_get_current_website_id` [@ANCHOR: cloudflare:COMM_get_current_website_id]

**Purpose:** one way to learn "which website am I working for" in both web requests and cron jobs. `cloudflare.utils.get_current_website_id()` in `models/edge_context.py`.

**Returns:** `int`. In an HTTP request that has a routed website, that website's id; otherwise (cron, unbound request) the id of `website.get_current_website()`.

**Concerns:** reads the request object's `website` attribute defensively, so it works in a non-HTTP context. No writes, no locks. Callers include `cf_turnstile_verify`.

**See also:** `cf_get_request_context`.


### `cloudflare:COMM_action_lift_ban_sync` [@ANCHOR: cloudflare:COMM_action_lift_ban_sync]

**Purpose:** the remote half of lifting an IP ban: `cloudflare.ip.ban._action_lift_ban_sync()` (`models/ip_ban.py`), called by `action_lift_ban()` for each active ban that has a Cloudflare rule id.

**Behaviour:** `ensure_one()`, reads the website's token and zone, calls `unban_ip(cf_rule_id, token, zone_id)` and sets `state = "lifted"` on success.

**Errors:** `UserError("Failed to lift ban via Cloudflare API: ...")` when Cloudflare refuses, leaving the state `active` so it can be retried.

**Concerns:** one blocking API call. Bans have no expiry, so this is the only way a ban ends.

**See also:** `cf_execute_ban`, `cloudflare/tests/test_waf_management.py`.


### `cloudflare:COMM_enqueue_cloudflare_purge` [@ANCHOR: cloudflare:COMM_enqueue_cloudflare_purge]

**Purpose:** converts changed records into queued cache purges, `cloudflare.purge.mixin._enqueue_cloudflare_purge(url_field)` (`models/purge_mixin.py`). Used by the page, blog post and product hooks in `bridge.py`.

| Argument | Type | Meaning |
|---|---|---|
| `url_field` | str | Name of the record field that holds the URL path (`url` for pages, `website_url` for posts and products). |

**Behaviour:** records with a website contribute their URL to that website; records without one contribute to every website (up to 1000), since they are served on all of them. Empty URLs are skipped. It then calls `cloudflare.purge.queue.enqueue_urls_batch(purge_map)` as the purge service account `cloudflare.user_cloudflare_purge`. Returns nothing.

**Concerns:** only queues rows (cheap, in the caller's transaction); the cron sends them. Callers invoke it before and after a write so both the old and new URL are purged.

**See also:** `cloudflare:COMM_enqueue_urls_batch`; tests `cloudflare/tests/test_bridge_and_misc.py`.


### `cloudflare:COMM_purge_cloudflare_menus` [@ANCHOR: cloudflare:COMM_purge_cloudflare_menus]

**Purpose:** website menu changes appear on every page, so they purge the whole cache for the affected websites, not single URLs. `cloudflare.purge.mixin._purge_cloudflare_menus()` (`models/purge_mixin.py`).

**Behaviour:** collects the menus' website ids; if any menu has no website, uses all websites (up to 1000). Then, as the purge service account, calls `cloudflare.purge.queue.enqueue_everything(website_ids=[...])`. Returns nothing.

**Concerns:** an "everything" entry also removes that website's other pending purge entries when processed. Cheap at write time; the cron sends it.

**See also:** `cloudflare:COMM_menu_write`, `cloudflare:COMM_menu_unlink`.


### `cloudflare:COMM_enqueue_urls_batch` [@ANCHOR: cloudflare:COMM_enqueue_urls_batch]

**Purpose:** the bulk way to queue URL purges, `cloudflare.purge.queue.enqueue_urls_batch(purge_map)` (`models/purge_queue.py`).

| Argument | Type | Meaning |
|---|---|---|
| `purge_map` | dict `{website_id: [url, ...]}` | URLs per website; the key `False` means no website. |

**Behaviour:** relative URLs starting with `/` are made absolute with the website's `domain` (or `web.base.url`); duplicates are removed; entries already pending for the same website and URL are not queued again (one search, not one per URL); the rest are created in a single `create()` as `purge_type = "url"` rows. An empty map returns at once.

**Returns:** nothing. **Concerns:** no Cloudflare call; the cron "Process Cache Purge Queue" sends the entries.

**See also:** `cf_process_queue_logic`; tests `cloudflare/tests/test_enqueue_urls_batch.py`.


### `cloudflare:COMM_purge_wizard_action_purge` [@ANCHOR: cloudflare:COMM_purge_wizard_action_purge]

**Purpose:** the manual purge wizard's Apply button, `cloudflare.purge.wizard.action_purge()` (`models/purge_wizard.py`).

**Behaviour:** `ensure_one()`, reads the website's token and zone (`UserError("Missing Cloudflare API Token or Zone ID ...")` when absent). `purge_type` is `everything`, `urls` or `tags`. `everything` calls `purge_everything(token, zone_id)`. For `urls` and `tags` the comma- or newline-separated `items_to_purge` text is split (empty gives `UserError("Please provide items to purge.")`); relative URLs are made absolute with the website domain or `web.base.url` and sent through `purge_urls()`, tags through `purge_tags()`. Success returns a `display_notification` client action that closes the wizard; a failed purge raises `UserError`.

**Concerns:** unlike the queue, this call is synchronous, in the user's request, and bypasses the queue. Everything purge empties the zone's cache, so the origin sees a load spike.

**See also:** `cloudflare:COMM_enqueue_urls_batch`; tests `cloudflare/tests/test_purge_everything.py`.


### `cloudflare:COMM_trigger_edge_purge_static_assets` [@ANCHOR: cloudflare:COMM_trigger_edge_purge_static_assets]

**Purpose:** detects that a static file changed since the last recorded change and queues a purge of the Cache-Tag `odoo-static-assets` for every website that has credentials. `cloudflare.config.manager._trigger_edge_purge_static_assets()` (`models/config_manager.py`).

**Behaviour:** gets the newest file time from `caching.mixin.get_fs_stats()` as the facility service account (if the service accounts do not exist yet it logs and returns), compares it with the system parameter `cloudflare.last_static_mtime`; when newer it stores the new time first, then creates one `tag` entry for `odoo-static-assets` per website with a token and zone, as the purge service account. Errors (`ValueError`, `OSError`, `RuntimeError`, `AccessError`) are logged, not raised.

**Returns:** nothing. **Concerns:** because the stored time is updated before the entries are created, a failure after that point loses the purge for that change. Nothing in the running code calls it at server start (only its test), so a purge happens only when something invokes it. The scan reads the file system of every installed module's `static/` folder.

**See also:** `cf_process_queue_logic`.


### `cloudflare:COMM_page_write` [@ANCHOR: cloudflare:COMM_page_write]

**Purpose:** purges a page's URL whenever it changes. `website.page.write(vals)` override in `models/bridge.py` (mixed with `cloudflare.purge.mixin`).

**Behaviour:** calls `_enqueue_cloudflare_purge("url")` before `super().write(vals)` (so the old URL is purged if the URL itself changes) and again after (the new URL), then returns the parent's result. Returns the parent's boolean; no new errors.

**Concerns:** only queues entries in the same transaction; a rolled-back edit rolls back the queued purges too.

**See also:** `cloudflare:COMM_page_unlink`, `cloudflare:COMM_enqueue_cloudflare_purge`; tests `cloudflare/tests/test_bridge_and_misc.py`.


### `cloudflare:COMM_page_unlink` [@ANCHOR: cloudflare:COMM_page_unlink]

**Purpose:** purges a page's URL when the page is deleted. `website.page.unlink()` override in `models/bridge.py`.

**Behaviour:** calls `_enqueue_cloudflare_purge("url")` first, while the URL fields are still readable, then `super().unlink()` and returns its result. The purge is queued, not sent.

**See also:** `cloudflare:COMM_page_write`, `cloudflare:COMM_enqueue_cloudflare_purge`.


### `cloudflare:COMM_blog_post_write` [@ANCHOR: cloudflare:COMM_blog_post_write]

**Purpose:** purges a blog post's URL whenever it changes. `blog.post.write(vals)` override in `models/bridge.py`.

**Behaviour:** `_enqueue_cloudflare_purge("website_url")` before and after `super().write(vals)`, so both the old and new URL are queued; returns the parent's result. The blog post also gets its own purge path in `user_websites/models/blog_post.py` for user-site posts.

**See also:** `cloudflare:COMM_enqueue_cloudflare_purge`; tests `cloudflare/tests/test_bridge_and_misc.py`.


### `cloudflare:COMM_menu_write` [@ANCHOR: cloudflare:COMM_menu_write]

**Purpose:** purges the whole cache of a website when one of its menus is edited. `website.menu.write(vals)` override in `models/bridge.py`.

**Behaviour:** calls `super().write(vals)` first, then `_purge_cloudflare_menus()`, and returns the parent's result. Every page carries the menu, which is why a single-URL purge would not be enough.

**See also:** `cloudflare:COMM_purge_cloudflare_menus`, `cloudflare:COMM_menu_unlink`.


### `cloudflare:COMM_menu_unlink` [@ANCHOR: cloudflare:COMM_menu_unlink]

**Purpose:** purges the whole cache of a website when one of its menus is deleted. `website.menu.unlink()` override in `models/bridge.py`.

**Behaviour:** calls `_purge_cloudflare_menus()` before `super().unlink()` so the menus' `website_id` can still be read, and returns the parent's result.

**See also:** `cloudflare:COMM_purge_cloudflare_menus`, `cloudflare:COMM_menu_write`.


### `cloudflare:COMM_product_write` [@ANCHOR: cloudflare:COMM_product_write]

**Purpose:** purges a product's URL whenever it changes. `product.template.write(vals)` override in `models/bridge.py`.

**Behaviour:** `_enqueue_cloudflare_purge("website_url")` before and after `super().write(vals)`; returns the parent's result. A product with no website is purged on every website, because the purge mixin treats a missing website as "all".

**See also:** `cloudflare:COMM_enqueue_cloudflare_purge`.


### `cloudflare:COMM_action_deploy_cf_waf` [@ANCHOR: cloudflare:COMM_action_deploy_cf_waf]

**Purpose:** the Settings button "Deploy WAF rules to Cloudflare", `res.config.settings.action_deploy_cf_waf()` in `models/res_config_settings.py`.

**Behaviour:** finds the WAF service account (`cloudflare.user_cloudflare_waf`), picks the settings page's website or the current one, and runs `cloudflare.config.manager.action_push_waf_rules(website_id=...)` as that account. A success tuple gives a `display_notification` action with the message; a failure raises `UserError("Failed to deploy WAF rules: ...")`.

**Concerns:** this replaces the zone's whole custom ruleset (a rule only at Cloudflare is removed).

**See also:** `cf_action_push_waf_rules`, `cloudflare:COMM_action_pull_cf_waf`.


### `cloudflare:COMM_action_pull_cf_waf` [@ANCHOR: cloudflare:COMM_action_pull_cf_waf]

**Purpose:** the Settings button "Pull WAF rules from Cloudflare", `res.config.settings.action_pull_cf_waf()` in `models/res_config_settings.py`.

**Behaviour:** as the WAF service account, runs `cloudflare.config.manager.action_pull_waf_rules(website_id=...)` for the settings page's website or the current one. Success gives a notification; failure raises `UserError("Failed to pull WAF rules: ...")`.

**Concerns:** replaces the website's Odoo rules with the zone's (unpushed local edits are lost).

**See also:** `cf_action_pull_waf_rules`, `cloudflare:COMM_action_deploy_cf_waf`.

### Entries still in the older bullet form

The items below are being converted to the per-anchor template above.

**Outbound API request safety:**
* **Idempotency-Aware Retry:** `[@ANCHOR: cloudflare:COMM_idempotency_aware_retry]` -- decides whether a
  failed Cloudflare API call may be replayed. The shared `requests` session retries the transient
  server-side failures (500/502/503/504) and rate limiting (429) for `HEAD`, `GET`, `OPTIONS`, `PUT`
  and `DELETE`, because repeating one of those cannot change the result beyond what the first attempt
  already did. `POST` and `PATCH` are retried on 429 only: a rate-limit refusal is rejected before
  Cloudflare applies it, whereas a 5xx carries no such guarantee and may arrive after a DNS record,
  a firewall access rule or a tunnel route has already been created -- replaying it would create a
  duplicate. `allowed_methods` stays restricted to the idempotent set as well, so urllib3's own
  read-error handling keeps refusing to replay a `POST` whose response was lost in transit.

**Encrypted credential storage (`website.py`):**
* **Fernet Key Resolution:** `[@ANCHOR: cloudflare:COMM_get_fernet]` -- Fernet is the symmetric authenticated-encryption scheme from the Python `cryptography` library. Returns the website's company's own cipher from `cloudflare.tenant.key` (see "Per-company encryption keys" above); it does not read the daemon key registry. Saving a secret with no deployment-wide crypto secret configured raises a `UserError`; reading one that cannot be decrypted yields the marker `***ERROR***`.

* **Symmetric Encrypt/Decrypt Primitive:** `[@ANCHOR: cloudflare:COMM_crypt_field]`

* **Generic Encrypted-Field Compute/Inverse:** `[@ANCHOR: cloudflare:COMM_compute_encrypted_field]`, `[@ANCHOR: cloudflare:COMM_inverse_encrypted_field]`

* **API Token Compute/Inverse:** `[@ANCHOR: cloudflare:COMM_compute_cf_api_token]`, `[@ANCHOR: cloudflare:COMM_inverse_cf_api_token]`

* **Turnstile Secret Compute/Inverse:** `[@ANCHOR: cloudflare:COMM_compute_cf_turnstile_secret]`, `[@ANCHOR: cloudflare:COMM_inverse_cf_turnstile_secret]`

* **Credential Resolution (Cached):** `[@ANCHOR: cloudflare:COMM_get_cloudflare_credentials]`

**Zone settings and tunnels:**
* **Zone Settings Wizard Defaults/Apply:** `[@ANCHOR: cloudflare:COMM_zone_settings_default_get]`, `[@ANCHOR: cloudflare:COMM_zone_settings_action_apply]`

* **Tunnel Route Display Name:** `[@ANCHOR: cloudflare:COMM_tunnel_route_compute_name]`

* **Tunnel Push Configuration:** `[@ANCHOR: cloudflare:COMM_tunnel_action_push_configuration]`

* **Tunnel Sync for Website:** `[@ANCHOR: cloudflare:COMM_sync_tunnels_for_website]`

**Low-level HTTP/native daemon layer:**
* **Generic API Error Handling:** `[@ANCHOR: cloudflare:COMM_handle_api_error]`

* **Generic HTTP Request Wrapper:** `[@ANCHOR: cloudflare:COMM_make_request]`

* **Cache-Tag Purge API:** `[@ANCHOR: cloudflare:COMM_purge_tags]`

* **Tunnel Configuration Update API:** `[@ANCHOR: cloudflare:COMM_update_cfd_tunnel_configuration]`

* **Native Library Resolution:** `[@ANCHOR: cloudflare:COMM_get_lib]`

* **Tunnel Simulator Start/Stop:** `[@ANCHOR: cloudflare:COMM_start_tunnel_simulator]`, `[@ANCHOR: cloudflare:COMM_stop_tunnel_simulator]`

* **Real Tunnel Daemon Stop:** `[@ANCHOR: cloudflare:COMM_stop_tunnel_daemon]`

* **Test Simulator Mixin Setup/Teardown/Request:** `[@ANCHOR: cloudflare:COMM_simulator_setup]`, `[@ANCHOR: cloudflare:COMM_simulator_teardown]`, `[@ANCHOR: cloudflare:COMM_simulate_edge_request]`

<stories_and_journeys>
## 5. Architectural Stories & Journeys

* [Asynchronous Cache Purging](hams_shared/docs/stories/cache_purging.md) `[@ANCHOR: story_cache_purging]`

* [Geo-Aware Request Context](hams_shared/docs/stories/request_context.md) `[@ANCHOR: story_request_context]`

* [Secure Edge Bridging via Tunnels](hams_shared/docs/stories/tunnels.md) `[@ANCHOR: story_tunnels]`

* [CAPTCHA Verification with Turnstile](hams_shared/docs/stories/turnstile_verification.md) `[@ANCHOR: story_turnstile]`

* [Automated WAF IP Banning](hams_shared/docs/stories/waf_banning.md) `[@ANCHOR: story_waf_banning]`

### Journeys
* [High-Performance Content Invalidation](hams_shared/docs/journeys/content_invalidation.md) `[@ANCHOR: journey_content_invalidation]`

* [Managing Edge Security](hams_shared/docs/journeys/edge_security.md) `[@ANCHOR: journey_edge_security]`

* [Infrastructure Provisioning](hams_shared/docs/journeys/infrastructure.md) `[@ANCHOR: journey_infrastructure]`

* [Intelligent Traffic Handling](hams_shared/docs/journeys/traffic_handling.md) `[@ANCHOR: journey_traffic_handling]`
</stories_and_journeys>
