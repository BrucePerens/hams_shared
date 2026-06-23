# ADR 0073: Fail-Fast Dependency Resolution

## Status
Accepted

## Context
Odoo's native architecture frequently swallows `ImportError` exceptions for missing Python libraries until the exact moment a specific view, controller, or method utilizing that library is triggered.

In a high-availability environment relying on background daemons and message brokers (e.g., RabbitMQ via `pika`, caching via `redis`), this behavior creates severe operational blind spots. A module might install and appear healthy during deployment, only to catastrophically fail in production hours later when an asynchronous task attempts to execute a missing import. This violates Site Reliability Engineering (SRE) best practices.

## Decision
We mandate the **Fail-Fast Dependency Resolution** pattern for all Odoo modules in this repository.

All external Python libraries utilized anywhere within a module MUST be explicitly declared in the `external_dependencies` dictionary of the module's `__manifest__.py` file.

```python
    "external_dependencies": {
        "python": ["pika", "redis", "cryptography", "psutil"],
    },
```

## Consequences
* **Deterministic Deployments:** By declaring dependencies in the manifest, the Odoo core engine will physically refuse to start the server or load the module if the dependency is missing on the host OS or within the virtual environment.
* **Shift-Left Error Handling:** Errors are shifted from unpredictable, silent runtime exceptions to immediate, highly visible deployment-time halts.
