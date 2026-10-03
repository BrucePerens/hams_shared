# ADR 0105: Odoo Tenants Are Separate Instances Next to hams.com

## Status
Proposed (night-shift branch `night-shift/multi-tenant`; Bruce's instruction 2026-10-03: "Set up the
production machine for multi-tenant", an Odoo tenant each for perens.com, postopen.org and a parking
instance for many parked domains, all through the same Cloudflare tunnel as hams.com).

## Context

`hams1` runs one Odoo (`/etc/odoo/odoo.conf`: `dbfilter ^hams_prod$`, `list_db False`, 25 workers) whose
single database the hams daemons reach by default-database JSON-2 calls, with Redis, RabbitMQ and the
daemon key manager behind it. The new sites are small, low-traffic, and belong to other brands. Three
shapes were considered; the full comparison, with measured memory, is in `docs/proposals/
MULTI_TENANT_ODOO.md` (hams_com).

1. One multi-database Odoo (`dbfilter` by hostname) shared with hams_prod. Rejected: the daemons' JSON-2
   calls name no database and would depend on `db_name`; cron, workers, `admin_passwd`, the loaded
   addons (`/opt/hams/src/hams_com`, proprietary) and the crash domain would all be shared, and a tenant
   administrator would see hams.com's module list.
2. One database with Odoo's multi-website. Rejected for the same sharing, and because it puts a stranger's
   site in hams.com's database and filestore.
3. **One Odoo instance per tenant.** Chosen. Measured on Odoo 19 with `website` and `website_blog`
   installed: a threaded instance (`workers = 0`) is about 140-190 MB resident; the same site with
   2 workers is about 630 MB. A shared one-process multi-database instance would save about 280 MB for
   three tenants and give up everything in the isolation list below.

## Decision

* Each tenant is declared by a small JSON spec (`hams_shared/tools/tenants/*.json`) and created by
  `tenant_ctl.py create` (dry run by default) from `tenant_lib.py`: its own operating-system user
  `t_<name>`, PostgreSQL role `t_<name>` (login by peer authentication over the unix socket, so no
  database password exists), database `<name>`, filestore under `/var/lib/hams-tenants/<name>`,
  `odoo.conf` under `/etc/hams-tenants/<name>` and an instance of the sandboxed template unit
  `hams-tenant@.service`. Names, domains, ports (18000-18999) and modules are validated; a tenant can
  load vanilla Odoo addons and only the allow-listed hams_open module `parking` (copied, root-owned,
  into `/usr/local/lib/hams-tenant-addons/<name>`), never a hams_com module.
* Isolation: a `hams_tenant` group role and `pg_hba` lines (`include_dir`, one-time line in
  `pg_hba.conf`) let a tenant role connect only to its own database and to `postgres`, and refuse it
  everywhere else on every transport; the tenant unit runs with `ProtectSystem=strict`,
  `ProtectProc=invisible`, `InaccessiblePaths=/opt/hams ...`, no capabilities and a memory, CPU and task
  ceiling; an nftables table keyed to the tenant uids refuses new connections to anything local (Redis,
  RabbitMQ, the hams daemons, PostgreSQL over TCP), the WireGuard network and the cloud metadata
  address, while loopback replies to cloudflared and outbound internet stay open.
* New provisioning vocabulary: host class `odoo_tenants` (like `ca_signer`): a test host never gets
  tenant directories or units; `tenant_ctl create --apply` refuses a host without the class.
* Cloudflare: every hams.com path rule that has no hostname matches every host, so it is first scoped
  to `hams.com` and `*.hams.com`, tenant hostnames go first, and the catch-all moves to the parking
  instance. This is a reviewed whole-list replacement derived from the live list
  (`tenant_cloudflare.py`), never a push of hams_prod's `cloudflare.tunnel.route` rows.
* Parking: the `parking` module (hams_open) answers every public request of the parking instance from
  its own table through `ir.http._match` and `_serve_fallback`; no website, no cookie, no route or backend page
  reachable from the internet (Odoo's `/<module>/static/*` files are still served). It depends only on `web` and `mail` (a tenant has no Redis, so
  `zero_sudo` cannot be installed there; `mail` is there for the service account's `notification_type`
  and brings its own cron jobs, hence `max_cron_threads = 1`); its privilege separation is one service
  account holding exactly the ACLs the public handler needs.

## Consequences

* Odoo package upgrades apply to every instance: tenants are upgraded one at a time
  (`MULTI_TENANT_ODOO.md`, "Upgrades").
* PostgreSQL's `max_connections` (100, about 45 used) is shared: each tenant's pool and role limit are
  small (`db_maxconn 10`, role limit 14).
* The `/xmlrpc` and `/jsonrpc` endpoints that `parking_ctl` and `odoo_site_migrate` use are deprecated
  in Odoo 19 and removed in 20; both tools keep their transport behind one class so a JSON-2 transport
  can replace it.
* hams_open's `cloudflare` module (Odoo-driven tunnel routes) is not installed in tenants; HTML edge
  caching for tenant sites needs its own small module (to-do) or Cloudflare cache rules.
