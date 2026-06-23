# MASTER 06: DNS CQRS Architecture

## Status
Accepted (Consolidates ADRs 0012, 0034)

## Context & Philosophy
Pointing a public-facing authoritative DNS server (PowerDNS) directly at Odoo's primary PostgreSQL database makes the ERP highly vulnerable to standard DNS floods and DDoS attacks.

## Decisions & Mandates

### 1. Command Query Responsibility Segregation (CQRS)
We physically isolate the DNS read infrastructure from the application state.
* **Command (Odoo):** Odoo manages the `ham.dns.zone` state. Any CRUD operation triggers an asynchronous message to RabbitMQ.
* **Sync Daemon:** An external Python daemon (`pdns_sync`) consumes the RabbitMQ events and pushes the translated state to PowerDNS via its REST API.
* **Query (PowerDNS):** PowerDNS operates entirely on its own high-speed, isolated SQLite backend (`gsqlite3`). DNS floods hit SQLite, leaving Odoo completely unaffected.

### 2. Reconciliation Loop
Because RabbitMQ is a fire-and-forget mechanism, there is a risk of state drift if messages are dropped. 
* A nightly Odoo cron job executes a full sweep of all active `ham.dns.zone` records.
* It triggers bulk updates into the RabbitMQ queue.
* Because the PowerDNS REST API uses `PATCH` (REPLACE), these operations are fully idempotent, silently overwriting the nameserver with the absolute truth from Odoo to enforce Eventual Consistency.
