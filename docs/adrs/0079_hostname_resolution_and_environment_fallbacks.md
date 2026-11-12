# ADR 0079: Hostname Resolution and Environment Fallbacks

## Status
Accepted

## Context
In a multi-environment architecture, we support deployment via both Docker Compose and Bare-Metal platforms. Hardcoding loopback IP addresses such as `127.0.0.1` inherently causes routing failures when services are spread across Docker containers (which possess distinct network namespaces). Conversely, relying solely on Docker-specific static hostnames (e.g., `postgres` or `redis`) breaks the application on bare-metal systems unless `/etc/hosts` is precisely manually synchronized.

To facilitate seamless execution for Open Source community members downloading `hams_open` modules, we require a standardized approach to host resolution that naturally supports both orchestrated and un-orchestrated environments without requiring manual system alterations.

## Decision
We mandate the **Environment Fallback Pattern** for all hostname configuration.

1. **No Loopback IPs:** Hardcoding `127.0.0.1` is strictly forbidden across the codebase.
2. **Strict Two-Argument Fallbacks:** The application MUST retrieve hostnames dynamically using `os.environ.get()` or `os.getenv()`, and it MUST ALWAYS utilize the two-argument variant with a strict fallback to `"localhost"`.

**Example:**
```python
host = os.environ.get("POSTGRES_HOST", "localhost")
```

### Standardized Environment Variables
When orchestrating via Docker Compose, the system will set specific environment variables pointing to the service containers. On bare-metal deployments, administrators may optionally define these variables and synchronize `/etc/hosts`, or leave them undefined to default cleanly to `"localhost"`.

| Service | Environment Variable | Docker Hostname | Default Fallback |
| :--- | :--- | :--- | :--- |
| PostgreSQL | `POSTGRES_HOST` | `postgres` | `"localhost"` |
| Redis | `REDIS_HOST` | `redis` | `"localhost"` |
| RabbitMQ | `RABBITMQ_HOST` | `rabbitmq` | `"localhost"` |
| Odoo | `ODOO_HOST` | `odoo` | `"localhost"` |
| PowerDNS | `POWERDNS_HOST` | `powerdns` | `"localhost"` |

## Consequences
* **Community Portability:** Developers can run the application directly on their local workstation without manually editing `/etc/hosts` or installing Docker.
* **CI/CD Enforcement:** The AST (abstract syntax tree) linter `check_burn_list.py`, which parses Python source and rejects code by its structure, physically prevents developers from merging host lookups that omit the `"localhost"` fallback, eliminating "missing host" regressions in production.
* **Orchestration Flexibility:** Allows infrastructure teams to dynamically decouple microservices onto separate physical servers simply by updating the `.env` vault.

## Status check 2026-11-12

Checked against `tools/check_burn_list.py` and the code. The ban on hardcoded `127.0.0.1` stands, but the fallback rule as written is not what is enforced or practised:
- The linter rule "CRITICAL NETWORK HARDCODING" flags the text `127.0.0.1` and the text `localhost` in any Python file outside `tools/` directories, including
  inside `os.environ.get("X", "localhost")`. A new `"localhost"` fallback therefore fails the linter (an existing one carries a `# burn-ignore-env:` reason, e.g.
  `distributed_redis_cache/daemons/cache_manager.py`). The linter message tells authors to use the Docker service name.
- The code does that: the fallback is the service name (`os.environ.get("REDIS_HOST", "redis")` in `daemons/gdpr_csv_export/main.py`,
  `REDIS_HOST_DEFAULT = os.getenv("REDIS_HOST") or "redis"` in `distributed_redis_cache/redis_pool.py`), and a bare-metal host maps that name to its own address in `/etc/hosts`.
  Read decision 2 as "two-argument form with a service-name fallback", not `"localhost"`.
- Of the five variables in the table only `REDIS_HOST`, `RABBITMQ_HOST` and `ODOO_HOST` are read by Python code today; `POSTGRES_HOST` and `POWERDNS_HOST`
  appear in no Python file (daemons use `DB_HOST` from their own environment files).
- The linter's wording is not an AST check on a missing fallback, as the Consequences section says; it is a regular-expression check on the literal text.
