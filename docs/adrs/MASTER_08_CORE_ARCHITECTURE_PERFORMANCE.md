# MASTER 08: Core Architecture & Performance

## Status
Accepted (Consolidates ADRs 0001, 0022, 0023, 0024, 0027, 0031, 0047, 0048, 0057, 0066, 0071)

## Context & Philosophy
The platform handles massive real-time data ingestion, external polling, and WebSocket concurrency.
Standard Odoo WSGI workers are optimized for low-concurrency ERP transactions.
To prevent CPU/RAM exhaustion, all heavy lifting MUST be distributed asynchronously and managed with strict O(1) memory profiling.

## Decisions & Mandates

### 1. Hybrid Monolith-Daemon Architecture
* High-CPU, high-I/O, and concurrent WebSocket tasks MUST be offloaded to standalone Python daemons.
* Odoo acts strictly as the database authority, ORM layer, and primary UI.

### 2. Scalability & Coherent Distributed Caching
* The Odoo web tier MUST remain entirely stateless to allow horizontal scaling behind a load balancer. Session data must reside in Redis.
* High-frequency lookups MUST use `@tools.ormcache`.
* **Secure Cached Resolver Pattern:** All cached lookup methods MUST accept an `override_svc_uid=None` parameter so external caller modules can execute the underlying database cache-miss queries securely under their own Micro-Service Account context.
* To keep distributed ORM caches in sync across multiple nodes, the ORM MUST emit a PostgreSQL `NOTIFY` upon data mutation. An external daemon listens to this and broadcasts a cache invalidation via Redis to all workers.

### 3. Asynchronous WSGI Offloading & Connection Pooling
* Long-running HTTP requests MUST spawn a bounded `ThreadPoolExecutor` background task, maintaining their own independent PostgreSQL cursor. Unbounded `threading.Thread` usage is a strict DoS vector and is forbidden.
* Direct integrations with Redis or RabbitMQ inside controllers MUST use module-level connection pooling to prevent TCP handshake exhaustion.

### 4. Bounded Chunking & O(1) Memory Mapping
* Unbounded array loops (like GDPR erasures) MUST chunk operations using `limit` and loop iteratively, explicitly committing `env.cr.commit()` inside the loop to release database locks.
* JSON data exports MUST stream via Python generators (`yield`) to maintain a flat memory footprint.
* Nested ORM queries are forbidden. Related data MUST be pre-fetched into memory-mapped dictionaries outside of the loop.

### 5. High-Performance Indexing & Database Locks
* Concurrency locking using `pg_advisory_xact_lock` MUST generate the lock ID using `env['zero_sudo.security.utils']._get_deterministic_hash()`. Standard `hash()` is salted per-process and will fail across horizontally scaled workers.
* Any model field searched via `ILIKE` or partial strings MUST use `index='trigram'` to leverage PostgreSQL's `pg_trgm` extension.

### 6. Postgres Notify Payload Truncation
* `NOTIFY` payloads are strictly limited to 8000 bytes. Event triggers MUST strictly transmit arrays of integer database IDs, leaving the async consumer to query the full data.

### 7. Asynchronous Bastion Pattern for External I/O
To prevent hanging WSGI workers and "split-brain" states, all heavy OS-level or external network tasks MUST follow this four-step pattern:
1. **State Initialization:** Odoo creates a tracking record and sets its status to `pending`.
2. **Transactional Dispatch:** Odoo uses `env.cr.postcommit.add()` to push the task payload to RabbitMQ.
3. **Isolated Execution:** The standalone Python daemon consumes the message and executes the dangerous operation.
4. **JSON-2 RPC Callback:** Upon completion, the daemon executes a JSON-2 RPC call back to Odoo to update the tracking record's state to `done` or `failed`.
