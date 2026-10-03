# Story: Running Several Small Odoo Sites Next to hams.com

As a **System Administrator**, I want to **create an isolated Odoo instance for a small site from a
short declarative spec**, so that perens.com, postopen.org and a parking instance can share the
production machine without any of them being able to reach hams.com's database, Redis, RabbitMQ or
daemon keys. Design: `docs/proposals/MULTI_TENANT_ODOO.md` (hams_com), ADR 0105.

## Scenario: Writing a spec

A spec names the tenant, its domains, its loopback port and its limits. Names, domains (IDNA, never
under hams.com), ports (only 18000-18999), worker counts (a gevent port must be explicit: Odoo's default
8072 is hams_prod's), modules (only an allow-list of hams_open modules besides vanilla ones) and the
whole fleet (unique names, domains and ports, one tunnel catch-all) are validated before anything
runs [@ANCHOR: tenant_lib:validate_spec].

## Scenario: Planning and creating a tenant

`tenant_ctl create SPEC` prints the plan and changes nothing; `--apply` runs the same steps. Every step
has a read-only check, so a second run, or a run after a failure, does only what is left
[@ANCHOR: tenant_lib:build_create_steps]. The tenant gets its own account, a PostgreSQL role that
authenticates by peer over the unix socket (no database password exists), its own database
[@ANCHOR: tenant_lib:render_pg_hba], an odoo.conf with no secret in it
[@ANCHOR: tenant_lib:render_odoo_conf], a systemd drop-in with its memory and CPU limits
[@ANCHOR: tenant_lib:render_unit_dropin], and a firewall table that stops its account opening new
connections to local services [@ANCHOR: tenant_lib:render_nft].

## Scenario: Backups, restore tests, status and removal

A backup is a `pg_dump -Fc` plus the filestore with checksums [@ANCHOR: tenant_lib:backup_tenant]; the
weekly restore test restores the newest backup into a scratch database and drops it
[@ANCHOR: tenant_lib:restore_test]. Status reports the unit, sizes, backup age and HTTP probes
[@ANCHOR: tenant_lib:status]. Removal needs the tenant's exact name, takes a final backup first and
never touches the backups [@ANCHOR: tenant_lib:delete_tenant].

## Scenario: Sending the tenants through the tunnel

The tunnel's existing path-only rules match every hostname, so they are first scoped to hams.com, then
tenant hostnames are put in front and the catch-all goes to the parking instance. Only a reviewed list
whose digest was approved can be applied, and only when the live configuration has not changed since
it was read [@ANCHOR: tenant_cloudflare:build_ingress].

## Scenario: Bulk parked domains

`parking_ctl` reads a text file of domains, validates every line and refuses a file with any problem
[@ANCHOR: parking_ctl:parse_domain_file], compares it with the records and changes only what differs
[@ANCHOR: parking_ctl:diff].

## Scenario: Moving a site's content in

`odoo_site_migrate` reads the source through a wrapper that can only call read methods
[@ANCHOR: odoo_site_migrate:read_only_source], keeps its credentials out of every output
[@ANCHOR: odoo_site_migrate:load_credentials], rewrites links, attachment ids and blog ids inside content
[@ANCHOR: odoo_site_migrate:rewrite_html] and proves the export is intact before importing it
[@ANCHOR: odoo_site_migrate:verify_export].
