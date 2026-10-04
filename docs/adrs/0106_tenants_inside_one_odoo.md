# ADR 0106: Other Sites Are Tenants Inside hams.com's Odoo

## Status
Accepted (Bruce, 2026-10-04: perens.com, postopen.org and the parking site "are tenants INSIDE hams.com's
Odoo (hams_prod, one database), not separate instances", and "Odoo is the single source of truth" for the
Cloudflare tunnel and DNS configuration).

Earlier the same night a design with one Odoo instance, OS user, PostgreSQL role and database per site
was built and three such instances (perens_com, postopen_org, parking) were run on hams1. Bruce rejected
that design before it was written down as an ADR; this ADR is the architecture decision itself. The
instance tooling it replaced (`tenant_ctl.py`, `tenant_lib.py`, the `hams-tenant@` units) is retired.

## Context

hams.com's own Odoo already has the parts of a multi-site server: Odoo's `website` model (many websites in
one database, each selected by its `domain`), the `cloudflare` module (tunnel ingress rows that Odoo
pushes to Cloudflare, per-website credentials, Custom Hostnames for domains whose zone is not in the
account) and `edge_routing` (a registry of members' custom domains). One tunnel, one Odoo, one database,
one place that says what Cloudflare must do. A second, third and fourth Odoo each with its own tunnel
rules, its own port and its own provisioning tool made the Cloudflare configuration a thing that Odoo did
not know about; an API push of those rules was overwritten by the next Odoo push, which is how the rule
about not touching Cloudflare from the Cloudflare side came about.

## Decision

**1. A tenant is a website of hams_prod bound to hostnames.** `tenant_sites` (hams_open) holds
`tenant.site` (website, hostnames) and `tenant.site.own.host` (the hostnames of the main site). No company
and no database per tenant.

**2. One request router decides what a hostname is.** By the hostname the client sent (never
`X-Forwarded-Host`):

| Kind | Rule | Served |
|---|---|---|
| main | an own hostname (`hams.com`, `*.hams.com`) or a member's `edge.routing.domain` | exactly as before |
| tenant | a `tenant.site.host` | that website only, GET/HEAD only, a path allow-list, controllers of `website`, `website_blog`, `web`, `http_routing`, `edge_cache` only |
| parking | a `parking.domain` | the parked page, redirect, for-sale page or 410, no cookie, no script |
| unknown | anything else, once own hostnames exist | an uncached 404 |

A request whose Host is not a domain name (`localhost`, an IP address, a one-word name) is never
classified: this is what keeps hams.com's own daemons (JSON-2 callers on loopback), the test harness and
operators working. Classification does not depend on `CF-Ray` being present, because that header's
behaviour through the tunnel was not verified.

**3. Content isolation is enforced in the ORM, not by convention.** While a tenant request is served,
`website.page`, `website.menu`, `website.rewrite`, `blog.blog` and `blog.post` find only records of the
tenant's website. Odoo otherwise shows a record with no website on every website, which on hams_prod would
publish hams.com's generic pages (`/privacy`, `/terms`) and blogs on perens.com. The main site is untouched.

**4. Everything reaches Odoo through the existing tunnel.** The tunnel catch-all stays `http://localhost:8069`;
no per-tenant or per-domain ingress rule is needed. The path rules for daemon ports (`^/websocket$`, `^/adif$`,
...) are scoped to `hams.com` and `*.hams.com` so a tenant or parked hostname cannot reach them.
`cloudflare.tunnel._ingress_problems` refuses a push that leaves a rule with a path and no hostname while any
tenant or parked hostname exists. `cloudflare.tunnel._build_ingress()` returns what a push would send, with no
network call, so a plan can be compared with the live list before anything is pushed.

**5. Static files.** perens.com's 1.1 GB `/static/` tree is served by the existing read-only static server
(`static_site_server.py`, unit `hams-static@perens_com`, loopback 18201) through one Odoo-managed route row
(`perens.com` and `www.perens.com`, path `^/static/`). It is a file server, not an Odoo; Odoo's filestore is a poor
home for 1.1 GB of PDFs and video.

**6. DNS and Custom Hostnames.** The `cloudflare` module's `cloudflare.dns.record` model is data only: nothing pushes it to Cloudflare. Zones that are in the
account already point at the tunnel, so nothing needs to change. `edge.routing.domain` creates a real
Cloudflare Custom Hostname when a record is created for a name that matches a website's domain; it must not be
used for a zone that is already in the account. Giving `cloudflare.dns.record` a push is future work; until then existing records (including `stun.hams.com`, which must stay) are noted in it as data and never changed.

## Why not separate instances

Per-tenant OS users, PostgreSQL roles, nftables rules, ports and units bought isolation that Odoo's own
website model plus the request router give for the cases that exist (public, read-only, low-traffic sites with
no logins), at the price of a second configuration source for Cloudflare, tooling that had to be run on the
production host, per-instance upgrades and backups, and a catch-all that pointed at something other than the
Odoo that owns the tunnel. A tenant that ever needs real isolation (its own administrators, uploads of code)
should be a separate server, not a fifth process on hams1.

## Consequences

* A bug in the router or in the scoped search could expose hams.com content on a tenant hostname. The tests
  cover the pages, blogs, redirects, backend paths, methods and forged forwarded hosts; they must be kept green.
* Anyone with administrator access to hams_prod can edit tenant sites. The tenants have no logins of their own.
* A hostname mapped by mistake to the main site's own patterns is main: `tenant.site.host` and
  `parking.domain` refuse a name that matches an own pattern.
* Cache purges for tenant sites are not sent (their websites carry no Cloudflare credentials); tenant pages rely
  on their time-to-live.
