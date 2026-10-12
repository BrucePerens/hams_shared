# Caching PWA & Service Worker (`caching`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

This module optimizes the Odoo frontend performance by implementing a client-side CDN (content delivery network: here the browser's own cache storage serves the assets, as a CDN edge server would) via a global Service Worker. It significantly reduces page load times and server load by caching static assets directly in the user's browser.

When a user loads a page, the Service Worker intercepts the requests for Odoo's JavaScript, CSS, and static module files. If the browser already has a copy of the file, it loads it from the browser's cache storage without ever talking to the network. Only successful (HTTP 200) same-origin `GET` responses are stored.

## 🪄 How It Works (Zero-Config)

You do not need to do anything special to make your custom modules work with this cache.

The Service Worker automatically looks for requests matching these patterns:
* `/web/assets/...` (Odoo's compiled JS/CSS bundles)
* `/web/static/...` (Core Odoo static files)
* `/<your_module_name>/static/...` (Your custom module's frontend assets)

As long as you place your Javascript, CSS, and UI icons inside your module's standard `static/` directory, they will be cached automatically.

Some requests are never cached even when they match: `/static/description/images/` (module documentation images) and anything under `/my/`, `/api/`, `/web/image/` or `/web/content/`. Page navigations are not cached either: they always go to the network and fall back to `/offline` only when it fails. A cached file is served without asking the server for a newer copy, so a changed file at the same URL is not picked up until the cache name changes (see below); Odoo's compiled bundles avoid this because their URL carries a hash (`/web/assets/<hash>/<bundle>`), and when the worker caches a bundle under a new hash it deletes the copy under the old one.

## 🔄 Automated Cache Invalidation

This module eliminates the need for manual version bumping or complex cache-busting query parameters.

**Filesystem-Linked Invalidation:**
- **Filesystem Scan:** Nothing scans during server startup itself. When `/sw.js` is requested and no cached scan result exists, the module performs an efficient recursive scan using `os.scandir` of all `static/` directories across all installed modules ([@ANCHOR: caching_fs_scan_logic]). Hidden files (starting with `.`) and `static/description/` are skipped and symbolic links are not followed. The result is cached through `distributed_redis_cache`'s `@distributed_cache()`: in each worker's memory, and in Redis for up to 24 hours.
- **MTime Tracking:** It identifies the latest modification timestamp (`mtime`) among all discovered assets.
- **Dynamic SW Generation:** This timestamp is injected into the `/sw.js` payload as part of the cache name `odoo-assets-cache-<mtime>-v<version>` (the version is the website's invalidation version), effectively versioning the Service Worker script itself. The per-file size limit and the quota are injected the same way.
- **Automatic Refresh:** When a new scan finds a different latest modification time, the Service Worker's signature changes. A server restart alone does not guarantee a new scan: it empties only each worker's in-memory copy of the result, and the Redis copy can survive up to 24 hours (the `/sw.js` text is also cached in Redis for 24 hours). Browsers detect the change on the next visit, triggering a background installation of the new worker, which purges this module's stale `odoo-assets-cache-*` caches. To force it, use **Invalidate Cache Now** in Website Settings: it changes only that website's cache version and so works even while the scan result is stale.

## 🚨 The File-Size Caveat & Safety Valve

Browsers give Service Workers a strict storage limit. If a Service Worker tries to cache massive files, it will max out the quota and the browser will panic and delete the entire cache—destroying the performance benefits of this module.

**The Dynamic Safety Valve:** To protect against this, the server runs a calculation each time it builds `/sw.js` (not at startup). It sums up the sizes of all static files found by the scan. The quota is configurable per website (default 35MB) and 10MB of it is always set aside for Odoo's `/web/assets/` bundles and overhead. If the files exceed the remaining quota, the module drops the largest files one at a time until the rest fit and sets the per-file limit just below the size of the last file dropped, so as many small files as possible are cached; the bundles themselves are exempt from that per-file limit. If everything fits, the limit is the larger of 10MB and the largest file plus 1KB; if the quota is 10MB or less, the limit is 0 and no module static file is cached.

The browser side enforces the quota too: the worker records when each non-bundle file was last used (in IndexedDB) and, when this site's storage use exceeds the quota after caching a file, deletes the 10 least recently used non-bundle files.

**The Golden Rule:** Keep your `static/` folders strictly reserved for lightweight UI code (JS, CSS) and small layout graphics. If you need to serve heavy media, user uploads, or large datasets, use Odoo's standard attachment routes (`/web/image` or `/web/content`). The Service Worker explicitly ignores those routes, allowing Cloudflare to handle the heavy lifting safely.

---

# Technical Documentation

**Context:** Technical documentation strictly for LLMs and Integrators.

## 1. Overview
Implements a global, root-scoped Service Worker (`/sw.js`) that proxies and caches frontend assets locally in the browser to provide near-instant load times.

## 2. Integration Rules
* Assets placed in your module's `static/` directory are cached automatically.
* **No Competing Workers:** DO NOT attempt to register another Service Worker.
* **WebSockets:** `ws://` and `wss://` protocols are hardcoded to bypass the proxy.
* **Dynamic Large File Prohibition**: The server calculates a per-file size limit from the website's quota (default 35MB) and injects it into the worker [@ANCHOR: caching_quota_calculation]. Heavy media MUST route via `/web/image` to prevent the cache from ejecting critical UI bundles.

* **Layout Injection**: The service worker registration script is injected globally into the frontend `website.layout` via XPath [@ANCHOR: xpath_rendering_caching_layout].

* **Settings Layout Injection**: The settings UI is injected into `website.res_config_settings_view_form` via XPath [@ANCHOR: xpath_rendering_caching_settings].

## 3. Zero-Sudo Architecture
This module is built with security as a primary concern, adhering strictly to the Zero-Sudo architecture (no `.sudo()`; privileged work runs as a dedicated, narrowly scoped service account -- defined in [zero_sudo.md](zero_sudo.md), accounts catalogued in [service_accounts.md](../service_accounts.md)):
- **Micro-Privileged Service Account**: A dedicated service user `caching.user_caching_service` is utilized for the filesystem scan ([@ANCHOR: caching_fs_scan_logic]). This account has zero access to business data: its only rights are read access to module records (`ir.module.module`) and to system parameters whose key begins with `caching.`.
- **Secure Parameter Access**: System parameters (in this module, only `caching.enable_sw_test_hooks`, which turns on the Service Worker's test-only message hooks and is off unless set to `true` or `1`) are retrieved through the `zero_sudo.security.utils` abstraction layer, preventing direct access to `ir.config_parameter` and maintaining strict audit trails.
- **Configuration Whitelisting**: Only specifically approved parameters are readable through that layer, preventing unauthorized configuration leakage. `caching.enable_sw_test_hooks` is on `zero_sudo`'s whitelist. The quota and the invalidation version are not system parameters: they are per-website fields, `website.caching_safe_quota_mb` and `website.caching_invalidation_version`, read by `controllers/main.py` (the older names `caching.safe_quota_mb` and `caching.invalidation_version` survive only in some story and journey texts).
- **No Sudo Escalation**: All background operations run within the context of their assigned service accounts without ever requesting global administrative (`sudo`) privileges.

## 4. Stories & Journeys
Detailed architectural narratives and process flows are documented in the `hams_shared/docs/` directory:

### Stories
* [Cache Quota Management](hams_shared/docs/stories/cache_quota_management.md) ([@ANCHOR: caching_quota_calculation])

* [Cache Invalidation Strategy](hams_shared/docs/stories/cache_invalidation_strategy.md) ([@ANCHOR: caching_fs_scan_logic])

* [Documentation Bootstrap](hams_shared/docs/stories/documentation_bootstrap.md) ([@ANCHOR: caching_docs_bootstrap])

### Journeys
* [Asset Request Flow](hams_shared/docs/journeys/asset_request_flow.md) ([@ANCHOR: caching_sw_fetch_interceptor])

* [Server Startup Scan](hams_shared/docs/journeys/server_startup_scan.md) ([@ANCHOR: caching_sw_serve_route])

* [Manual Invalidation](hams_shared/docs/journeys/manual_invalidation.md) ([@ANCHOR: test_caching_sudo_params])

## 4b. Stage 1 Anchor-Coverage Sweep Additions
* **PWA Manifest Route:** `[@ANCHOR: caching:COMM_pwa_manifest]` -- `/manifest.json`, reflects the current website's name/theme colors.

* **PWA Offline Fallback Route:** `[@ANCHOR: caching:COMM_pwa_offline_route]` -- `/offline`.

* **Manual Cache-Model Invalidation:** `[@ANCHOR: caching:COMM_force_invalidate_cache]` -- `caching.mixin.force_invalidate_cache()`, drops the distributed Redis cache entry for the calling model.

* **Settings Read:** `[@ANCHOR: caching:COMM_settings_get_values]` -- `res.config.settings.get_values()`, reads the quota/invalidation-version fields from the current website.

* **Settings Write:** `[@ANCHOR: caching:COMM_settings_set_values]` -- `res.config.settings.set_values()`, writes the quota back to the website.

* **Force Cache Invalidation Action:** `[@ANCHOR: caching:COMM_settings_force_cache_invalidation]` -- increments the website's cache invalidation version.

## 5. Testing
Tests are located in the `tests/` directory and cover:
- Service Worker delivery and headers [@ANCHOR: caching_sw_serve_route].

- Quota calculation logic [@ANCHOR: caching_quota_calculation].
- Cache invalidation triggers.
- UI Tour for registration check [@ANCHOR: caching_sw_fetch_interceptor].

- Zero-Sudo compliance for FS scan [@ANCHOR: caching_fs_scan_logic].
