# MASTER 07: Zero-DB Architecture

## Status
Accepted (Consolidates ADRs 0003, 0032)

## Context & Philosophy
During major amateur radio contests, the global DX (long-distance contact) Cluster network generates an extreme velocity of signal reports. Writing every incoming spot to a relational PostgreSQL table causes massive disk I/O churn and index fragmentation, degrading the entire ERP.

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

## Status check 2026-11-12

Checked against `ham_dx_cluster`. `ham.dx.spot` is an `AbstractModel` that pushes spots to a Redis sorted set and `bus.bus` and writes no spot row; the cache keeps 4 hours and a cron prunes it every 15 minutes (`_cron_prune_redis`).
One table now exists beside it: `ham.dx.spot.daily` (`ham_dx_spot_daily.py`), a counts-only history per DXCC (DX Century Club award) entity (a country or territory on the DX Century Club list, the ARRL (American Radio Relay League) award list used for DX contacts) and UTC day that a cron drains from Redis. It holds no callsign, spotter or remark, so the mandate that individual spots are never written still holds.
