# Distributed Redis Cache (`distributed_redis_cache`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

The Distributed Redis Cache module replaces Odoo's per-process method cache (`@tools.ormcache`) with a high-performance, distributed Redis backend: methods decorated with `@distributed_cache()` (see the Developer & AI Usage Guide below) share their cached results through Redis instead of each worker computing and holding its own copy. Odoo's own ORM record cache is not modified. This enables true horizontal scaling for Odoo Community by enforcing phase coherence across multiple WSGI workers and completely separate physical web servers. "Phase coherence" here means: once any worker signals that cached data changed, every other worker discards its in-memory copies before it serves its next HTTP request or runs its next cron job, so workers do not drift onto different versions of the same cached data (see Architecture below).

## 🛠️ Dependencies & Installation

Because this is a standalone Open Source module that relies on an external Python daemon (`daemons/cache_manager.py`, which you run as its own systemd service; the module does not start it), you must ensure its system dependencies are satisfied before installing it in your database. The module will fail-fast at startup if these are missing.

**External Dependencies:** `python-dotenv`

**Required Python Modules:** `redis`, `asyncpg`, `python-dotenv`

* **Debian/Ubuntu Installation:**
    ```bash
    sudo apt-get install python3-redis python3-asyncpg python3-dotenv
    ```
* **Pip Installation:**
    ```bash
    pip3 install redis asyncpg python-dotenv
    ```

**System Requirements:**
* A running `redis-server` instance.

## 🌟 Key Features

* **Phase Coherence:** Uses PostgreSQL `NOTIFY` and a lightweight Python daemon (`cache_manager.py`) to increment a shared Redis counter that every worker checks before each HTTP request and each cron job (see Architecture).
* **Granular Invalidation:** Instead of Odoo's process-wide `clear_caches()`, invalidating a model deletes only that model's entries for that database from Redis (keys beginning `<dbname>:distributed_cache:<model>:`). Other workers still clear their whole in-memory cache when they see the signal (see Architecture step 4).
* **Control Panel:** Provides a backend form, the Distributed Cache Manager, to check that this Odoo server can reach Redis (**Check Redis Status**) and to invalidate one model's cache on demand (**Invalidate Model Cache**).
* **Zero-Sudo Security:** "Zero-Sudo" is this project's rule against Odoo's `.sudo()` superuser mode, enforced by the `zero_sudo` module this one depends on. The cache-manager daemon goes further and needs no Odoo login at all: it runs as the unprivileged `odoo` OS user with `NoNewPrivileges=true` (see `daemons/cache-manager.service`) and connects to PostgreSQL as a dedicated role that needs only `CONNECT` (see `daemons/README.md`). [@ANCHOR: COMM_story_zero_sudo_cache_manager]

## 🚀 Getting Started

1. Ensure the Python dependencies (`redis`, `asyncpg`) are installed on your host.
2. Install the `distributed_redis_cache` module from the Odoo Apps menu.
3. Configure Redis settings:
   * Navigate to **Settings > Distributed Redis Cache** (its own section of the main Settings page; the Distributed Cache Manager form has no connection fields). [@ANCHOR: COMM_distributed_cache_settings_view]
   * Enter your Redis host, port, and password. These are stored as system parameters (`ir.config_parameter` keys `distributed_redis_cache.redis_host`, `distributed_redis_cache.redis_port` and `distributed_redis_cache.redis_password`; the password field is masked in the form). Until they are set, Odoo uses the `REDIS_HOST`, `REDIS_PORT` and `REDIS_PASSWORD` environment variables, defaulting to host `redis`, port 6379, no password. Saving the settings makes every worker reconnect with the new values: the saving worker immediately, every other worker at its next request or cron job via the same invalidation signal described under Architecture.
4. Start the background sync daemon:
   * Execute `daemons/cache_manager.py` as a systemd service (a unit file is provided at `daemons/cache-manager.service`; it loads `/opt/hams/etc/redis.env`, so a Redis that requires a login works). Without this daemon the `global_cache_invalidation_counter` key never exists and no worker ever sees another worker's invalidation.
   * The daemon requires `/opt/hams/etc/keys/cache_manager_db.env`, which holds `DB_USER`/`DB_PASS` for its restricted PostgreSQL role; it refuses to start without those credentials. See `daemons/README.md` for how that file is provisioned.
   * It also loads `/opt/hams/etc/keys/cache_manager.env` if that file exists. That file is managed automatically by the Daemon Key Manager module and holds only an Odoo login and API key, which the current daemon does not use.
   * The daemon does **not** read the Redis settings saved in Odoo. It takes `REDIS_HOST`, `REDIS_PORT` and `REDIS_PASSWORD` from its own environment, defaulting to `localhost`, 6379 and no password (note that the Odoo side defaults to host `redis`), and `DB_HOST`, `DB_PORT` and `DB_NAME` likewise, defaulting to `localhost`, 5432 and `odoo`. It listens on exactly one database, `DB_NAME`. Set these for the service when your deployment differs from the defaults.
5. Navigate to **Settings > Technical > Distributed Cache** in Odoo to verify the connection status. The menu is shown to members of the Cache Manager group, which includes the administrator by default. The status check tests only this Odoo server's own Redis connection, not the daemon. [@ANCHOR: COMM_distributed_cache_view]

## Upgrading modules on a server (Redis credentials)

A Redis server that requires a password refuses unauthenticated clients. If Odoo is started by hand for a module upgrade (`odoo -u ...`) without the environment the service normally gets (`REDIS_PASSWORD`), every cache invalidation during the upgrade fails with "Authentication required" while the upgrade itself succeeds, leaving the Redis copies of cached results stale for up to 24 hours. Two safeguards exist:

* `invalidate_model_cache` raises an error, instead of only logging a warning, when Redis rejects the credentials while the registry is still loading (module install or upgrade; [@ANCHOR: distributed_redis_cache:COMM_invalidation_auth_failure_aborts_upgrade]), so an unauthenticated upgrade fails at its first invalidation. A serving worker only logs a warning and falls back to its local cache.
* `scripts/odoo_upgrade.py` runs the upgrade with the service's own environment (it reads the Odoo systemd service's `EnvironmentFile=`, `Environment=` and `User=` through `systemctl show`, never printing the values), refuses to run if the service defines no `REDIS_PASSWORD` or is still running, and exits non-zero if the output shows a Redis authentication failure. After a successful upgrade it also compiles every stylesheet bundle (with `scripts/check_asset_bundles_shell.py`) and exits with status 4 if one fails to compile, because Odoo otherwise serves a stub stylesheet with HTTP 200 and the site appears unstyled; `--no-css-check` skips that step. Exit status 4 means: do not start Odoo.

If an upgrade did run unauthenticated, delete the `<db>:distributed_cache:*` keys from Redis with `SCAN` and `DEL` (never `FLUSHALL`) and restart Odoo. A restart alone does not help: it clears only each worker's in-memory cache, not Redis.

## 🏗️ Architecture (CQRS)

This module implements a strict CQRS (Command Query Responsibility Segregation) pattern to bypass Odoo's single-threaded limitations:
1. When code that changed cached data calls `notify_model_invalidation(env, model_name)` (or an administrator saves the Redis settings, or uses **Invalidate Model Cache**), the worker runs `SELECT pg_notify('distributed_cache_invalidation', <payload>)`, where the payload is the JSON object `{"model": ..., "dbname": ...}`. Nothing is sent automatically when records are written, views change or modules are installed: each module whose cached results depend on some data must call `notify_model_invalidation()` itself when that data changes. PostgreSQL delivers the notification only when the transaction commits. After the same commit, a `notify_model_invalidation()` caller (including the **Invalidate Model Cache** button) also deletes that model's Redis entries and its own in-memory entries for that model (see `invalidate_model_cache` below); the Settings save clears only the saving worker's cached connection settings.
2. The standalone `cache_manager.py` daemon, utilizing `asyncpg`, instantly catches the PostgreSQL notification. It ignores (and logs) any payload that is not a JSON object with both `model` and `dbname`.
3. The daemon publishes the invalidation payload to a Redis Pub/Sub bus AND increments a single global Redis counter key, in the same pipeline.
4. **Corrected 2026-09-09**: no Odoo WSGI worker actually subscribes to that Pub/Sub bus (it exists only so the daemon's own test suite can verify the publish side against a real subscriber) -- there is no background listener thread anywhere in this module. What every worker actually does is poll the global counter with a synchronous Redis `GET` at the start of each HTTP request (`ir.http._authenticate`) and before each cron job (`ir.cron._process_job`, since cron dispatch never passes through `_authenticate`); if it changed since the last request this worker handled, the worker immediately flushes its *entire* local in-memory LRU (least-recently-used, capped at 8192 entries) cache (every model, not only the one that changed), and its cached Redis connection settings, before continuing. The poll is skipped only while the registry is still loading (module install or upgrade), so an unreachable Redis cannot fail an upgrade. While the daemon runs and Redis is reachable, this still guarantees no worker ever serves a value from before the last invalidation -- it is just a request-time poll-and-clear-all, not a push-based, per-model subscription. If the daemon is not running, the counter never changes; if Redis is unreachable, the poll fails with a logged warning. In either case other workers keep serving their in-memory entries, which have no expiry of their own, until a later counter change or a process restart.

## 💻 Developer & AI Usage Guide

This module provides essential tools to integrate customized models into the distributed caching ecosystem safely. When developing new modules, always use the functions detailed below instead of standard Odoo caching (`@tools.ormcache`).

### 1. The `@distributed_cache()` Decorator
Replaces Odoo's `@tools.ormcache` to provide precise, cross-worker distributed caching with multi-tenant awareness (website and company boundaries). [@ANCHOR: COMM_distributed_cache_decorator]

**Usage:**
```python
from odoo.addons.distributed_redis_cache.redis_cache import distributed_cache
from odoo import models

class MyModel(models.Model):
    _name = 'my.model'

    @distributed_cache()
    def my_expensive_computation(self, param1):
        # Result will be cached in Redis and local memory across all workers
        return ...
```

**How it caches:** the cache key combines the database name, model name, method name, `website_id` and the active company IDs (from `allowed_company_ids` in the context, or else the user's companies), and a SHA-256 hash of the recordset IDs and arguments. A call first checks the worker's in-memory cache, then Redis; on a miss it runs the method and stores the result in both, with a 24-hour expiry in Redis. Results must be picklable (serializable with Python's `pickle`); one that is not is still returned and kept in memory, but is not written to Redis. Redis payloads are HMAC-signed (a keyed SHA-256 signature) and any payload whose signature does not verify is discarded, never unpickled. The signing key is derived from `HAMS_CRYPTO_KEY`, else `/var/lib/odoo/hams_crypto.secret`, else Odoo's `admin_passwd` (the literal `admin` is refused). With none of these configured, the decorator skips Redis entirely and caches per process only. If Redis is unreachable, the method is computed and cached in memory only ("fail-open"), so an outage slows the site down but does not break it. Passing `redis_bypass_cache=True` in the context skips caching entirely.

### 2. `notify_model_invalidation(env, model_name)`
Triggers a cross-worker invalidation signal via PostgreSQL `NOTIFY`. Use this when you update records that are aggressively cached and need immediate propagation. Nothing happens until the current transaction commits: then this worker clears the model's Redis and in-memory entries, and the notification reaches the daemon, which signals every other worker as described under Architecture. A `model_name` that is not a model in this database is refused with a logged warning and no signal. [@ANCHOR: COMM_notify_model_invalidation_logic]

**Usage:**
```python
from odoo.addons.distributed_redis_cache.redis_cache import notify_model_invalidation

def invalidate_my_model(self):
    # Sends a PostgreSQL (PG) NOTIFY to invalidate 'my.model' caches globally
    notify_model_invalidation(self.env, 'my.model')
```

### 3. `invalidate_model_cache(env, model_name, local_only=False)`
Flushes the cache specifically for the given model locally, and also within the Redis store unless `local_only=True`. `notify_model_invalidation()` calls it for you after commit; daemon events do not call it (other workers react to them by clearing their whole in-memory cache instead). Calling it directly sends no signal: other workers keep their in-memory copies until some later invalidation changes the counter. Use `notify_model_invalidation()` when other workers must see the change. [@ANCHOR: COMM_manual_cache_invalidation]

**Usage:**
```python
from odoo.addons.distributed_redis_cache.redis_cache import invalidate_model_cache

def force_clear(self):
    # Clears local and Redis cache synchronously for 'my.model'
    invalidate_model_cache(self.env, 'my.model')
```

**Testing Note:** The module disables test bypass for caching to ensure parity with production: tests use the same Redis and counter-polling paths as a serving worker. If you encounter test pollution, pass the `.with_context(redis_bypass_cache=True)` context variable during your environment operations; the `@distributed_cache()` wrapper then calls the method directly, neither reading nor writing any cache. The Redis Cache Interceptor (the `ir.http._authenticate` override that runs the per-request counter poll described under Architecture) is separate from this flag and does not consult it. [@ANCHOR: COMM_redis_cache_interceptor]

### 5. Internals worth knowing

* `poll_and_clear_local_cache(env)` [@ANCHOR: distributed_redis_cache:COMM_poll_and_clear_local_cache] reads Redis's `global_cache_invalidation_counter`; if it changed since the last poll by any caller in this process, it empties the in-memory cache and this database's cached Redis connection settings. `ir.http._authenticate` and `ir.cron._process_job` both call it, so a worker notices another worker's change on its next request or cron run. A Redis error is logged as a warning and nothing is cleared.
* `clear_db_config_cache(dbname)` [@ANCHOR: distributed_redis_cache:clear_db_config_cache] forgets the cached (host, port, password) of one database, so the next `get_redis_connection(env)` reads the current Settings values. It runs when Redis settings are saved (on the saving worker) and from `poll_and_clear_local_cache` (on every other worker).
* `res.config.settings.set_values()` [@ANCHOR: distributed_redis_cache:res_config_settings_set_values] saves the Redis settings, clears the saving worker's cached connection settings, and sends the `distributed_cache_invalidation` `pg_notify` so the daemon bumps the global counter and every other worker clears theirs too; without that, a saved change would reach each worker only when it was restarted.
* The cache-manager daemon's `_require_db_credentials()` [@ANCHOR: COMM_cache_manager_require_db_credentials] raises `RuntimeError` at start-up unless both `DB_USER` and `DB_PASS` are set, so it never connects to PostgreSQL without its own scoped role.

---

<stage1_sweep>
## Anchor-Coverage Notes

Every cache payload written to Redis is HMAC-signed before storage and verified before
deserialization -- `_pickle.loads()` is never called on a payload whose signature doesn't verify,
since a compromised/misconfigured Redis instance is otherwise a remote-code-execution vector.

* **Crypto Secret Resolution:** `[@ANCHOR: distributed_redis_cache:COMM_raw_crypto_secret]` -- mirrors `zero_sudo.security.utils._get_crypto_secret()`'s own fallback chain (`HAMS_CRYPTO_KEY`, then `/var/lib/odoo/hams_crypto.secret`, then Odoo's `admin_passwd`; the literal `admin` is refused) independently, to avoid a circular `@distributed_cache()` dependency.

* **HMAC Key Derivation:** `[@ANCHOR: distributed_redis_cache:COMM_cache_hmac_key]` -- returns `None` (never a guessable default) when no real secret is configured.

* **Payload Signing:** `[@ANCHOR: distributed_redis_cache:COMM_sign_payload]`

* **Payload Verification:** `[@ANCHOR: distributed_redis_cache:COMM_verify_and_unwrap_payload]` -- rejects any payload whose HMAC doesn't match before it ever reaches `_pickle.loads()`.

* **Daemon Key Registration:** `[@ANCHOR: distributed_redis_cache:COMM_post_init_hook]` -- on install, registers the "Redis Cache Manager" daemon with `daemon.key.registry` (see [daemon_key_manager.md](daemon_key_manager.md)), which manages the daemon's `cache_manager.env` file.

* **Redis Connection Resolution:** `[@ANCHOR: distributed_redis_cache:COMM_get_redis_connection]` -- the connection settings come from the Settings system parameters, else the `REDIS_*` environment variables.

* **Postgres NOTIFY Callback:** `[@ANCHOR: distributed_redis_cache:COMM_postgres_notify_handler]` -- schedules the Redis broadcast task on the asyncio event loop when a NOTIFY arrives.
</stage1_sweep>

<stories_and_journeys>
## 6. Architectural Stories & Journeys

### Stories
* [Distributed Cache Decoration](distributed_redis_cache/hams_shared/docs/stories/cache_decoration.md)
* [Cross-Worker Cache Invalidation](distributed_redis_cache/hams_shared/docs/stories/cache_invalidation.md)
* [Manual Cache Management](distributed_redis_cache/hams_shared/docs/stories/manual_management.md)
* [System Resilience](distributed_redis_cache/hams_shared/docs/stories/resilience.md)

### Journeys
* [Daemon Operations](distributed_redis_cache/hams_shared/docs/journeys/daemon_operations.md)
* [Invalidation Pipeline](distributed_redis_cache/hams_shared/docs/journeys/invalidation_pipeline.md)
* [Request Caching Lifecycle](distributed_redis_cache/hams_shared/docs/journeys/request_caching_lifecycle.md)

</stories_and_journeys>
