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
- **Filesystem Scan:** Nothing scans during server startup itself. When `/sw.js` is requested and no cached scan result exists, the module performs an efficient recursive scan using `os.scandir` of all `static/` directories across all installed modules (`caching_fs_scan_logic`). Hidden files (starting with `.`) and `static/description/` are skipped and symbolic links are not followed. The result is cached through `distributed_redis_cache`'s `@distributed_cache()`: in each worker's memory, and in Redis for up to 24 hours.
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
* **Dynamic Large File Prohibition**: The server calculates a per-file size limit from the website's quota (default 35MB) and injects it into the worker `caching_quota_calculation`. Heavy media MUST route via `/web/image` to prevent the cache from ejecting critical UI bundles.

* **Layout Injection**: The service worker registration script is injected globally into the frontend `website.layout` via XPath `xpath_rendering_caching_layout`.

* **Settings Layout Injection**: The settings UI is injected into `website.res_config_settings_view_form` via XPath `xpath_rendering_caching_settings`.

## 3. Zero-Sudo Architecture
This module is built with security as a primary concern, adhering strictly to the Zero-Sudo architecture (no `.sudo()`; privileged work runs as a dedicated, narrowly scoped service account -- defined in [zero_sudo.md](zero_sudo.md), accounts catalogued in [service_accounts.md](../service_accounts.md)):
- **Micro-Privileged Service Account**: A dedicated service user `caching.user_caching_service` is utilized for the filesystem scan (`caching_fs_scan_logic`). This account has zero access to business data: its only rights are read access to module records (`ir.module.module`) and to system parameters whose key begins with `caching.`.
- **Secure Parameter Access**: System parameters (in this module, only `caching.enable_sw_test_hooks`, which turns on the Service Worker's test-only message hooks and is off unless set to `true` or `1`) are retrieved through the `zero_sudo.security.utils` abstraction layer, preventing direct access to `ir.config_parameter` and maintaining strict audit trails.
- **Configuration Whitelisting**: Only specifically approved parameters are readable through that layer, preventing unauthorized configuration leakage. `caching.enable_sw_test_hooks` is on `zero_sudo`'s whitelist. The quota and the invalidation version are not system parameters: they are per-website fields, `website.caching_safe_quota_mb` and `website.caching_invalidation_version`, read by `controllers/main.py` (the older names `caching.safe_quota_mb` and `caching.invalidation_version` survive only in some story and journey texts).
- **No Sudo Escalation**: All background operations run within the context of their assigned service accounts without ever requesting global administrative (`sudo`) privileges.

## 4. Stories & Journeys
Detailed architectural narratives and process flows are documented in the `hams_shared/docs/` directory:

### Stories
* [Cache Quota Management](hams_shared/docs/stories/cache_quota_management.md) (`caching_quota_calculation`)

* [Cache Invalidation Strategy](hams_shared/docs/stories/cache_invalidation_strategy.md) (`caching_fs_scan_logic`)

* [Documentation Bootstrap](hams_shared/docs/stories/documentation_bootstrap.md) (`caching_docs_bootstrap`)

### Journeys
* [Asset Request Flow](hams_shared/docs/journeys/asset_request_flow.md) (`caching_sw_fetch_interceptor`)

* [Server Startup Scan](hams_shared/docs/journeys/server_startup_scan.md) (`caching_sw_serve_route`)

* [Manual Invalidation](hams_shared/docs/journeys/manual_invalidation.md) (`test_caching_sudo_params`)

## 4b. Stage 1 Anchor-Coverage Sweep Additions
* **PWA Manifest Route:** `caching:COMM_pwa_manifest` -- `/manifest.json`, reflects the current website's name/theme colors.

* **PWA Offline Fallback Route:** `caching:COMM_pwa_offline_route` -- `/offline`.

* **Manual Cache-Model Invalidation:** `caching:COMM_force_invalidate_cache` -- `caching.mixin.force_invalidate_cache()`, drops the distributed Redis cache entry for the calling model.

* **Settings Read:** `caching:COMM_settings_get_values` -- `res.config.settings.get_values()`, reads the quota/invalidation-version fields from the current website.

* **Settings Write:** `caching:COMM_settings_set_values` -- `res.config.settings.set_values()`, writes the quota back to the website.

* **Force Cache Invalidation Action:** `caching:COMM_settings_force_cache_invalidation` -- increments the website's cache invalidation version.

## 5. Testing
Tests are located in the `tests/` directory and cover:
- Service Worker delivery and headers `caching_sw_serve_route`.

- Quota calculation logic `caching_quota_calculation`.
- Cache invalidation triggers.
- UI Tour for registration check `caching_sw_fetch_interceptor`.

- Zero-Sudo compliance for FS scan `caching_fs_scan_logic`.

## Anchor reference

One entry per anchor, written from the code in `caching/` (`models/caching_mixin.py`, `controllers/main.py`, `controllers/pwa.py`, `models/res_config_settings.py`, `static/src/sw/sw.js`). Several ids are written here without the `COMM_` prefix that the in-code anchor carries. Tests are in `caching/tests/`.

### `caching_fs_scan_logic` [@ANCHOR: caching_fs_scan_logic]
Purpose: find out when and how large the static files of all installed modules are, so the service worker can be versioned and sized. `caching.mixin.get_fs_stats(override_svc_uid=None)` (anchored in code as `COMM_caching_fs_scan_logic`) is `@api.private @api.model @distributed_cache()`, so it cannot be called over RPC and its result is cached by `distributed_redis_cache` in worker memory and in Redis (up to 24 hours).

| Argument | Type | Meaning |
|---|---|---|
| `override_svc_uid` | int or None | When given, the method switches to that user (`with_user`); the `/sw.js` route passes the `caching.user_caching_service` account so the scan never runs as the visitor. |

Returns a tuple `(latest_mtime, file_sizes)`: the newest modification time (float seconds, `0.0` if nothing found) and a list of file sizes in bytes sorted largest first. Behaviour: for every installed module with a `static/` directory it walks the tree with `os.scandir` recursively, skipping names starting with `.` and `static/description`, and not following symbolic links; an unreadable directory is logged and skipped. Blocking file system I/O, proportional to the number of static files, so the cache matters. Tests: `test_05_zero_sudo_scan` and `test_01_quota_config_updates_sw` in `tests/test_settings_and_cache.py`.

### `caching_quota_calculation` [@ANCHOR: caching_quota_calculation]
Purpose: turn the per-website quota into the two numbers injected into the service worker. `caching.mixin.get_global_static_info(quota_mb, override_svc_uid=None)` (anchored in code as `COMM_caching_quota_calculation`) is `@api.private @api.model @distributed_cache()`.

| Argument | Type | Meaning |
|---|---|---|
| `quota_mb` | int | Website quota in megabytes (default 35). |
| `override_svc_uid` | int or None | Service account to run as, passed on to `get_fs_stats`. |

Returns `(latest_mtime, max_file_size)`, both as strings (whole seconds; bytes). The safe quota is `max(0, quota_mb - 10)` MB, because 10 MB is reserved for Odoo bundles. If the safe quota is 0 the limit is `"0"` (no module file cached); with no static files it is 10 MB; if all files fit it is the larger of 10 MB and the largest file plus 1 KB; otherwise the largest files are dropped one at a time until the rest fit and the limit is just under the last dropped file (size minus 1). Pure computation over the scan result; no writes. Tests: `test_01_quota_config_updates_sw`, `test_06_quota_edge_cases` in `tests/test_settings_and_cache.py`.

### `xpath_rendering_caching_layout`
Status: unknown, to-do. This document says the registration script is injected into `website.layout` by an XPath, but the current code does not do that: `caching/__manifest__.py` adds `static/src/js/register.js` (which calls `navigator.serviceWorker.register('/sw.js')` when the browser supports it, and logs success or failure) and `toast.js` to the `web.assets_frontend` bundle, and there is no template inheriting `website.layout`. No file in `caching/` carries an anchor with this id, so it exists only in this document. To-do: either add the anchor to `register.js` and reword the sentence, or drop the id from the document. Behaviour today: every frontend page loads the bundle and registers the one root-scope worker; a failure is logged to the console and does not break the page. Related: `caching_sw_serve_route` (the script that is registered).

### `xpath_rendering_caching_settings` [@ANCHOR: xpath_rendering_caching_settings]
Purpose: the settings screen block of the module. `views/res_config_settings_views.xml` inherits `website.res_config_settings_view_form` and, with one XPath (`//block[@id='website_settings']`, position `after`), adds the block "Caching Service Worker" with four settings: Safe Quota (field `caching_safe_quota_mb`, MB), Force Cache Invalidation (read-only `caching_invalidation_version` plus the button "Invalidate Cache Now", `action_force_cache_invalidation`), PWA Theme Color and PWA Background Color (colour widgets). The in-code anchor is `COMM_xpath_rendering_caching_settings`.

Special concerns: the XPath depends on the block id in Odoo's own website settings view, so an Odoo upgrade that renames it breaks the view at load. Only users who can open Website Settings see it. Test: `test_04_xpath_rendering_settings` in `tests/test_settings_and_cache.py` (cited there as `test_xpath_rendering_caching_settings`).

### `caching_docs_bootstrap`
Status: unknown, to-do. The anchor names the installation of this module's own manual. The file `caching/data/documentation.html` exists, but the manifest has no `knowledge_docs` entry for it, and the `post_init_hook` in `caching/__init__.py` (`_post_init_hook(env)`, docstring "Inject documentation") has a body of only `pass`, so nothing installs it. No code carries the anchor. The generic installer is `ir.module.module._bootstrap_knowledge_docs()` in `zero_sudo` (see `manual_doc_auto_install`), which reads `knowledge_docs` from the manifest. To-do: add the manifest entry (with `public`/`help_topic` if members should see it) or remove the empty hook and the story.

### `caching_sw_fetch_interceptor` [@ANCHOR: caching_sw_fetch_interceptor]
Purpose: the service worker's `fetch` listener in `caching/static/src/sw/sw.js` (anchored in code as `COMM_caching_sw_fetch_interceptor`), which decides for every request whether to serve it from the cache or the network. It runs inside the browser's service worker, not on the server; the `event` argument is a `FetchEvent`.

Rules in order: only `GET` is handled; the Chrome `only-if-cached` cross-origin case, `ws:`/`wss:`, paths under `/my/`, `/api/`, `/web/image/`, `/web/content/` and `/static/description/images/` are left to the browser. A page navigation goes to the network and falls back to the cached `/offline` page (or a built-in response). Only paths matching `CACHE_URL_REGEX` (`/web/assets/...` and `/<module>/static/...`) are intercepted: a cached copy is returned without asking the server; otherwise the network response is returned and, if it is status 200 and same-origin (`basic`), stored in the cache named `__CACHE_NAME__` unless it is larger than the injected maximum file size (checked from `Content-Length`, or measured when that is absent). A full quota (`QuotaExceededError`) triggers one LRU eviction and a retry. Caching a new `/web/assets/<hash>/<bundle>` deletes the old hash of the same bundle. The cache write is not awaited, so it never delays the page. Tests: `test_01_sw_fetch_presence`, `test_02_sw_regex_logic` (`tests/test_sw_fetch.py`) and the tours in `tests/test_tour.py`.

### `caching_sw_serve_route` [@ANCHOR: caching_sw_serve_route]
Purpose: serve the service worker script from the site root so its scope covers the whole site. `ServiceWorkerController.service_worker()` in `controllers/main.py` (anchored in code as `COMM_caching_sw_serve_route`) handles `GET /sw.js` (`auth="public"`, `website=True`, not in the sitemap); it takes no arguments.

Behaviour: reads the template `caching/static/src/sw/sw.js` (from the Redis key `caching_sw_js_content`, TTL 86400 s, or from disk; a Redis failure is logged and the disk copy is used, so the route never fails because of Redis; a missing file gives HTTP 404). It gets the current website's quota (`caching_safe_quota_mb`, default 35) and invalidation version (default 1), runs `get_global_static_info` as the `caching.user_caching_service` account (a context flag `force_fs_scan` first drops the cached scan) and replaces the placeholders `__CACHE_NAME__` (`odoo-assets-cache-<mtime>-v<version>`), `__MAX_FILE_SIZE_BYTES__`, `__MAX_STORAGE_BYTES__` and `__TEST_HOOKS_ENABLED__` (true only when the system parameter `caching.enable_sw_test_hooks` is `1` or `true`). Returns `application/javascript` with `Cache-Control: no-cache, max-age=0` so browsers re-check the worker on every visit. No user data is involved. Tests: `test_01_sw_headers`, `test_01b_sw_still_serves_when_redis_is_unreachable` in `tests/test_service_worker.py`.

### `test_caching_sudo_params` [@ANCHOR: test_caching_sudo_params]
Test anchor `COMM_test_caching_sudo_params`: `test_03_caching_sudo_params` in `tests/test_settings_and_cache.py`. It sets a system parameter through `zero_sudo.security.utils._set_system_param` and reads it back with `_get_system_param`, to show that the module reads parameters through the zero_sudo abstraction rather than `ir.config_parameter` directly. The test uses the legacy key `caching.safe_quota_mb`; the real quota now lives in the website field `caching_safe_quota_mb`, and the only parameter the code reads is `caching.enable_sw_test_hooks`. To-do: update the test to that key so it covers the real path. The anchor is also cited at the foot of `controllers/main.py`.

### `caching:COMM_pwa_manifest` [@ANCHOR: caching:COMM_pwa_manifest]
Purpose: the web app manifest that lets a browser offer to install the site. `PWAController.manifest()` in `controllers/pwa.py` handles `GET /manifest.json` (`auth="public"`, `website=True`, not in the sitemap) with no arguments and returns a JSON document, `Content-Type: application/json`. Fields: `name` and `short_name` (the website's name, or "Odoo PWA" without a website), `start_url` `/`, `display` `standalone`, `background_color` and `theme_color` from the website fields `caching_pwa_background_color` (default `#ffffff`) and `caching_pwa_theme_color` (default `#875A7B`), and one icon, `/web/static/img/logo.png`, 192x192. Read-only, no caching of its own, no user data. Test: `test_11_manifest_json_reflects_the_current_website` in `tests/test_settings_and_cache.py`.

### `caching:COMM_pwa_offline_route` [@ANCHOR: caching:COMM_pwa_offline_route]
Purpose: the page the service worker shows when a navigation fails because the network is down. `PWAController.offline()` handles `GET /offline` (`auth="public"`, `website=True`, not in the sitemap), takes no arguments and returns the rendered QWeb template `caching.pwa_offline_template` (`data/pwa_offline_template.xml`, built on `website.layout`). The worker finds this page with `caches.match('/offline')` and otherwise falls back to a built-in response. The route holds no data and has no side effects. Test: `test_02_offline_fallback_page_renders` in `tests/test_service_worker.py` and the offline tour in `tests/test_tour.py`.

### `caching:COMM_force_invalidate_cache` [@ANCHOR: caching:COMM_force_invalidate_cache]
Purpose: drop the cached result of this module's scan. `caching.mixin.force_invalidate_cache()` is `@api.private @api.model`, takes no arguments and returns nothing; it calls `invalidate_model_cache(env, "caching.mixin")` from `distributed_redis_cache`, which removes the Redis entries of the `@distributed_cache` methods of the model and tells other workers to clear their memory copies. The `/sw.js` route calls it (as the caching service account) when the context flag `force_fs_scan` is set. Side effects: the next `/sw.js` request re-scans the file system. If Redis is unreachable the underlying call degrades as documented in `distributed_redis_cache`. Test: `test_10_force_invalidate_cache_calls_the_real_redis_invalidation` in `tests/test_settings_and_cache.py`.

### `caching:COMM_settings_get_values` [@ANCHOR: caching:COMM_settings_get_values]
Purpose: fill the settings screen with the current website's caching values. `res.config.settings.get_values()` is an `@api.model` `@public_rpc` override with no arguments. It calls the standard method, then determines the website from the context key `website_id` or, failing that, `website.get_current_website()` (a `ValueError` is logged), and adds `caching_safe_quota_mb` and `caching_invalidation_version` from that website; without a website the defaults 35 and 1 are used. Returns the values dictionary. Read-only. Because the settings model is transient, the quota shown is per website, not global. Test: `test_08_get_values_reads_from_current_website` in `tests/test_settings_and_cache.py`.

### `caching:COMM_settings_set_values` [@ANCHOR: caching:COMM_settings_set_values]
Purpose: save the quota chosen on the settings screen. `res.config.settings.set_values()` is a `@public_rpc` override with no arguments and no return value: it calls the standard method and, when `website_id` is set, writes `caching_safe_quota_mb` to that website. The invalidation version is read-only here and is changed only by `action_force_cache_invalidation`. A changed quota takes effect the next time `/sw.js` is built (the cached `get_global_static_info` result is keyed by the quota value). Access follows Odoo's rules for the settings model (administrators). Test: `test_09_set_values_writes_back_to_website` in `tests/test_settings_and_cache.py`.

### `caching:COMM_settings_force_cache_invalidation` [@ANCHOR: caching:COMM_settings_force_cache_invalidation]
Purpose: the "Invalidate Cache Now" button. `res.config.settings.action_force_cache_invalidation()` is `@public_rpc` and needs one record. When the settings record has a website it adds 1 to that website's `caching_invalidation_version`, which changes the cache name in `/sw.js` (`...-v<version>`), so every browser installs a new worker and deletes the old `odoo-assets-cache-*` caches. Returns the client action `{"type": "ir.actions.client", "tag": "reload"}` so the settings page reloads and shows the new version. It affects only that website and works even when the file scan result is stale. Test: `test_02_force_invalidation` in `tests/test_settings_and_cache.py`.
