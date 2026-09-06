# Cloudflare Edge Orchestration (`cloudflare`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

This module acts as the command center for your Cloudflare CDN and Web Application Firewall (WAF). It automates edge caching, security, and IP bans across multiple websites, eliminating the need to manually manage these settings in the Cloudflare dashboard.

## 🌟 What It Does

* **Multi-Website Support:** Seamlessly manage multiple domains/zones from a single Odoo instance. Credentials and settings are isolated per website.
* **Automated Static Caching & Purging:** It forces Cloudflare to aggressively cache static assets (like images, CSS, and JS) for a full year. If any static file is modified, the module detects it during boot and automatically triggers a global purge across all configured zones.
* **Intelligent Content Invalidation:** Automatically enqueues purges for specific URLs when website pages, blog posts, or products are edited.
* **WAF Management:** Build, backup, and deploy Cloudflare Firewall rules directly from the Odoo backend.
* **Honeypot IP Banning:** Instantly ban malicious IPs at the network edge when they trigger honeypot traps.
* **Zero Trust Tunnels:** Provision and manage `cloudflared` tunnels directly from Odoo.
* **Turnstile Integration:** Backend validator for Cloudflare's invisible Turnstile CAPTCHA.
* **Zone Settings Control:** Adjust security levels, development mode, and cache TTL per website.

## 🛠️ How to Set It Up

1. Ensure the `cloudflare` module is in your Odoo `addons` directory.
2. Configure credentials in **Settings > Website > Cloudflare Edge**:
   * `CF API Token` (Requires `Zone.Cache Purge`, `Zone.Firewall Services`, and `Account.Cloudflare Tunnel` permissions)
   * `CF Zone ID`
   * `CF Account ID` (Required for Zero Trust Tunnels)
3. For global defaults, you can also set `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, and `CLOUDFLARE_ACCOUNT_ID` in your server's `.env` file.

---

# Technical Documentation

## 1. Overview
Control plane for the CDN edge. Manages Cache-Tags, WAF bans, and Turnstile CAPTCHA verification to offload processing to Cloudflare's edge.

## 2. API Interfaces
* **WAF IP Banning:** `env['cloudflare.waf'].ban_ip(...)` dynamically injects firewall rules `[@ANCHOR: cf_execute_ban]`. Supports multiple websites.

* **WAF Management:** Pull `[@ANCHOR: cf_action_pull_waf_rules]` and push `[@ANCHOR: cf_action_push_waf_rules]` firewall rules.

* **Cache Purging:** `env['cloudflare.purge.queue'].enqueue_urls(...)` and `enqueue_tags(...)`. Processes an asynchronous queue grouped by website to prevent credential mixing `[@ANCHOR: cf_process_queue_logic]`.

* **Turnstile API:** `env['cloudflare.turnstile'].verify_token(...)` evaluates tokens against the Cloudflare API `[@ANCHOR: cf_turnstile_verify]`.

* **Edge Context:** `env['cloudflare.utils'].get_request_context()` extracts geographic and threat data from trusted headers `[@ANCHOR: cf_get_request_context]`.

* **Tunnel Management:** Wizard generates installation commands `[@ANCHOR: cf_tunnel_setup]`. Sync and delete tunnels across accounts `[@ANCHOR: cf_sync_tunnels]`, `[@ANCHOR: cf_delete_tunnel]`.

## 3. Automated Subsystems
* **Header Injection:** Injects `Cloudflare-CDN-Cache-Control` headers via `ir.http._post_dispatch` `[@ANCHOR: ir_http_post_dispatch_headers]`. Dynamic and sensitive routes `[@ANCHOR: cf_nocache_routes]` are excluded.
* **Boot-time Sync:** Scans `static/` folders on boot and invalidates `odoo-static-assets` across all zones if changes are detected.
* **Content Hooks:** Automatically enqueues purges for `website.page`, `blog.post`, and `product.template` modifications.

## 4. Zero-Sudo & Micro-Privilege Architecture
Strictly adheres to Zero-Sudo architecture using dedicated service accounts:
* `cloudflare.user_cloudflare_purge`: Cache purging.
* `cloudflare.user_cloudflare_waf`: WAF and IP banning.
* `cloudflare.user_cloudflare_tunnel`: Tunnel management.

---

## 4b. Stage 1 Anchor-Coverage Sweep Additions

**Installation and configuration:**
* **Daemon/Install Hook:** `[@ANCHOR: cloudflare:COMM_post_init_hook]` -- initializes WAF state and bootstraps documentation on module install.

* **Edge State Initialization:** `[@ANCHOR: cloudflare:COMM_initialize_cloudflare_state]` -- pulls existing WAF rulesets or pushes the default set, per website.

* **WAF Caller Authorization:** `[@ANCHOR: cloudflare:COMM_check_waf_caller_authorized]` -- gates `action_pull_waf_rules`/`action_push_waf_rules` on every call, regardless of `@distributed_cache()` state.

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
* **Fernet Key Resolution:** `[@ANCHOR: cloudflare:COMM_get_fernet]` -- reads from the daemon key registry, never an environment variable, to preserve multi-tenant isolation.

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
