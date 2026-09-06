# Edge Routing Module

## Technical Specification

### 1. Custom Domain Mapping
Maps an externally-owned FQDN (e.g. a club's own `www.myclub.org`) onto an internal `website_slug`, so a custom domain can front any record implementing `edge.routing.mixin`.
* **FQDN and Reserved-Slug Validation:** `[@ANCHOR: edge_routing:COMM_domain_check_name]`

* **Domain Create/Write/Unlink Cache-Invalidation Cycle:** `[@ANCHOR: edge_routing:COMM_domain_crud_cycle]`

* **Domain Create:** `[@ANCHOR: edge_routing:COMM_domain_create]`

* **Domain Write:** `[@ANCHOR: edge_routing:COMM_domain_write]`

* **Domain Unlink:** `[@ANCHOR: edge_routing:COMM_domain_unlink]`

### 2. Vanity URL Slug Mixin
`edge.routing.mixin` gives any model a globally-unique, auto-generated `website_slug`, shared across every model that implements it (no two models can claim the same slug).
* **Reserved Slug Rejection:** `[@ANCHOR: edge_routing:COMM_check_reserved_slugs]`

* **Cross-Model Registry Discovery:** `[@ANCHOR: edge_routing:COMM_get_routing_models]`

* **Slug Collision Check:** `[@ANCHOR: edge_routing:COMM_check_slug_collision]`

* **Mixin Create (Auto Slug Assignment):** `[@ANCHOR: edge_routing:COMM_mixin_create]`

* **Mixin Unlink (Slug Cache Release):** `[@ANCHOR: edge_routing:COMM_mixin_unlink]`

### 3. Domain-to-Record Resolution
The actual routing entry point: resolves a custom domain all the way through to the record it fronts.
* **Domain → Slug → Record Composition:** `[@ANCHOR: edge_routing:COMM_get_record_by_domain]`

## External Dependencies

* `distributed_redis_cache` for the RAM-cached domain/slug resolution lookups.
* `zero_sudo` for the service-account env used by cross-model slug lookups and cache-invalidation notifications.

## Cross-Module Interfaces

### PagerDuty Domain Sync
`edge.routing.domain.push_all_to_pager_duty()` pushes the full custom-domain routing table to PagerDuty asynchronously via `ir.cron`, triggered by `_invalidate_cache()` on any domain create/write/unlink.
