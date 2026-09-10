# Distributed Redis Cache (`distributed_redis_cache`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

Fine-grained distributed caching and phase coherence for horizontally scaled Odoo clusters.

## Features
- **Distributed Redis-backed cache**: Replaces or augments Odoo's local cache for cluster-wide consistency.
- **Multi-Website Awareness**: Isolated cache keys per website (`website_id`) to ensure strict data separation.
- **Cache Drift Prevention**: Ensures all Odoo nodes stay synchronized in real-time.
- **Fail-Open Design**: Automatically falls back to local memory if Redis is unavailable, ensuring high availability.
- **Fine-grained Invalidation**: Precisely flushes specific models instead of the entire cache, minimizing performance impact.
- **Batch Processing**: Uses Redis `SCAN` in batches for efficient cleanup of millions of keys without blocking the server.
- **Management UI**: Dedicated interface for health checks and manual cache invalidation.
- **Zero-Sudo Architecture**: All background operations execute with minimal privileges using dedicated service accounts.

## Installation
This module requires a Redis server.
Ensure the `redis` and `asyncpg` Python packages are installed.

## Configuration
Configure the Redis connection via environment variables:
- `REDIS_HOST`: Defaults to `redis` or `localhost`.
- `REDIS_PORT`: Defaults to `6379`.
- `REDIS_PASSWORD`: Optional Redis password.

## Architecture
- **Postgres NOTIFY**: Triggered on model mutation to signal invalidation.
- **Cache Manager Daemon**: A standalone service bridging Postgres NOTIFY to Redis Pub/Sub. Includes robust reconnection and payload validation logic.
- **Redis Pub/Sub**: Distributes invalidation signals across all Odoo workers.
- **Middleware Interceptor**: Odoo workers check signals in `ir.http` and flush local caches before processing requests.

## Security
Built with the **Zero-Sudo** architecture. Operations are performed by dedicated service accounts with minimal privileges. The `cache_manager_sys` user handles daemon-to-database communication.

## Documentation
Comprehensive user documentation is available via the **Knowledge** module after installation.

---

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for LLMs and Integrators. Use this to build dependent modules without needing the source code.
</system_role>

<architecture>
## 1. Architecture & Overview
Standard Odoo `@tools.ormcache` relies on a local worker registry cache, which can drift out of sync in multi-node environments. This module provides a fine-grained, distributed Redis-backed cache enforcing strict phase coherence.

**The Invalidation Pipeline:**
1. An Odoo worker mutates a cached model and fires a PostgreSQL `NOTIFY` on the `distributed_cache_invalidation` channel.
2. The standalone `cache_manager.py` daemon receives the `NOTIFY`, validates the payload, publishes it to the Redis `odoo_cache_invalidation_bus` channel, and `INCR`s a single global counter key in the same pipeline. [@ANCHOR: cache_manager_redis_publish]

3. **Corrected 2026-09-09**: no Odoo worker actually subscribes to that Pub/Sub channel or runs a background listener thread -- confirmed by reading `ir.http._authenticate` and grepping the whole codebase for any production `pubsub()`/`subscribe()` call (there is none outside the daemon's own test suite). Each worker instead polls the global counter with a plain `GET` at the start of every HTTP request; on a change, it clears its ENTIRE local LRU cache (every model, not a per-model queue) before the request proceeds. [@ANCHOR: redis_cache_interceptor]
</architecture>

<resilience>
## 2. Resilience (Fail-Open)
If Redis is unreachable, the system gracefully falls back to a standard Python dictionary (`_local_cache`) limited to 8192 entries. It continues functioning without crashing, though cross-node coherence is temporarily lost. Background listeners handle connection drops gracefully.
</resilience>

<api>
## 3. Application Programming Interface (API)

```python
from odoo.addons.distributed_redis_cache.redis_cache import distributed_cache, invalidate_model_cache, notify_model_invalidation
```

* **`@distributed_cache()`**: Decorator for `api.model` functions. Generates SHA256 cache keys based on serialized arguments and writes to Redis with a 24h TTL. Handles `bytes`, `sets`, `frozensets`, and recordsets deterministically. **Website Aware**: Isolated keys if `website_id` is in context. [@ANCHOR: distributed_cache_decorator]

* **`invalidate_model_cache(env, model_name, local_only=False)`**: Forcibly flushes model cache. Uses batched `SCAN` for production safety. [@ANCHOR: invalidate_model_cache_logic]

* **`notify_model_invalidation(env, model_name)`**: Triggers cluster-wide invalidation signal via Postgres NOTIFY. [@ANCHOR: notify_model_invalidation_logic]
</api>

<ui>
## 4. UI: Distributed Cache View [@ANCHOR: distributed_cache_view]

Provides a management form to check Redis status and manually invalidate model caches. [@ANCHOR: manual_cache_invalidation] [@ANCHOR: check_redis_status_logic]
</ui>

<config>
## 5. Configuration [@ANCHOR: cache_manager_config]
Configurable via environment variables or `.env` file at `/opt/hams/etc/keys/cache_manager.env`.
</config>

<stage1_sweep>
## 7. Stage 1 Anchor-Coverage Sweep Additions

Every cache payload written to Redis is HMAC-signed before storage and verified before
deserialization -- `_pickle.loads()` is never called on a payload whose signature doesn't verify,
since a compromised/misconfigured Redis instance is otherwise a remote-code-execution vector.

* **Crypto Secret Resolution:** `[@ANCHOR: distributed_redis_cache:COMM_raw_crypto_secret]` -- mirrors `zero_sudo.security.utils._get_crypto_secret()`'s own env-var/file/`admin_passwd` fallback chain independently, to avoid a circular `@distributed_cache()` dependency.

* **HMAC Key Derivation:** `[@ANCHOR: distributed_redis_cache:COMM_cache_hmac_key]` -- returns `None` (never a guessable default) when no real secret is configured.

* **Payload Signing:** `[@ANCHOR: distributed_redis_cache:COMM_sign_payload]`

* **Payload Verification:** `[@ANCHOR: distributed_redis_cache:COMM_verify_and_unwrap_payload]` -- rejects any payload whose HMAC doesn't match before it ever reaches `_pickle.loads()`.

* **Daemon Key Registration:** `[@ANCHOR: distributed_redis_cache:COMM_post_init_hook]`

* **Redis Connection Resolution:** `[@ANCHOR: distributed_redis_cache:COMM_get_redis_connection]`

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

### Installation
* **Documentation Injection:** Provisions documentation into `knowledge.article` upon installation. [@ANCHOR: doc_inject_distributed_redis_cache]

### Zero-Sudo
* **Micro-Privilege Service Account:** Uses `cache_manager_sys` for daemon operations. [@ANCHOR: story_zero_sudo_cache_manager]
</stories_and_journeys>
