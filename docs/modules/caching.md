# Caching PWA & Service Worker (`caching`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

This module optimizes the Odoo frontend performance by implementing a client-side CDN via a global Service Worker. It significantly reduces page load times and server load by caching static assets directly in the user's browser.

When a user loads a page, the Service Worker intercepts the requests for Odoo's JavaScript, CSS, and static module files. If the browser already has a copy of the file, it loads it instantly from the hard drive (0ms latency) without ever talking to the network.

## 🪄 How It Works (Zero-Config)

You do not need to do anything special to make your custom modules work with this cache.

The Service Worker automatically looks for requests matching these patterns:
* `/web/assets/...` (Odoo's compiled JS/CSS bundles)
* `/web/static/...` (Core Odoo static files)
* `/<your_module_name>/static/...` (Your custom module's frontend assets)

As long as you place your Javascript, CSS, and UI icons inside your module's standard `static/` directory, they will be cached automatically.

## 🔄 Automated Cache Invalidation

This module eliminates the need for manual version bumping or complex cache-busting query parameters.

**Filesystem-Linked Invalidation:**
- **Boot Scan:** During server startup, the module performs an efficient recursive scan using `os.scandir` of all `static/` directories across all installed modules ([@ANCHOR: caching_fs_scan_logic]).
- **MTime Tracking:** It identifies the latest modification timestamp (`mtime`) among all discovered assets.
- **Dynamic SW Generation:** This timestamp is injected into the `/sw.js` payload, effectively versioning the Service Worker script itself.
- **Automatic Refresh:** When any static file is modified and the server restarts, the Service Worker's signature changes. Browsers detect this update on the next visit, triggering a background installation of the new worker and an immediate purge of the stale cache.

## 🚨 The File-Size Caveat & Safety Valve

Browsers give Service Workers a strict storage limit. If a Service Worker tries to cache massive files, it will max out the quota and the browser will panic and delete the entire cache—destroying the performance benefits of this module.

**The Dynamic Safety Valve:** To protect against this, our Service Worker runs a mathematical calculation on the server during boot. It sums up the sizes of all static files across all installed modules. If the total size exceeds the safe limits of the browser's cache quota (configurable, default 35MB), it automatically calculates a dynamic max file size limit. It instructs the browser to cache as many small files as possible while intentionally rejecting the largest, heaviest files, keeping the total cache footprint safely underneath the browser's panic threshold.

**The Golden Rule:** Keep your `static/` folders strictly reserved for lightweight UI code (JS, CSS) and small layout graphics. If you need to serve heavy media, user uploads, or large datasets, use Odoo's standard attachment routes (`/web/image` or `/web/content`). The Service Worker explicitly ignores those routes, allowing Cloudflare to handle the heavy lifting safely.

---

# Technical Documentation

**Context:** Technical documentation strictly for LLMs and Integrators.

## 1. Overview
Implements a global, root-scoped Service Worker (`/sw.js`) that proxies and caches frontend assets locally in the browser to provide near-instant load times.

## 2. Integration Rules
* Assets placed in your module's `static/` directory are cached automatically.
* **No Competing Workers:** DO NOT attempt to register another Service Worker.
* **WebSockets:** `ws://` protocols are hardcoded to bypass the proxy.
* **Dynamic Large File Prohibition**: The worker mathematically calculates an active quota limit (approx 35MB) [@ANCHOR: caching_quota_calculation]. Heavy media MUST route via `/web/image` to prevent the cache from ejecting critical UI bundles.
* **Layout Injection**: The service worker registration script is injected globally into the frontend `website.layout` via XPath [@ANCHOR: xpath_rendering_caching_layout].
* **Settings Layout Injection**: The settings UI is injected into `website.res_config_settings_view_form` via XPath [@ANCHOR: xpath_rendering_caching_settings].

## 3. Zero-Sudo Architecture
This module is built with security as a primary concern, adhering strictly to the Zero-Sudo architecture:
- **Micro-Privileged Service Account**: A dedicated service user `caching.user_caching_service` is utilized for the filesystem scan ([@ANCHOR: caching_fs_scan_logic]). This account has zero access to business data.
- **Secure Parameter Access**: System parameters are retrieved through the `zero_sudo.security.utils` abstraction layer, preventing direct access to `ir.config_parameter` and maintaining strict audit trails.
- **Configuration Whitelisting**: Only specifically approved parameters (`caching.safe_quota_mb`, `caching.invalidation_version`) are accessible to the caching service, preventing unauthorized configuration leakage.
- **No Sudo Escalation**: All background operations run within the context of their assigned service accounts without ever requesting global administrative (`sudo`) privileges.

## 4. Stories & Journeys
Detailed architectural narratives and process flows are documented in the `docs/` directory:

### Stories
* [Cache Quota Management](docs/stories/cache_quota_management.md) ([@ANCHOR: caching_quota_calculation])
* [Cache Invalidation Strategy](docs/stories/cache_invalidation_strategy.md) ([@ANCHOR: caching_fs_scan_logic])
* [Documentation Bootstrap](docs/stories/documentation_bootstrap.md) ([@ANCHOR: caching_docs_bootstrap])

### Journeys
* [Asset Request Flow](docs/journeys/asset_request_flow.md) ([@ANCHOR: caching_sw_fetch_interceptor])
* [Server Startup Scan](docs/journeys/server_startup_scan.md) ([@ANCHOR: caching_sw_serve_route])
* [Manual Invalidation](docs/journeys/manual_invalidation.md) ([@ANCHOR: test_caching_sudo_params])

## 5. Testing
Tests are located in the `tests/` directory and cover:
- Service Worker delivery and headers [@ANCHOR: caching_sw_serve_route].
- Quota calculation logic [@ANCHOR: caching_quota_calculation].
- Cache invalidation triggers.
- UI Tour for registration check [@ANCHOR: caching_sw_fetch_interceptor].
- Zero-Sudo compliance for FS scan [@ANCHOR: caching_fs_scan_logic].
