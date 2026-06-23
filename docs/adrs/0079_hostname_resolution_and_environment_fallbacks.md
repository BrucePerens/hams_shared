# ADR 0079: Hostname Resolution and Environment Fallbacks

## Status
Accepted

## Context
In a multi-environment architecture, we support deployment via both Docker Compose and Bare-Metal platforms. Hardcoding loopback IP addresses such as `127.0.0.1` inherently causes routing failures when services are spread across Docker containers (which possess distinct network namespaces). Conversely, relying solely on Docker-specific static hostnames (e.g., `postgres` or `redis`) breaks the application on bare-metal systems unless `/etc/hosts` is precisely manually synchronized.

To facilitate seamless execution for Open Source community members downloading `hams_community` modules, we require a standardized approach to host resolution that naturally supports both orchestrated and un-orchestrated environments without requiring manual system alterations.

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
* **CI/CD Enforcement:** The AST linter `check_burn_list.py` physically prevents developers from merging host lookups that omit the `"localhost"` fallback, eliminating "missing host" regressions in production.
* **Orchestration Flexibility:** Allows infrastructure teams to dynamically decouple microservices onto separate physical servers simply by updating the `.env` vault.
