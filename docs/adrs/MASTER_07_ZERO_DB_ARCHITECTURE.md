# MASTER 07: Zero-DB Architecture

## Status
Accepted (Consolidates ADRs 0003, 0032)

## Context & Philosophy
During major amateur radio contests, the global DX Cluster network generates an extreme velocity of signal reports. Writing every incoming spot to a relational PostgreSQL table causes massive disk I/O churn and index fragmentation, degrading the entire ERP.

## Decisions & Mandates

### ### 1. Ephemeral Memory Routing
### The DX Cluster ingestion engine (`ham.dx.spot`) MUST be implemented as an Odoo `AbstractModel`. It acts purely as a memory router.
### * It intercepts incoming JSON2-RPC payloads.
### * It validates the payload and pushes it directly to a Redis Sorted Set (for short-term historical caching) and the Odoo WebSocket Bus (`bus.bus`) for real-time UI updates..
* It explicitly DOES NOT execute PostgreSQL `INSERT` statements.

### 2. UI Exemption
Because `AbstractModels` do not possess backing database tables, standard Odoo List, Kanban, and Search views cannot query this data.
* Ephemeral data models are explicitly exempt from requiring standard Odoo backend UI facilities.
* Historical searching is handled exclusively by the 4-hour Redis cache and the specialized OWL frontend component.
