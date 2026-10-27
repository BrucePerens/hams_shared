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

* **Zone settings wizard.** "Always Use HTTPS" is sent to Cloudflare for the zone when the administrator picks a value, and left alone when none is picked. [@ANCHOR: cloudflare:COMM_zone_settings_always_use_https] The HSTS choice is sent as Cloudflare's `security_header` setting: on carries the max age (86400 seconds when blank), include-subdomains, nosniff and, if asked, preload; off sends a disabled header with a zero max age. [@ANCHOR: cloudflare:COMM_zone_settings_security_header]

* **Tunnel entry points are gated.** `action_sync_tunnels` is a public model method that any logged-in session, including the public website user, could dispatch; `_check_tunnel_caller_authorized()` refuses it unless the caller is in the Cloudflare Tunnel group, a Settings administrator or a service account, before any Cloudflare call is made. [@ANCHOR: cloudflare:COMM_check_tunnel_caller_authorized]

* **Per-company encryption keys.** Website secrets (the Turnstile secret and the API token) are encrypted with a random per-company key, one `cloudflare.tenant.key` row per company, rather than one key shared by every tenant. The row holds that key wrapped by the deployment-wide crypto secret, and only one row is allowed per company. [@ANCHOR: cloudflare:COMM_tenant_key_fields] `_get_or_create_fernet(company)` returns the company's cipher, creating and wrapping a key on first use (two concurrent first reads end up on the same row), and returns `None` when the deployment secret is missing or malformed or the stored key cannot be unwrapped. [@ANCHOR: cloudflare:COMM_tenant_key_get_or_create_fernet]

* **WAF IP Banning:** `env['cloudflare.waf'].ban_ip(...)` creates a Cloudflare IP Access Rule (block or challenge) for the address on the website's zone and records it in `cloudflare.ip.ban` `[@ANCHOR: cf_execute_ban]`. Supports multiple websites. Callers must be in the Cloudflare WAF group, be Settings administrators, or be service accounts. Repeat calls for an already-active ban do nothing; a ban that could not be sent (no credentials, API error) is recorded with state `failed` and retried on the next call.

* **WAF Management:** Pull `[@ANCHOR: cf_action_pull_waf_rules]` and push `[@ANCHOR: cf_action_push_waf_rules]` firewall rules.

* **Cache Purging:** `env['cloudflare.purge.queue'].enqueue_urls(...)` and `enqueue_tags(...)`. Processes an asynchronous queue grouped by website to prevent credential mixing `[@ANCHOR: cf_process_queue_logic]`. The "Process Cache Purge Queue" cron runs it every minute as the purge service account, up to 10 batches of 30 entries per run. A successful purge deletes its entries; an "everything" purge also drops that website's other pending entries. Entries for a website with no API token or Zone ID, or whose API call fails, are marked `failed` and are not retried.

* **Turnstile API:** `env['cloudflare.turnstile'].verify_token(...)` evaluates tokens against the Cloudflare API `[@ANCHOR: cf_turnstile_verify]`.

* **Edge Context:** `env['cloudflare.utils'].get_request_context()` extracts geographic and threat data from trusted headers `[@ANCHOR: cf_get_request_context]`. "Trusted" means the `CF-*` headers are used only when the connecting peer is loopback (the Tunnel case) or inside the trusted Cloudflare IP range allow-list (empty unless an administrator turns on **Trust Cloudflare's Published IP Ranges** in Settings, for a deployment without a Tunnel); from any other peer every geographic and threat field is returned as `None`, so a direct request to the origin cannot forge them.

* **Tunnel Management:** Wizard generates installation commands `[@ANCHOR: cf_tunnel_setup]`. Sync and delete tunnels across accounts `[@ANCHOR: cf_sync_tunnels]`, `[@ANCHOR: cf_delete_tunnel]`.

* **Multi-website tunnels:** one server fronting several websites is the ordinary case here. The "Ensure Tunnel Daemon Running" cron keeps **every** tunnel that has credentials up `[@ANCHOR: ensure_tunnel_running]`, one `cloudflared` daemon per tunnel, each tracked under its own Cloudflare tunnel id: a running tunnel is never started twice, a dead one is restarted, and one website's Cloudflare failure never stops another's tunnel `[@ANCHOR: ensure_one_tunnel_running]`. "Routes pushed yet?" lives on the tunnel record (`routes_provisioned`); installs carrying the old single `cloudflare.tunnel.provisioned` system parameter have it folded onto the one tunnel it was really about, once `[@ANCHOR: migrate_global_provisioned_flag]`.

## 3. Automated Subsystems
* **Header Injection:** Injects `Cloudflare-CDN-Cache-Control` headers via `ir.http._post_dispatch` `[@ANCHOR: edge_cache:COMM_ir_http_post_dispatch_headers]`. This code, the first-visit cookie rules and the CSRF refresh script live in the `edge_cache` module (a dependency of this one, usable on its own by an instance with no Redis). Dynamic and sensitive routes `[@ANCHOR: cf_nocache_routes]` are excluded.

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

**Installation and configuration:**
* **Install Hook:** `[@ANCHOR: cloudflare:COMM_post_init_hook]` -- on module install, runs `initialize_cloudflare_state()` and then installs the module's `knowledge_docs` articles, both as the WAF service account.

* **Edge State Initialization:** `[@ANCHOR: cloudflare:COMM_initialize_cloudflare_state]` -- pulls existing WAF rulesets or pushes the default set, per website.

* **WAF Caller Authorization:** `[@ANCHOR: cloudflare:COMM_check_waf_caller_authorized]` -- gates `action_pull_waf_rules`/`action_push_waf_rules` on every call, regardless of `@distributed_cache()` state.

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

**Custom domain (`edge.routing.domain`) Cloudflare integration:**
* **Domain Create:** `[@ANCHOR: cloudflare:COMM_domain_create]` -- provisions a Cloudflare custom hostname for the matching website.

* **Website-Domain Mapping:** `[@ANCHOR: cloudflare:COMM_get_website_mapping]`

* **Custom Hostname Provisioning (Batch):** `[@ANCHOR: cloudflare:COMM_create_custom_hostname_batch]`

* **Custom Hostname Deletion (Batch):** `[@ANCHOR: cloudflare:COMM_delete_custom_hostname_batch]`

* **SSL Status Sync:** `[@ANCHOR: cloudflare:COMM_action_sync_ssl_status]`

**Edge context and purge dispatch:**
* **Current Website ID Resolution:** `[@ANCHOR: cloudflare:COMM_get_current_website_id]` -- unifies HTTP and cron contexts.

* **IP Ban Lift Sync:** `[@ANCHOR: cloudflare:COMM_action_lift_ban_sync]`

* **Purge Enqueue (URL/Tag):** `[@ANCHOR: cloudflare:COMM_enqueue_cloudflare_purge]`

* **Purge Menus:** `[@ANCHOR: cloudflare:COMM_purge_cloudflare_menus]` -- website.menu changes purge everything, not a single URL.

* **Purge Queue Batch Enqueue:** `[@ANCHOR: cloudflare:COMM_enqueue_urls_batch]`

* **Manual Purge Wizard Action:** `[@ANCHOR: cloudflare:COMM_purge_wizard_action_purge]`

* **Static Asset mtime-Triggered Purge:** `[@ANCHOR: cloudflare:COMM_trigger_edge_purge_static_assets]`

**Content bridge hooks (`bridge.py`):**
* **Page Write/Unlink:** `[@ANCHOR: cloudflare:COMM_page_write]`, `[@ANCHOR: cloudflare:COMM_page_unlink]`

* **Blog Post Write:** `[@ANCHOR: cloudflare:COMM_blog_post_write]`

* **Menu Write/Unlink:** `[@ANCHOR: cloudflare:COMM_menu_write]`, `[@ANCHOR: cloudflare:COMM_menu_unlink]`

* **Product Template Write:** `[@ANCHOR: cloudflare:COMM_product_write]`

**Settings shortcuts:**
* **Deploy/Pull WAF from Settings:** `[@ANCHOR: cloudflare:COMM_action_deploy_cf_waf]`, `[@ANCHOR: cloudflare:COMM_action_pull_cf_waf]`

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
