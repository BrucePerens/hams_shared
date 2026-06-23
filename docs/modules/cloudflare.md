# Cloudflare Edge Orchestration (`cloudflare`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

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

<stories_and_journeys>
## 5. Architectural Stories & Journeys

* [Asynchronous Cache Purging](docs/stories/cache_purging.md) `[@ANCHOR: story_cache_purging]`
* [Geo-Aware Request Context](docs/stories/request_context.md) `[@ANCHOR: story_request_context]`
* [Secure Edge Bridging via Tunnels](docs/stories/tunnels.md) `[@ANCHOR: story_tunnels]`
* [CAPTCHA Verification with Turnstile](docs/stories/turnstile_verification.md) `[@ANCHOR: story_turnstile]`
* [Automated WAF IP Banning](docs/stories/waf_banning.md) `[@ANCHOR: story_waf_banning]`

### Journeys
* [High-Performance Content Invalidation](docs/journeys/content_invalidation.md) `[@ANCHOR: journey_content_invalidation]`
* [Managing Edge Security](docs/journeys/edge_security.md) `[@ANCHOR: journey_edge_security]`
* [Infrastructure Provisioning](docs/journeys/infrastructure.md) `[@ANCHOR: journey_infrastructure]`
* [Intelligent Traffic Handling](docs/journeys/traffic_handling.md) `[@ANCHOR: journey_traffic_handling]`
</stories_and_journeys>
