# Edge Routing Module

## Technical Specification

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

### 1. Custom Domain Mapping
Maps an externally-owned FQDN (e.g. a club's own `www.myclub.org`) onto an internal `website_slug`, so a custom domain can front any record implementing `edge.routing.mixin`. The domain table is a registry, not a request router: per the module's own README (`edge_routing/README.md` in `hams_open`), nothing in `hams_open` reads `target_slug` when serving a request, and custom-domain traffic is served through Odoo core's `website.domain`.
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

## External Dependencies

* `distributed_redis_cache` for the RAM-cached domain/slug resolution lookups.
* `zero_sudo` for the service-account env used by cross-model slug lookups and cache-invalidation notifications.

## Cross-Module Interfaces

### PagerDuty Domain Sync
`edge.routing.domain.push_all_to_pager_duty()` pushes the custom-domain list to PagerDuty asynchronously via `ir.cron`, triggered by `_invalidate_cache()` on any domain create/write/unlink. Per the module's README, "PagerDuty" here is hams_open's own `pager_duty` monitoring module ([pager_duty.md](pager_duty.md)), not the commercial PagerDuty service, and what is pushed is the deduplicated list of domain names only (every `edge.routing.domain` name, plus every `ham.dns.zone` name when `ham_dns` is installed), with no slugs or routes; `pager_duty` uses the list as the target of its Let's Encrypt "certbot" readiness check.
