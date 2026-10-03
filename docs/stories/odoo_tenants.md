# Story: Running Several Small Odoo Sites Next to hams.com

As a **System Administrator**, I want to **create an isolated Odoo instance for a small site from a
short declarative spec**, so that perens.com, postopen.org and a parking instance can share the
production machine without any of them being able to reach hams.com's database, Redis, RabbitMQ or
daemon keys. Design: `docs/proposals/MULTI_TENANT_ODOO.md` (hams_com), ADR 0105. (These tools live under `tools/`, which the anchor
linters do not scan, so this story carries no anchors.)

## Scenario: Writing a spec

A spec names the tenant, its domains, its loopback port and its limits. Names, domains (IDNA, never
under hams.com), ports (only 18000-18999), worker counts (a gevent port must be explicit: Odoo's default
8072 is hams_prod's), modules (only an allow-list of hams_open modules besides vanilla ones) and the
whole fleet (unique names, domains and ports, one tunnel catch-all) are validated before anything
runs.

## Scenario: Planning and creating a tenant

`tenant_ctl create SPEC` prints the plan and changes nothing; `--apply` runs the same steps. Every step
has a read-only check, so a second run, or a run after a failure, does only what is left. The tenant gets its own account, a PostgreSQL role that
authenticates by peer over the unix socket (no database password exists), its own database, an odoo.conf with no secret in it, a systemd drop-in with its memory and CPU limits, and a firewall table that stops its account opening new
connections to local services.

## Scenario: Backups, restore tests, status and removal

A backup is a `pg_dump -Fc` plus the filestore with checksums; the
weekly restore test restores the newest backup into a scratch database and drops it. Status reports the unit, sizes, backup age and HTTP probes. Removal needs the tenant's exact name, takes a final backup first and
never touches the backups.

## Scenario: Upgrading after an Odoo package upgrade

The Debian package is shared, so every instance runs the new code after its next restart.
`tenant_ctl upgrade NAME` backs the tenant up, stops it, runs `odoo -u` for its modules as its own
account and starts it, and reports whether it answers; tenants are done one at a time.

## Scenario: Sending the tenants through the tunnel

The tunnel's existing path-only rules match every hostname, so they are first scoped to hams.com, then
tenant hostnames are put in front and the catch-all goes to the parking instance. Only a reviewed list
whose digest was approved can be applied, and only when the live configuration has not changed since
it was read.

## Scenario: Bulk parked domains

`parking_ctl` reads a text file of domains, validates every line and refuses a file with any problem, compares it with the records and changes only what differs.

## Scenario: Exporting a site from an offline snapshot

When a source server must not be contacted again, `odoo_site_migrate export --snapshot-db` reads a restored database dump and its filestore through the same five read methods, produces the same export files a live export would, leaves out the untouched module and theme view copies Odoo made for the website, and records in the export's inventory what the database holds that is not site content.

## Scenario: A public-site tenant has no signup, no login and no mail

`lockdown` sets invitation-only signup and no password reset, deactivates every outgoing mail server, mail template and mail scheduled action, and deletes nothing; the import replaces website forms by a static notice; the tunnel answers 404 for the backend, login and form paths of a tenant whose spec says `public_site_only`.

## Scenario: Moving a site's content in

`odoo_site_migrate` reads the source through a wrapper that can only call read methods, keeps its credentials out of every output, rewrites links, attachment ids and blog ids inside content and proves the export is intact before importing it.
