# Story: Other Websites as Tenants of hams.com's Odoo

As the **owner of hams.com** I want **perens.com, postopen.org and other sites to be websites of the same Odoo**
(one database, one Cloudflare tunnel, one place that says what the tunnel does), so that each site shows only
its own public content, nothing of hams.com's members or backend is reachable on its names, and the Cloudflare
configuration has a single source of truth. The decision is ADR 0106.

## Scenario: What a request for a hostname is

The hostname used is the one the client sent, never `X-Forwarded-Host`, which Odoo's proxy mode would otherwise
substitute [@ANCHOR: tenant_sites:COMM_normalize_host] [@ANCHOR: tenant_sites:COMM_original_host]; a request whose
two hostnames differ is refused [@ANCHOR: tenant_sites:COMM_classify]. The router puts every request in one of
five kinds: the main site, a tenant, a parked domain, an unknown hostname, or a refused one
[@ANCHOR: tenant_sites:COMM_match_guard]. A name that is not a domain name (`localhost`, an address, a one-word
name: this machine's daemons, the test harness, an operator on loopback) is never classified.

The main site's hostnames are listed as patterns, `example.com` and `*.example.com`
[@ANCHOR: tenant_sites:COMM_host_matches] [@ANCHOR: tenant_sites:COMM_valid_host_pattern]
[@ANCHOR: tenant_sites:COMM_own_host_normalize] [@ANCHOR: tenant_sites:COMM_own_host_patterns]. Until one is
listed nothing changes for a hostname that is not a tenant's. A tenant or parked hostname may never match one
[@ANCHOR: tenant_sites:COMM_host_not_own].

## Scenario: A visitor opens a tenant site

The tenant's hostnames select its website, `www.` included [@ANCHOR: tenant_sites:COMM_host_normalize]
[@ANCHOR: tenant_sites:COMM_host_create] [@ANCHOR: tenant_sites:COMM_host_write]
[@ANCHOR: tenant_sites:COMM_website_id_for_host] [@ANCHOR: tenant_sites:COMM_current_website_id]
[@ANCHOR: tenant_sites:COMM_request_website_id] [@ANCHOR: tenant_sites:COMM_request_state].

Only GET, HEAD and OPTIONS are served [@ANCHOR: tenant_sites:COMM_tenant_match]. Only the paths of an allow-list
are routes, and only from the `website`, `website_blog`, `web`, `http_routing` and `edge_cache` controllers
[@ANCHOR: tenant_sites:COMM_tenant_path_allowed] [@ANCHOR: tenant_sites:COMM_tenant_module_allowed]. Everything
else (the backend, login, JSON-RPC, XML-RPC, JSON-2, the websocket, every route of a members' module) is refused
as an unknown page [@ANCHOR: tenant_sites:COMM_route_module] [@ANCHOR: tenant_sites:COMM_refuse].

Odoo shows a record that has no website on every website. On a tenant the page of a URL
[@ANCHOR: tenant_sites:COMM_scoped_page_info], a redirect [@ANCHOR: tenant_sites:COMM_serve_redirect], the blogs
and posts [@ANCHOR: tenant_sites:COMM_scoped_search] and a record named in a URL
[@ANCHOR: tenant_sites:COMM_scoped_access] must belong to the tenant's own website, so hams.com's pages, blogs
and redirects never show. A tenant site has no "Sign in" link [@ANCHOR: tenant_sites:COMM_site_create]
[@ANCHOR: tenant_sites:COMM_hide_login_link].

## Scenario: A parked or unknown hostname

A module may add a kind of hostname, and its own routes [@ANCHOR: tenant_sites:COMM_extra_kind]
[@ANCHOR: tenant_sites:COMM_public_route] (parking adds `parking` and the for-sale form post;
[@ANCHOR: parking:COMM_extra_kind] [@ANCHOR: parking:COMM_public_route]). Any other hostname gets a plain 404
that is never cached and carries no cookie [@ANCHOR: tenant_sites:COMM_serve_fallback]
[@ANCHOR: tenant_sites:COMM_serve_other] [@ANCHOR: tenant_sites:COMM_plain_response]
[@ANCHOR: tenant_sites:COMM_post_dispatch].

## Scenario: The tunnel is pushed

A push is refused while any tenant or parked hostname exists and a rule has a path but no hostname, because such
a rule would send those hostnames to a daemon port [@ANCHOR: tenant_sites:COMM_ingress_problems]
[@ANCHOR: tenant_sites:COMM_tenant_hosts_exist] [@ANCHOR: parking:COMM_tenant_hosts_exist].

## Scenario: Who may read the tables

Only the manager group and the router's service account read the host tables; the anonymous user has no access
to any of them [@ANCHOR: tenant_sites:COMM_service_account_acl]. The module's documentation is installed with it
[@ANCHOR: tenant_sites:COMM_post_init_hook].
