# Edge Routing Module

## Technical Specification

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

### 1. Custom Domain Mapping
Maps an externally-owned FQDN (e.g. a club's own `www.myclub.org`) onto an internal `website_slug`, so a custom domain can front any record implementing `edge.routing.mixin`. The domain table is a registry, not a request router: per the module's own README (`edge_routing/README.md` in `hams_open`), nothing in `hams_open` reads `target_slug` when serving a request, and custom-domain traffic is served through Odoo core's `website.domain`.
* **FQDN and Reserved-Slug Validation:** `edge_routing:COMM_domain_check_name`

* **Domain Create/Write/Unlink Sync Trigger (`_trigger_pager_duty_sync()`; the anchor's name predates the removal of the domain cache, so it no longer invalidates any cache):** `edge_routing:COMM_domain_crud_cycle`

* **Domain Create (lowercases and strips the name):** `edge_routing:COMM_domain_create`

* **Domain Write (same normalization when the name changes):** `edge_routing:COMM_domain_write`

* **Domain Unlink:** `edge_routing:COMM_domain_unlink`

### 2. Vanity URL Slug Mixin
`edge.routing.mixin` gives any model a `website_slug` (lowercase letters, digits and hyphens, enforced by a database CHECK constraint), unique in the database by an EXCLUDE constraint within one model and, across models, by `_generate_unique_slug()`, which looks at the slugs of every model that implements the mixin. A record created with only a `name` gets a slug generated from it; an explicitly requested slug that is merely taken gets a numeric suffix, while a requested reserved slug is rejected.
* **Reserved Slug Rejection:** `edge_routing:COMM_check_reserved_slugs`

* **Cross-Model Registry Discovery:** `edge_routing:COMM_get_routing_models`

* **Slug Collision Check:** `edge_routing:COMM_check_slug_collision`

* **Unique Slug Generation (suffixes on collision, locks the chosen slug):** `edge_routing:COMM_mixin_generate_unique_slug`

* **Slug Lookup (cached with `@distributed_cache()`):** `edge_routing:COMM_mixin_get_record_by_slug`

* **Mixin Write (re-checks the slug and invalidates the cached old and new slugs):** `edge_routing:COMM_mixin_write`

* **Mixin Create (Auto Slug Assignment):** `edge_routing:COMM_mixin_create`

* **Mixin Unlink (Slug Cache Release):** `edge_routing:COMM_mixin_unlink`

## External Dependencies

* `distributed_redis_cache` for the cached slug lookup `get_record_by_slug()` (the earlier cached domain lookup has been removed).
* `zero_sudo` for the service-account env used by cross-model slug lookups and cache-invalidation notifications.

## Cross-Module Interfaces

### PagerDuty Domain Sync
`edge.routing.domain.push_all_to_pager_duty()` pushes the custom-domain list to PagerDuty asynchronously via `ir.cron`, triggered by `_trigger_pager_duty_sync()` on any domain create/write/unlink (and also by the `ir_cron_push_pager_duty` scheduled action's own daily timer). Per the module's README, "PagerDuty" here is hams_open's own `pager_duty` monitoring module ([pager_duty.md](pager_duty.md)), not the commercial PagerDuty service, and what is pushed is the deduplicated list of domain names only (every `edge.routing.domain` name, plus every `ham.dns.zone` name when `ham_dns` is installed), with no slugs or routes; `pager_duty` uses the list as the target of its Let's Encrypt "certbot" readiness check.

## Anchor reference

### `edge_routing:COMM_domain_check_name` [@ANCHOR: edge_routing:COMM_domain_check_name]

**Purpose.** The validation constraint on the custom-domain registry: a mapped name must look like a host name and must not point at a reserved site slug.

**Where.** `_check_name` on `edge.routing.domain` (`edge_routing/models/domain.py`), an `@api.constrains("name", "target_slug")` method that runs at create and write.

**Rules.** The `name` must be non-empty and contain a dot, else `ValidationError("Domain must be a valid FQDN (e.g. www.myclub.org)")`. A `target_slug` that, lower-cased, is in `RESERVED_SLUGS` (the fixed reserved list in `edge_routing/utils.py`) raises "This target slug is reserved and cannot be used." The model also has a database `UNIQUE(name)` constraint.

**Special concerns.** It is deliberately light: it does not resolve DNS or fully validate labels. Names are lowered and stripped by `create`/`write` before the check.

**See also.** Test `edge_routing/tests/test_custom_domains.py`; `edge_routing:COMM_domain_create`.

### `edge_routing:COMM_domain_crud_cycle` [@ANCHOR: edge_routing:COMM_domain_crud_cycle]

**Purpose.** After any change to the domain table, schedule the push of the full domain list to the PagerDuty module (hams_open's own `pager_duty` monitoring module).

**Signature.** `_trigger_pager_duty_sync(self)`, called by `create`, `write` and `unlink` of `edge.routing.domain`; no arguments, returns nothing. The anchor's name is historical: an older version also invalidated a domain cache, which no longer exists.

**Behaviour.** It looks up the cron `edge_routing.ir_cron_push_pager_duty` (without raising if absent) and calls `cron._trigger()`, which queues an asynchronous run of `push_all_to_pager_duty()`, so the web request does not wait on an HTTP call.

**Errors.** Best effort: any exception is logged as a warning and swallowed, so a failed trigger never blocks the create, write or delete.

**See also.** Test `test_custom_domains.py`; `edge_routing:COMM_domain_create`, `edge_routing:COMM_domain_write`, `edge_routing:COMM_domain_unlink`.

### `edge_routing:COMM_domain_create` [@ANCHOR: edge_routing:COMM_domain_create]

**Purpose.** Creates custom-domain rows with a normalised name and then schedules the PagerDuty sync.

**Signature.** `@api.model_create_multi create(self, vals_list)` on `edge.routing.domain`; `vals_list` is a list of value dicts with `name` (FQDN) and `target_slug`. Returns the new recordset.

**Behaviour.** For each dict with a `name`, the name is lower-cased and stripped before `super().create`, so `WWW.MyClub.org ` and `www.myclub.org` are the same domain under the `UNIQUE(name)` constraint. After creation it calls `_trigger_pager_duty_sync()` once.

**Errors.** `ValidationError` from `_check_name` (not a FQDN, or reserved slug); a unique-constraint error for a duplicate name.

**See also.** `edge_routing:COMM_domain_check_name`, `edge_routing:COMM_domain_crud_cycle`; test `test_custom_domains.py`.

### `edge_routing:COMM_domain_write` [@ANCHOR: edge_routing:COMM_domain_write]

**Purpose.** Updates custom-domain rows, keeping names normalised and the PagerDuty list current.

**Signature.** `write(self, vals)` on `edge.routing.domain`; `vals` is a dict of fields. Returns `True` as Odoo's `write` does.

**Behaviour.** If `vals` contains a non-empty `name`, it is lower-cased and stripped, as in `create`. After `super().write` it always calls `_trigger_pager_duty_sync()`, even if the change was to `target_slug` only.

**Errors.** `ValidationError` from the `_check_name` constraint and the unique-name error, both raised by the ORM before the trigger runs.

**Special concerns.** The trigger queues an asynchronous cron and never fails the write.

**See also.** `edge_routing:COMM_domain_create`, `edge_routing:COMM_domain_crud_cycle`; test `test_custom_domains.py`.

### `edge_routing:COMM_domain_unlink` [@ANCHOR: edge_routing:COMM_domain_unlink]

**Purpose.** Deletes custom-domain rows and re-pushes the shortened list to the PagerDuty module so the removed domain stops being monitored or certificate-managed.

**Signature.** `unlink(self)` on `edge.routing.domain`; returns `True`.

**Behaviour.** Calls `super().unlink()` first and then `_trigger_pager_duty_sync()`. The cron later reads the table afresh (in batches of 1000) and posts the full, de-duplicated list, so the deleted name is absent from it.

**Errors.** Any ORM error from `unlink` propagates; a failure of the trigger is logged and ignored.

**Special concerns.** Deleting a row does not touch DNS or any Odoo `website.domain` record, because this table is a registry only.

**See also.** `edge_routing:COMM_domain_crud_cycle`; test `test_custom_domains.py`.

### `edge_routing:COMM_check_reserved_slugs` [@ANCHOR: edge_routing:COMM_check_reserved_slugs]

**Purpose.** A constraint that stops a record from taking a vanity slug that a real site route, a language prefix or the platform itself uses (for example `admin`, `web`, `en_US`).

**Where.** `_check_reserved_slugs`, an `@api.constrains("website_slug")` method of `edge.routing.mixin` (`edge_routing/models/routing_mixin.py`).

**Behaviour.** For each record with a slug, if the slug is in `reserved_slug_words(env)` it raises `ValidationError("The slug '%s' is reserved and cannot be used.")`. That set is the fixed `RESERVED_SLUGS` list, the first path segment of every registered route, and Odoo's language slugs, so it grows as modules add routes.

**Special concerns.** `create` and `write` also reject a requested reserved slug before slugging, so this constraint is a second check that also covers any write path that reaches the ORM without going through them.

**See also.** `edge_routing:reserved_slug_words`; test `edge_routing/tests/test_routing_mixin.py`.

### `edge_routing:COMM_get_routing_models` [@ANCHOR: edge_routing:COMM_get_routing_models]

**Purpose.** Lists every model that shares the global vanity-URL namespace, so a slug is unique across member sites, group sites and any other model using the mixin.

**Signature.** `@api.model _get_routing_models(self)`; no arguments. Returns a list of model-name strings.

**Behaviour.** Iterates `env.registry.items()` and returns each model whose class is a subclass of `edge.routing.mixin`, excluding the abstract mixin itself. The list is computed per call from the loaded registry, so a module that adds the mixin to a model is included once installed.

**Special concerns.** Pure read of the registry; no database access and no locking. Used by `_generate_unique_slug` to search each model's existing slugs.

**See also.** `edge_routing:COMM_mixin_generate_unique_slug`; test `test_routing_mixin.py`.

### `edge_routing:COMM_check_slug_collision` [@ANCHOR: edge_routing:COMM_check_slug_collision]

**Purpose.** The single yes/no question "is this slug already taken?", split out so a test can force a permanent collision (slug exhaustion) by replacing it.

**Signature.** `_check_slug_collision(self, slug, existing_slugs)`; `slug` is a candidate string; `existing_slugs` is a set of strings (reserved words, forbidden slugs and slugs found in every routing model). Returns `slug in existing_slugs` as a `bool`.

**Behaviour.** No side effects, no database access, constant time.

**Special concerns.** It is called inside the loop of `_generate_unique_slug`, up to 1000 attempts before that method raises `ValidationError`. Tests patch it to prove the limit.

**See also.** `edge_routing:COMM_mixin_generate_unique_slug`; test `test_routing_mixin.py`.

### `edge_routing:COMM_mixin_generate_unique_slug` [@ANCHOR: edge_routing:COMM_mixin_generate_unique_slug]

**Purpose.** Produces a URL-safe slug that is unique across the whole site namespace, from a name or a requested slug.

**Signature.** `_generate_unique_slug(self, base_string, record_id=False, forbidden_slugs=None)`. `base_string`: text to slugify; an empty value returns `""`. `record_id`: the record being updated, excluded from the collision search on its own model. `forbidden_slugs`: extra slugs to avoid, used to keep several records in one batch apart. Returns the slug string.

**Behaviour.** It slugifies the text, gathers reserved words, forbidden slugs and every slug starting with the base from each routing model (inactive records included, via the zero-sudo service account when available), then tries `base`, `base-1`, `base-2` and so on, up to 1000 times. A free candidate is claimed with `pg_try_advisory_xact_lock` on its hash, held until the transaction ends, so two concurrent transactions cannot take the same slug.

**Errors.** `ValidationError` after 1000 attempts.

**See also.** `edge_routing:COMM_check_slug_collision`, `edge_routing:COMM_get_routing_models`; test `test_routing_mixin.py`.

### `edge_routing:COMM_mixin_get_record_by_slug` [@ANCHOR: edge_routing:COMM_mixin_get_record_by_slug]

**Purpose.** Resolves a public URL slug to the id of the record that owns it, for member and group site routing.

**Signature.** `@api.private @api.model @distributed_cache() get_record_by_slug(self, slug)`. `slug`: the first path segment of a request (lower-cased inside). Returns the record id (`int`) or `False`; `False` for an empty slug.

**Behaviour.** Runs as the edge-routing service account when it exists (otherwise the caller's environment) with `active_test=False`, and searches `website_slug = slug` with a plain `=`: an `=ilike` would treat a visitor-supplied `%` or `_` as wildcards. The result is cached in the distributed Redis cache.

**Special concerns.** Not callable over RPC (`@api.private`) and takes no caller-supplied user. The cache entry is invalidated through `_notify_cache_invalidation` when a slug is written or deleted (`edge_routing:COMM_mixin_write`, `edge_routing:COMM_mixin_unlink`).

**See also.** Verified through `user_websites:test_group_site_routing`.

### `edge_routing:COMM_mixin_write` [@ANCHOR: edge_routing:COMM_mixin_write]

**Purpose.** Keeps slugs valid and unique when a record changes, and keeps the slug cache correct.

**Signature.** `write(self, vals)` on `edge.routing.mixin`; returns the result of `super().write`.

**Behaviour.** A requested `website_slug` is checked against reserved words (`ValidationError`), refused for more than one record at once (`UserError`) and rewritten by `_generate_unique_slug`, so a taken slug gets a numeric suffix. A record with no slug whose `name` is written gets a slug generated from the name unless the context key `edge_routing_no_name_slug` is set; for a batch write the missing slugs are set with one SQL `UPDATE`. After the write, when slug or name changed, it notifies cache invalidation for the old and new slugs through `zero_sudo.security.utils._notify_cache_invalidation`.

**Errors.** The notification is best effort: failures are logged, not raised.

**See also.** `edge_routing:COMM_mixin_get_record_by_slug`; tests `test_routing_mixin.py`, `user_websites:test_slug_cache_invalidation`.

### `edge_routing:COMM_mixin_create` [@ANCHOR: edge_routing:COMM_mixin_create]

**Purpose.** Gives every new record of a routing model a unique, valid slug without the caller having to compute one.

**Signature.** `@api.model_create_multi create(self, vals_list)` on `edge.routing.mixin`; returns the new recordset.

**Behaviour.** For each dict: an explicit `website_slug` is slugified and, if reserved, rejected with `ValidationError`; otherwise it is made unique by `_generate_unique_slug`, which adds a numeric suffix when the slug is merely taken. With no slug, a `name` produces one (skipped when the context key `edge_routing_no_name_slug` is set). Slugs assigned earlier in the same batch are passed as `forbidden_slugs` so two records in one call do not collide.

**Special concerns.** Takes an advisory lock per chosen slug for the transaction.

**See also.** `edge_routing:COMM_mixin_generate_unique_slug`, `edge_routing:COMM_check_reserved_slugs`; test `test_routing_mixin.py`.

### `edge_routing:COMM_mixin_unlink` [@ANCHOR: edge_routing:COMM_mixin_unlink]

**Purpose.** Releases a deleted record's slug and makes sure no stale cache entry still resolves it.

**Signature.** `unlink(self)` on `edge.routing.mixin`; returns the result of `super().unlink()`.

**Behaviour.** It collects the non-empty slugs of the records first, deletes them, then calls `zero_sudo.security.utils._notify_cache_invalidation(self._name, slugs)` so `get_record_by_slug` caches (here and on other workers) forget them. The slug becomes available for reuse at once, because the uniqueness constraint is on stored rows.

**Errors.** A failing notification (a `pg_notify` database error) is logged as a warning and ignored; the delete is not undone.

**See also.** `edge_routing:COMM_mixin_get_record_by_slug`, `edge_routing:COMM_mixin_write`; test `test_routing_mixin.py`.
