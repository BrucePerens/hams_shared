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
   * The daemon requires `/opt/hams/etc/keys/cache_manager/cache_manager_db.env`, which holds `DB_USER`/`DB_PASS` for its restricted PostgreSQL role; it refuses to start without those credentials. See `daemons/README.md` for how that file is provisioned.
   * It does not read `/opt/hams/etc/keys/cache_manager.env`, the Odoo login the Daemon Key Manager used to keep for this service: the daemon has no use for an Odoo login, and since version 1.1 the module no longer registers one.
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

### 1. The `@distributed_cache()` Decorator [@ANCHOR: COMM_distributed_cache_decorator]
Replaces Odoo's `@tools.ormcache` to provide precise, cross-worker distributed caching with multi-tenant awareness (website and company boundaries).

**Purpose.** Caches the result of an Odoo model method in two levels: a per-process in-memory cache (L1) and the shared Redis store (L2), keyed so that tenants never see each other's results. It replaces `@tools.ormcache`, which only knows about one worker.

**Call interface.** `distributed_cache()` takes no arguments and returns the decorator, so it is always written with parentheses. The decorated function must be a method of an Odoo model (the wrapper reads `self.env` and `self._name`); it may take any positional and keyword arguments and keeps its own name and docstring. The wrapper does this, in order:

| Step | What happens |
|---|---|
| Bypass | If `redis_bypass_cache` is truthy in the context, the method is called directly and nothing is read or written. |
| Key | `<dbname>:distributed_cache:<model>:<method>:w<website_id>:c<company ids>:<argument hash>`. `website_id` is the context's `website_id` or 0; the company part is the sorted `allowed_company_ids` from the context, else the sorted ids of `env.companies`, else `0`; the hash is `_get_hash` (see `COMM_distributed_cache_key_generation`) and includes `self`. |
| L1 | The process-wide `_local_cache` (LRU, 8192 entries, guarded by `LRU_LOCK`) is checked first; a hit returns the stored object itself, not a copy. |
| L2 | With an HMAC key (`_cache_hmac_key` is not `None`) and a Redis pool, the key is read, the HMAC verified, the payload unpickled and copied into L1. |
| Miss | The method runs; the result is pickled, signed and written with `SETEX` for 86400 seconds (24 hours), and is always stored in L1. |

**Returns and errors.** The method's own return value. An exception raised by the method propagates and nothing is cached. A `redis.RedisError` is logged as a warning and the call carries on without Redis (fail open). A payload that is malformed, fails the HMAC check or will not unpickle is logged and ignored, and Redis is not written for that call. A result that cannot be pickled is returned and kept in L1 only.

**Special concerns.** Synchronous and blocking: a Redis call waits at most the pool's 1 second socket timeout. The only lock taken is `LRU_LOCK`, briefly and never across a Redis call or the method itself. Callers share the cached object, so they must not mutate a returned list or dict, and a method whose result depends on something that is not in the key (the user, the language, the time) must not be decorated. L1 entries have no expiry of their own: they go when `poll_and_clear_local_cache` sees the invalidation counter change, when `invalidate_model_cache` runs for the model, or when the process restarts. Redis entries live 24 hours. Pickle is only read back after the HMAC verified, so a writer without the application secret cannot plant code.

**See also.** Test: `tests/test_distributed_cache.py` (cites this anchor); related `COMM_notify_model_invalidation_logic`, `COMM_invalidate_model_cache_logic`, `distributed_redis_cache:COMM_get_redis_connection`, `distributed_redis_cache:COMM_cache_hmac_key`.

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

* **Daemon Key Registration (removed in 1.1):** `[@ANCHOR: distributed_redis_cache:unregister_cache_manager_key]` -- versions before 1.1 registered the "Redis Cache Manager" daemon with `daemon.key.registry` (see [daemon_key_manager.md](daemon_key_manager.md)) in a `post_init_hook`, which minted an Odoo API key for `cache_manager.env` that the daemon never used. 1.1 has no `post_init_hook`; `migrations/1.1` calls `unregister_cache_manager_key(env)`, which removes the registry row and revokes the key (idempotent; the service account and the leftover key file stay).

### `COMM_raw_crypto_secret` [@ANCHOR: distributed_redis_cache:COMM_raw_crypto_secret]

**Purpose.** Finds the application secret from which the cache's HMAC key is derived. It is called by `_cache_hmac_key` on every sign and verify and is deliberately independent of `zero_sudo.security.utils._get_crypto_secret()`, because that method is itself `@distributed_cache()` decorated and calling it from the cache's own read path would recurse (and `zero_sudo` already depends on this module).

**Call interface.** `_raw_crypto_secret()` takes no arguments and returns a `str`. Order of lookup: the `HAMS_CRYPTO_KEY` environment variable; the stripped contents of `/var/lib/odoo/hams_crypto.secret` (an `OSError` while reading is logged as a warning and the lookup continues); Odoo's `admin_passwd` from `tools.config`. An empty result, or the literal `admin`, is refused and `""` is returned.

**Errors and side effects.** It never raises. When no usable secret exists it logs one error per process (module flag `_warned_missing_crypto_secret`), not one per call. It reads the file on every call (no caching), so changing the secret file takes effect without a restart; it takes no lock.

**Security.** The secret is never logged. A hardcoded fallback is never substituted, since a publicly known key would let anyone forge signed cache entries.

**See also.** Test: `tests/test_fixes.py` (cites this anchor); caller `distributed_redis_cache:COMM_cache_hmac_key`.

### `COMM_cache_hmac_key` [@ANCHOR: distributed_redis_cache:COMM_cache_hmac_key]

**Purpose.** Derives the key that signs every payload written to Redis, so the cache can refuse to unpickle anything it did not write itself. Redis holds pickles, and an unsigned pickle read back from a shared or compromised Redis is a way to run code in every worker.

**Call interface.** `_cache_hmac_key()` takes no arguments. It returns 32 bytes, `SHA-256("<secret>:distributed_redis_cache_hmac")`, where the secret comes from `_raw_crypto_secret`; or `None` when no real secret is configured. It does not raise.

**Special concerns.** `None` is a signal, not a key: the decorator then skips Redis entirely and caches in process memory only, because deriving a key from an empty secret would give every unconfigured deployment the same publicly guessable key. Callers must never sign or verify with `None`. The key is recomputed on each call (a hash of a short string), so a rotated secret changes the key at once and existing Redis entries then fail verification and are recomputed (they are ignored, not unpickled). No locks or I/O beyond the secret lookup.

**See also.** Test: `tests/test_fixes.py`; related `distributed_redis_cache:COMM_raw_crypto_secret`, `distributed_redis_cache:COMM_sign_payload`, `distributed_redis_cache:COMM_verify_and_unwrap_payload`.

### `COMM_sign_payload` [@ANCHOR: distributed_redis_cache:COMM_sign_payload]

**Purpose.** Wraps a pickled result into the string that is stored in Redis, so that a reader can check it came from this application.

**Call interface.** `_sign_payload(key, payload_bytes)`: `key` is the bytes from `_cache_hmac_key` (never `None`); `payload_bytes` is the pickle of the cached value. It returns a `str` of the form `<64 hex characters of HMAC-SHA-256>:<payload as hex>`; the payload is hex encoded so the value is plain ASCII and survives a Redis connection with `decode_responses=True`. It raises nothing for valid input (a `TypeError` for a `None` key or non-bytes payload).

**Special concerns.** Pure function: no I/O, no locks, deterministic. The hex encoding doubles the stored size. The signature covers only the payload bytes, not the Redis key, so a valid payload copied under a different key would verify; the key already carries the database, model, tenant and argument hash, and Redis write access is the trust boundary.

**See also.** Inverse `distributed_redis_cache:COMM_verify_and_unwrap_payload`; test `tests/test_fixes.py`.

### `COMM_verify_and_unwrap_payload` [@ANCHOR: distributed_redis_cache:COMM_verify_and_unwrap_payload]

**Purpose.** Checks the HMAC on a value read from Redis and returns the pickle bytes, so `_pickle.loads()` is only ever called on data this application signed.

**Call interface.** `_verify_and_unwrap_payload(key, stored)`: `key` is the bytes from `_cache_hmac_key`; `stored` is the `str` read from Redis. It returns the payload `bytes`. It raises `ValueError` when the value has no `:` separator or an empty payload ("missing HMAC signature"), when the payload is not valid hex, or when the signature does not match ("tampered or forged"). The comparison uses `hmac.compare_digest` (constant time). A non-ASCII signature part makes `compare_digest` raise `TypeError`, which the decorator does not catch (unknown whether any path can produce one; to-do: decide and test).

**Special concerns.** Pure function, no I/O or locks. The decorator treats `ValueError` as "payload rejected": it logs a warning, skips Redis for that call, and recomputes the value. An entry from before a secret rotation fails here and is recomputed.

**See also.** Inverse `distributed_redis_cache:COMM_sign_payload`; test `tests/test_fixes.py`.

### `COMM_get_redis_connection` [@ANCHOR: distributed_redis_cache:COMM_get_redis_connection]

**Purpose.** Returns the Redis client every part of the module uses, choosing the connection settings from the Settings page (`ir.config_parameter`) when an environment is given and otherwise from the process-wide default pool.

**Call interface.** `get_redis_connection(env=None)` returns a `redis.Redis`. With `env`, the host, port and password come from the system parameters `distributed_redis_cache.redis_host`, `.redis_port` and `.redis_password`, read through `zero_sudo.security.utils` with `redis_bypass_cache=True` (no `.sudo()`); a port that is not an integer falls back to the default. The resolved tuple is cached per database name in `_db_configs` until `clear_db_config_cache`. If the tuple equals the defaults (`REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` environment variables, else `redis`, 6379, none) the shared `redis_pool` is used; otherwise a `ConnectionPool` is created once per (host, port, password) and kept in `_custom_pools`, never freed. Without `env` the default pool is returned.

**Special concerns.** No connection is opened here, so it does not raise for an unreachable server; errors appear on the first command. Both pools use `decode_responses=True` and 1 second connect and socket timeouts, and the ACL user from `REDIS_USERNAME`. `POOL_LOCK` guards both dictionaries and is held while the settings are read the first time. The password is never logged.

**See also.** Test `tests/test_fixes.py`; related `COMM_redis_connection_pool`, `distributed_redis_cache:clear_db_config_cache`.

### `COMM_postgres_notify_handler` [@ANCHOR: distributed_redis_cache:COMM_postgres_notify_handler]

**Purpose.** The callback the cache-manager daemon registers with `asyncpg` for the `distributed_cache_invalidation` channel. It turns each PostgreSQL notification into a Redis publish without blocking the connection that delivered it.

**Call interface.** `postgres_notify_handler(connection, pid, channel, payload)` is a plain (synchronous) function: `connection` is the asyncpg connection, `pid` the notifying backend's pid, `channel` the channel name, `payload` the string sent by `pg_notify` (a JSON object with `model` and `dbname`). It returns `None`. It logs the notification at info level and calls `asyncio.create_task(broadcast_to_redis(payload))`, so it must run inside the event loop (asyncpg calls listeners there). The task is kept in the module set `_background_tasks` until it finishes, so it is not garbage collected mid-run.

**Special concerns.** It validates nothing itself: `broadcast_to_redis` (`COMM_cache_manager_redis_publish`) drops payloads that are not a JSON object with `model` and `dbname`. Each notification becomes its own task; a stalled Redis surfaces as a per-call timeout (10 seconds) rather than a growing backlog. Fully asynchronous side effects: Redis publish and counter increment.

**See also.** Test `tests/test_fixes.py`; `distributed_redis_cache:cache_manager_main` for the loop that installs it.

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
