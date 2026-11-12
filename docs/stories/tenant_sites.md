# Story: Other Websites as Tenants of hams.com's Odoo

As the **owner of hams.com** I want **perens.com, postopen.org and other sites to be websites of the same Odoo**
(one database, one Cloudflare tunnel (the connection that carries public web traffic to the machine), one place that says what the tunnel does), so that each site shows only
its own public content, nothing of hams.com's members or backend is reachable on its names, and the Cloudflare
configuration has a single source of truth. The decision is [ADR (Architecture Decision Record) 0106](../adrs/0106_tenants_inside_one_odoo.md): a *tenant* is a website of hams.com's own Odoo, bound to one or more hostnames, with no company or database of its own.

## Scenario: What a request for a hostname is

The hostname used is the one the client sent, never `X-Forwarded-Host`, which Odoo's proxy mode would otherwise
substitute `tenant_sites:COMM_normalize_host` `tenant_sites:COMM_original_host`; a request whose
two hostnames (the one the client sent and `X-Forwarded-Host`) differ is refused `tenant_sites:COMM_classify`. The router puts every request in one of
five kinds: the main site, a tenant, a parked domain, an unknown hostname, or a refused one
`tenant_sites:COMM_match_guard`. A name that is not a domain name (`localhost`, an address, a one-word
name: this machine's daemons, the test harness, an operator on loopback) is never classified.

The main site's hostnames are listed as patterns, `example.com` and `*.example.com`
`tenant_sites:COMM_host_matches` `tenant_sites:COMM_valid_host_pattern`
`tenant_sites:COMM_own_host_normalize` `tenant_sites:COMM_own_host_patterns`. Until one is
listed nothing changes for a hostname that is not a tenant's. A tenant or parked hostname may never match one
`tenant_sites:COMM_host_not_own`.

## Scenario: A visitor opens a tenant site

The tenant's hostnames select its website, `www.` included `tenant_sites:COMM_host_normalize`
`tenant_sites:COMM_host_create` `tenant_sites:COMM_host_write`
`tenant_sites:COMM_website_id_for_host` `tenant_sites:COMM_current_website_id`
`tenant_sites:COMM_request_website_id` `tenant_sites:COMM_request_state`.

Only GET, HEAD and OPTIONS (the read-only HTTP methods) are served `tenant_sites:COMM_tenant_match`. Only the paths of an allow-list
are routes, and only from the `website`, `website_blog`, `web`, `http_routing` and `edge_cache` controllers
`tenant_sites:COMM_tenant_path_allowed` `tenant_sites:COMM_tenant_module_allowed`. Everything
else (the backend, login, JSON-RPC, XML-RPC, JSON-2, the websocket, every route of a members' module) is refused
as an unknown page `tenant_sites:COMM_route_module` `tenant_sites:COMM_refuse`.

Odoo shows a record that has no website on every website. On a tenant the page of a URL
`tenant_sites:COMM_scoped_page_info`, a redirect `tenant_sites:COMM_serve_redirect`, the blogs
and posts `tenant_sites:COMM_scoped_search` and a record named in a URL
`tenant_sites:COMM_scoped_access` must belong to the tenant's own website, so hams.com's pages, blogs
and redirects never show. A tenant site gets Odoo's plain layout: no "Sign in" link, and none of what a module of another author (any module outside Odoo's own addons directories, whatever
its manifest says: html_editor names its author "odoo" and is stock) adds to every
website's layout, in views or in frontend scripts and styles, unless the module is listed as kept
`tenant_sites:COMM_site_create` `tenant_sites:COMM_apply_public_layout`
`tenant_sites:COMM_foreign_modules` `tenant_sites:COMM_is_stock_module`
`tenant_sites:COMM_foreign_layout_views`
`tenant_sites:COMM_is_tenant_website` `tenant_sites:COMM_asset_addons`
`tenant_sites:COMM_asset_related`.

## Scenario: A parked or unknown hostname

A module may add a kind of hostname, and its own routes `tenant_sites:COMM_extra_kind`
`tenant_sites:COMM_public_route` (parking adds `parking` and the for-sale form post;
`parking:COMM_extra_kind` `parking:COMM_public_route`). Any other hostname gets a plain 404
that is never cached and carries no cookie `tenant_sites:COMM_serve_fallback`
`tenant_sites:COMM_serve_other` `tenant_sites:COMM_plain_response`
`tenant_sites:COMM_post_dispatch`.

## Scenario: The tunnel is pushed

A push is refused while any tenant or parked hostname exists and a rule has a path but no hostname, because such
a rule would send those hostnames to a daemon port `tenant_sites:COMM_ingress_problems`
`tenant_sites:COMM_tenant_hosts_exist` `parking:COMM_tenant_hosts_exist`.

## Scenario: Who may read the tables

Only the manager group and the router's service account read the host tables; the anonymous user has no access
to any of them `tenant_sites:COMM_service_account_acl`. The module's documentation is installed with it
`tenant_sites:COMM_post_init_hook`.

## Anchor reference

Each anchor in the story has its own entry here (ADR 0113). Argument tables use the code's names; "service account" is `tenant_sites.user_tenant_sites_service` unless said otherwise.

### `tenant_sites:COMM_normalize_host` [@ANCHOR: tenant_sites:COMM_normalize_host]

**Purpose:** turns a host name from outside into the one form the router compares, or refuses it. `normalize_host(raw)` in `tenant_sites/utils.py`, a pure function (no Odoo, no database).

| Argument | Type | Meaning |
|---|---|---|
| `raw` | str | A Host header value or a hostname typed by an operator. |

**Returns:** a lower-case ASCII (punycode) name without port or trailing dot, or the empty string when it is not a plausible DNS name. Refused: not a string, a bracketed IPv6 literal, a non-numeric port, longer than 253 characters, fewer than two labels, all-numeric labels (an IPv4 address) and any label that is not 1 to 63 letters, digits or hyphens (no leading or trailing hyphen).

**Errors:** none; every bad input gives `""`. No locks or side effects.

**Security:** this is the first gate for every request, so IP addresses and one-word names such as `localhost` are never treated as a site.

**See also:** test anchor `tenant_sites:COMM_test_normalize_host` in `tenant_sites/tests/`.

### `tenant_sites:COMM_original_host` [@ANCHOR: tenant_sites:COMM_original_host]

**Purpose:** reads the Host header as the client sent it. `original_host(environ)` in `tenant_sites/utils.py`.

| Argument | Type | Meaning |
|---|---|---|
| `environ` | dict | The WSGI environ of the request. |

**Returns:** the original `HTTP_HOST` kept by werkzeug's ProxyFix in `environ["werkzeug.proxy_fix.orig"]`, else `HTTP_HOST`, else `""`.

**Why:** Odoo's proxy mode runs ProxyFix with `x_host=1`, which replaces `HTTP_HOST` with the client-supplied `X-Forwarded-Host`. Trusting that value would let a visitor make one domain answer as another and poison the edge cache. No side effects.

**See also:** test anchor `tenant_sites:COMM_test_original_host` in `tenant_sites/tests/`.

### `tenant_sites:COMM_classify` [@ANCHOR: tenant_sites:COMM_classify]

**Purpose:** decides which of five kinds a request is, once per request. `IrHttp._tenant_classify()` and `_tenant_compute_kind()` in `tenant_sites/models/ir_http.py`; the answer is memoised in the per-request state.

**Returns:** `main` (the main site's own names, member custom domains, or any name that is not a domain name), `tenant`, a kind added by another module (`parking`), `unknown`, or `reject`.

**Order of tests:** no valid host gives `main`; the original Host differing from the proxy-rewritten Host gives `reject`; a match with the main site's patterns gives `main`; a tenant host gives `tenant` (and records the website id in the request state); the `_tenant_extra_kind(host)` hook; an `edge.routing.domain` row with that name gives `main`; with no main patterns configured at all it gives `main` (so nothing changes until the operator lists them); otherwise `unknown`.

**Concerns:** several indexed lookups per request, all through the service account, cached in the request state.

**See also:** test anchor `tenant_sites:COMM_test_classify` in `tenant_sites/tests/`.

### `tenant_sites:COMM_match_guard` [@ANCHOR: tenant_sites:COMM_match_guard]

**Purpose:** the routing guard, an override of `ir.http._match(path_info)` in `tenant_sites/models/ir_http.py`. It is where the kind decides what routes exist.

**Behaviour:** `main` goes to Odoo's own matching. `tenant` goes to `_tenant_match()` (read-only, allow-listed paths and modules). Any other kind (parked, unknown, reject) raises `NotFound` unless `_tenant_public_route(kind, path, method)` says the path is a real route for that kind (parking's inquiry post).

**Returns:** the route match from Odoo, or raises `werkzeug.exceptions.NotFound`.

**Security:** this is why the login page, backend, JSON-RPC, XML-RPC, JSON-2 and websocket paths answer "not found" on every name that is not the main site. No side effects of its own.

**See also:** test anchor `tenant_sites:COMM_test_match_guard` in `tenant_sites/tests/`.

### `tenant_sites:COMM_host_matches` [@ANCHOR: tenant_sites:COMM_host_matches]

**Purpose:** tests a host name against a list of hostname patterns. `host_matches(host, patterns)` in `tenant_sites/utils.py`, pure.

| Argument | Type | Meaning |
|---|---|---|
| `host` | str | An already normalized host name. |
| `patterns` | iterable of str | Exact names, or `*.example.com` leading wildcards. |

**Returns:** `True` when the host equals a pattern, or fits a wildcard: `*.example.com` matches `a.example.com` and `a.b.example.com` but not `example.com` itself. `False` otherwise or for an empty list.

**See also:** test anchor `tenant_sites:COMM_test_host_matches` in `tenant_sites/tests/`.

### `tenant_sites:COMM_valid_host_pattern` [@ANCHOR: tenant_sites:COMM_valid_host_pattern]

**Purpose:** normalizes one entry of the main site's host list. `normalize_host_pattern(raw)` in `tenant_sites/utils.py`, pure.

| Argument | Type | Meaning |
|---|---|---|
| `raw` | str or None | `example.com` or `*.example.com`. |

**Returns:** the normalized name, or `*.` followed by the normalized base for a wildcard, or `""` when it is neither (including a wildcard over an invalid name). Used by the own-host model before saving.

**See also:** test anchor `tenant_sites:COMM_test_valid_host_pattern` in `tenant_sites/tests/`.

### `tenant_sites:COMM_own_host_normalize` [@ANCHOR: tenant_sites:COMM_own_host_normalize]

**Purpose:** validates and normalizes the `name` of a `tenant.site.own.host` row before it is saved. `TenantSiteOwnHost._normalize_vals(vals)` in `tenant_sites/models/tenant_site_own_host.py`; called by `create` and `write`.

| Argument | Type | Meaning |
|---|---|---|
| `vals` | dict | The values being written; changed in place. |

**Behaviour:** does nothing when `name` is absent; otherwise replaces it with `normalize_host_pattern(name)`. **Errors:** `ValidationError("'%s' is not a valid hostname or pattern.")`. The table also has a unique constraint on `name`.

**See also:** test anchor `tenant_sites:COMM_test_own_host_normalize` in `tenant_sites/tests/`.

### `tenant_sites:COMM_own_host_patterns` [@ANCHOR: tenant_sites:COMM_own_host_patterns]

**Purpose:** the list of hostname patterns that belong to the main site. `tenant.site.host._own_host_patterns()` in `tenant_sites/models/tenant_site_host.py`.

**Returns:** a tuple of up to 1000 pattern strings from `tenant.site.own.host`. An empty tuple means nothing is configured, and the router then leaves every unrecognised host as the main site.

**Special concerns:** read as the tenant service account, and memoised in the per-request state so one request reads the table once. A change to the table takes effect on the next request. No writes.

**See also:** test anchor `tenant_sites:COMM_test_own_host_patterns` in `tenant_sites/tests/`.

### `tenant_sites:COMM_host_not_own` [@ANCHOR: tenant_sites:COMM_host_not_own]

**Purpose:** a database constraint that keeps a tenant from taking a main-site name. `TenantSiteHost._check_not_own_host()` in `tenant_sites/models/tenant_site_host.py`, triggered on `name`.

**Behaviour:** for each record, if `host_matches(name, own_patterns)` it raises `ValidationError("%s is a hostname of the main site and cannot belong to a tenant.")`. It runs after normalization, so `WWW.Hams.Com` is caught too. The parking module has the matching check for parked domains (`parking:COMM_domain_not_main`).

**Security:** prevents a tenant row from hijacking hams.com or its members' pages.

**See also:** test anchor `tenant_sites:COMM_test_host_not_own` in `tenant_sites/tests/`.

### `tenant_sites:COMM_host_normalize` [@ANCHOR: tenant_sites:COMM_host_normalize]

**Purpose:** normalizes a tenant hostname before it is stored. `TenantSiteHost._normalize_vals(vals)` in `tenant_sites/models/tenant_site_host.py`.

| Argument | Type | Meaning |
|---|---|---|
| `vals` | dict | Values being created or written; `name` is replaced in place. |

**Behaviour:** skips records without `name`; otherwise stores `normalize_host(name)`. **Errors:** `ValidationError("'%s' is not a valid hostname.")` for an IP address, one-word name or bad characters. The `name` is unique across all tenants.

**See also:** test anchor `tenant_sites:COMM_test_host_normalize` in `tenant_sites/tests/`.

### `tenant_sites:COMM_host_create` [@ANCHOR: tenant_sites:COMM_host_create]

**Purpose:** `TenantSiteHost.create(vals_list)` in `tenant_sites/models/tenant_site_host.py`, `@api.model_create_multi`. It runs `_normalize_vals` on every dict first, then calls the parent, so every stored hostname is lower-case ASCII. Returns the new records. **Errors:** `ValidationError` for an invalid name, and the unique-name constraint for a hostname already owned by a tenant. Access is limited to the tenant-sites manager group.

**See also:** tested in `tenant_sites/tests/test_models.py` (the test cites this anchor).

### `tenant_sites:COMM_host_write` [@ANCHOR: tenant_sites:COMM_host_write]

**Purpose:** `TenantSiteHost.write(vals)` in `tenant_sites/models/tenant_site_host.py`. It normalizes `name` in `vals` through `_normalize_vals` and then calls the parent, so a rename cannot introduce an unnormalized hostname. Returns the parent's boolean. **Errors:** `ValidationError` for an invalid name. Since requests find a tenant by hostname, a rename takes effect for the next request, with no cache to clear beyond the per-request state.

**See also:** tested in `tenant_sites/tests/test_models.py` (the test cites this anchor).

### `tenant_sites:COMM_website_id_for_host` [@ANCHOR: tenant_sites:COMM_website_id_for_host]

**Purpose:** finds the tenant website for a hostname. `tenant.site.host._website_id_for_host(raw_host)` in `tenant_sites/models/tenant_site_host.py`.

| Argument | Type | Meaning |
|---|---|---|
| `raw_host` | str | A Host header value; it is normalized first. |

**Returns:** the website id, or `0` when the host is invalid or belongs to no active tenant site (`site_id.active`).

**Special concerns:** one indexed search as the service account, memoised per request under the key `("site", host)`. Called by the router, by `website._get_current_website_id` and by the parking module's check that a parked domain does not shadow a tenant.

**See also:** test anchor `tenant_sites:COMM_test_website_id_for_host` in `tenant_sites/tests/`.

### `tenant_sites:COMM_current_website_id` [@ANCHOR: tenant_sites:COMM_current_website_id]

**Purpose:** makes a tenant's hostname select its website. `Website._get_current_website_id(domain_name, fallback=True)` in `tenant_sites/models/website.py`, an override of Odoo's lookup.

| Argument | Type | Meaning |
|---|---|---|
| `domain_name` | str | The request's domain. |
| `fallback` | bool | Passed to Odoo: fall back to the first website when nothing matches. |

**Returns:** the tenant website id when the host belongs to a tenant (`www.` included), else Odoo's own answer. Without this override the site would be served as whichever website Odoo's default lookup found.

**See also:** test anchor `tenant_sites:COMM_test_current_website_id` in `tenant_sites/tests/`.

### `tenant_sites:COMM_request_website_id` [@ANCHOR: tenant_sites:COMM_request_website_id]

**Purpose:** tells other code which tenant website, if any, the current request is for. `tenant.site.host._tenant_request_website_id()` in `tenant_sites/models/tenant_site_host.py`.

**Returns:** the website id, or `0` when no request is being served or it is not a tenant request.

**Special concerns:** reads only the per-request state the router already filled in; it never queries the database, so the models it guards (`website.page`, `blog.post` and so on) can call it from inside their own searches without recursion. No side effects.

**See also:** test anchor `tenant_sites:COMM_test_request_website_id` in `tenant_sites/tests/`.

### `tenant_sites:COMM_request_state` [@ANCHOR: tenant_sites:COMM_request_state]

**Purpose:** per-request scratch storage. `state()` in `tenant_sites/request_state.py`.

**Returns:** the dict for the request being served, or `None` when there is no request (cron, tests without a request). Keys in use: `kind`, `own`, `website_id` and `("site", host)`.

**Special concerns:** held in a `WeakKeyDictionary` keyed by the werkzeug request object, so the data is freed with the request and nothing is attached to Odoo's request proxy. One dict per request, and a request is handled by one thread, so no locks.

**See also:** test anchor `tenant_sites:COMM_test_request_state` in `tenant_sites/tests/`.

### `tenant_sites:COMM_tenant_match` [@ANCHOR: tenant_sites:COMM_tenant_match]

**Purpose:** routes a request for a tenant host. `IrHttp._tenant_match(path_info)` in `tenant_sites/models/ir_http.py`, called from the routing guard.

**Behaviour:** a method other than GET, HEAD or OPTIONS is refused; a path outside the allow-list is refused; a path whose matching controller is not in an allowed module is refused; otherwise Odoo's own `_match` runs. **Returns** that match; every refusal is a `NotFound`, never a 405, so a visitor cannot learn which backend routes exist.

**Security:** this is the central rule that makes a tenant site read-only and public-content-only.

**See also:** test anchor `tenant_sites:COMM_test_tenant_match` in `tenant_sites/tests/`.

### `tenant_sites:COMM_tenant_path_allowed` [@ANCHOR: tenant_sites:COMM_tenant_path_allowed]

**Purpose:** the path allow-list for tenant hosts. `tenant_path_allowed(path)` in `tenant_sites/utils.py`, pure.

**Returns:** `True` when `path` matches `TENANT_PATH_ALLOW`: `/`, `/blog` and below, `/web/assets/`, `/web/static/`, `/web/image` and `/web/content`, `/website/static` and `/website/image`, a module's `/static/` files, `/robots.txt`, `/sitemap*.xml` and `/favicon.ico`. An empty path is `False`.

**Concerns:** only a first filter; `tenant_module_allowed` also checks which controller answers the path.

**See also:** test anchor `tenant_sites:COMM_test_tenant_path_allowed` in `tenant_sites/tests/`.

### `tenant_sites:COMM_tenant_module_allowed` [@ANCHOR: tenant_sites:COMM_tenant_module_allowed]

**Purpose:** second filter for tenant requests, by controller module. `tenant_module_allowed(path, module_name)` in `tenant_sites/utils.py`, pure.

| Argument | Type | Meaning |
|---|---|---|
| `path` | str | The request path. |
| `module_name` | str or None | The Python module of the controller the path would route to. |

**Returns:** for `/` and `/robots.txt` only `odoo.addons.website.` is accepted; for any other path the module must start with one of `website`, `website_blog`, `user_websites.controllers.blog_urls`, `web`, `http_routing` or `edge_cache`. `None` gives `False`.

**See also:** test anchor `tenant_sites:COMM_test_tenant_module_allowed` in `tenant_sites/tests/`.

### `tenant_sites:COMM_route_module` [@ANCHOR: tenant_sites:COMM_route_module]

**Purpose:** finds which controller module would answer a path, without side effects. `IrHttp._tenant_route_module(path_info)` in `tenant_sites/models/ir_http.py`.

**Returns:** the dotted Python module name of the matching endpoint, or `None` when no route matches (`NotFound` or `MethodNotAllowed` are caught).

**Special concerns:** it asks the routing map directly because Odoo's own matching sets up language and frontend state as a side effect, which must happen only for a path that is really served. It sets `request.website_routing` first, as the website module does.

**See also:** test anchor `tenant_sites:COMM_test_route_module` in `tenant_sites/tests/`.

### `tenant_sites:COMM_refuse` [@ANCHOR: tenant_sites:COMM_refuse]

**Purpose:** refuses a tenant request the way Odoo answers an unknown page. `IrHttp._tenant_refuse()` in `tenant_sites/models/ir_http.py`.

**Behaviour:** first lets Odoo route a path that has no route (`/_tenant_sites_/no/route/here`), so the request gets the language and frontend state the website's 404 handling needs, then always raises `NotFound`. **Errors:** `NotFound`; it never returns. This is why a refused tenant request looks identical to a request for a page that does not exist.

**See also:** test anchor `tenant_sites:COMM_test_refuse` in `tenant_sites/tests/`.

### `tenant_sites:COMM_scoped_page_info` [@ANCHOR: tenant_sites:COMM_scoped_page_info]

**Purpose:** on a tenant website, a page belongs to that website or is not found. `WebsitePage._get_page_info(request)` in `tenant_sites/models/scoped_content.py`.

**Behaviour:** Odoo's lookup runs first. When the request is for a tenant and a page was found, a count as the service account checks that the page's `website_id` equals the tenant's website; a page with no website or another website's page gives `None`.

**Returns:** the page info dict, or `None`. Outside tenant requests the answer is unchanged. This keeps hams.com's generic pages off other sites.

**See also:** test anchor `tenant_sites:COMM_test_scoped_page_info` in `tenant_sites/tests/`.

### `tenant_sites:COMM_serve_redirect` [@ANCHOR: tenant_sites:COMM_serve_redirect]

**Purpose:** limits redirects on a tenant website to its own. `IrHttp._serve_redirect()` in `tenant_sites/models/ir_http.py`, an override that calls the parent first.

**Returns:** the parent's `website.rewrite` records filtered to those whose `website_id` equals the tenant's website; unchanged when the request is not a tenant request or nothing was found. A redirect shared by all websites (no website) is dropped on a tenant, so hams.com's redirects do not apply there.

**See also:** test anchor `tenant_sites:COMM_test_serve_redirect` in `tenant_sites/tests/`.

### `tenant_sites:COMM_scoped_search` [@ANCHOR: tenant_sites:COMM_scoped_search]

**Purpose:** limits blogs and posts to the tenant's website. `TenantSiteScopedMixin._search(domain, offset, limit, order, **kwargs)` in `tenant_sites/models/scoped_content.py`, mixed into `blog.blog` and `blog.post`.

**Behaviour:** when the request is for a tenant website, the domain is ANDed with `website_id = <tenant website>`, and the parent search runs. Otherwise unchanged. **Returns:** the usual search result.

**Concerns:** deliberately not applied to `website.page` or `website.menu`: a new website's menu points at shared pages, and hiding them from every search would break menu rendering; there such a link just answers 404.

**See also:** test anchor `tenant_sites:COMM_test_scoped_search` in `tenant_sites/tests/`.

### `tenant_sites:COMM_scoped_access` [@ANCHOR: tenant_sites:COMM_scoped_access]

**Purpose:** a record named in a URL on a tenant website must belong to it. `TenantSiteAccessMixin.can_access_from_current_website(website_id=False)` in `tenant_sites/models/scoped_content.py`, a `@public_rpc` method mixed into `website.page`, `website.rewrite`, `blog.blog` and `blog.post`.

**Returns:** on a tenant request, `True` only when every record's `website_id` equals the tenant's website (a record with no website is refused); otherwise Odoo's own answer. Without the override Odoo would accept a record with no website on every website.

**See also:** test anchor `tenant_sites:COMM_test_scoped_access` in `tenant_sites/tests/`.

### `tenant_sites:COMM_site_create` [@ANCHOR: tenant_sites:COMM_site_create]

**Purpose:** creating a tenant site gives its website the plain layout. `TenantSite.create(vals_list)` in `tenant_sites/models/tenant_site.py`.

**Behaviour:** creates the records, clears the `assets` registry cache and the general cache (the asset bundles of these websites are now built differently), then runs `_apply_public_layout()`. Returns the new records. `write` and `unlink` also clear the caches. A tenant site has a unique `website_id`; the website is protected with `ondelete="restrict"`. Needs the tenant-sites manager group.

**See also:** tested in `tenant_sites/tests/test_http.py` (the test cites this anchor).

### `tenant_sites:COMM_apply_public_layout` [@ANCHOR: tenant_sites:COMM_apply_public_layout]

**Purpose:** gives each tenant the plain Odoo layout. `TenantSite._apply_public_layout()` in `tenant_sites/models/tenant_site.py`.

**Behaviour:** as the service account and with the tenant's website in the context, it deactivates the "Sign in" link view (`portal.user_sign_in`) for that website only, then `_hide_foreign_layout_views()` deactivates the layout extensions of other authors' modules. Writing a view with a website in the context makes a website-specific copy, so the main site and other tenants keep theirs. Frontend scripts and styles are handled separately in `ir_asset.py`.

**Returns:** nothing. **Concerns:** repeatable; what is already hidden is left alone.

**See also:** tested in `tenant_sites/tests/test_http.py` (the test cites this anchor).

### `tenant_sites:COMM_foreign_modules` [@ANCHOR: tenant_sites:COMM_foreign_modules]

**Purpose:** the set of modules a tenant site must not inherit from. `TenantSite._foreign_module_names()` in `tenant_sites/models/tenant_site.py`.

**Returns:** a set of names of installed modules that are not stock Odoo (decided by `_is_stock_module`) and not listed in `tenant.site.kept.module` (modules whose layout and assets every site needs, such as a site-wide content security policy).

**Why not the manifest author:** Odoo S.A. writes its name in several ways, and a stock module wrongly counted as foreign was left out of a tenant's bundles, which stopped the stylesheet compiling. Reads up to 10000 module rows as the service account.

**See also:** tested in `tenant_sites/tests/test_http.py` (the test cites this anchor).

### `tenant_sites:COMM_is_stock_module` [@ANCHOR: tenant_sites:COMM_is_stock_module]

**Purpose:** decides whether a module ships with Odoo. `TenantSite._is_stock_module(name)` in `tenant_sites/models/tenant_site.py`.

| Argument | Type | Meaning |
|---|---|---|
| `name` | str | Technical module name. |

**Returns:** `True` when the module's directory is directly inside Odoo's base addons directory or its community addons directory (compared by real path), `False` otherwise or when the module has no path. The project's own modules live in other checkouts, so they are never stock.

**See also:** tested in `tenant_sites/tests/test_http.py` (the test cites this anchor).

### `tenant_sites:COMM_foreign_layout_views` [@ANCHOR: tenant_sites:COMM_foreign_layout_views]

**Purpose:** finds the views to hide on a tenant. `TenantSite._foreign_layout_views()` in `tenant_sites/models/tenant_site.py`.

**Returns:** a recordset (up to 100000) of active `ir.ui.view` rows that belong to foreign modules (through `ir.model.data`), have no website of their own (they apply to all websites) and inherit one of the layout views `website.layout`, `portal.frontend_layout`, `web.frontend_layout`, `web.layout` or `website.homepage`. `_hide_foreign_layout_views` deactivates each for the tenant's website only.

**See also:** tested in `tenant_sites/tests/test_http.py` (the test cites this anchor).

### `tenant_sites:COMM_is_tenant_website` [@ANCHOR: tenant_sites:COMM_is_tenant_website]

**Purpose:** answers "is this website a tenant?" `TenantSite._is_tenant_website(website_id)` in `tenant_sites/models/tenant_site.py`.

| Argument | Type | Meaning |
|---|---|---|
| `website_id` | int | A website id. |

**Returns:** `True` when an active `tenant.site` has that website. One count query as the service account. Used by the asset overrides, which run for the website the bundle is built for, not only during a tenant request.

**See also:** tested in `tenant_sites/tests/test_http.py` (the test cites this anchor).

### `tenant_sites:COMM_asset_addons` [@ANCHOR: tenant_sites:COMM_asset_addons]

**Purpose:** keeps other authors' scripts and styles out of a tenant's asset bundles. `IrAsset._get_active_addons_list(website_id=None, **params)` in `tenant_sites/models/ir_asset.py`.

**Returns:** Odoo's list of addons whose manifest assets apply, minus the foreign modules when `website_id` is a tenant website; unchanged for the main site and other websites. The result feeds the bundle cache, which `TenantSite` create, write and unlink clear.

**See also:** tested in `tenant_sites/tests/test_http.py` (the test cites this anchor).

### `tenant_sites:COMM_asset_related` [@ANCHOR: tenant_sites:COMM_asset_related]

**Purpose:** the same exclusion for `ir.asset` rows. `IrAsset._get_related_assets(domain, website_id=None, **params)` in `tenant_sites/models/ir_asset.py`.

**Returns:** the parent's assets; for a tenant website, rows are dropped when they apply to every website (`website_id` empty) and their path starts in a foreign module. Rows made for a specific website are kept. Unchanged for other websites.

**See also:** tested in `tenant_sites/tests/test_http.py` (the test cites this anchor).

### `tenant_sites:COMM_extra_kind` [@ANCHOR: tenant_sites:COMM_extra_kind]

**Purpose:** the hook through which another module adds a kind of hostname. `IrHttp._tenant_extra_kind(host)` in `tenant_sites/models/ir_http.py`.

| Argument | Type | Meaning |
|---|---|---|
| `host` | str | A normalized host name that is neither main nor tenant. |

**Returns:** the name of a kind (a string), or `None` (the default). Modules override it, call `super()` and return their kind when the host is theirs; parking returns `"parking"`. Called by the classifier, so it should be cheap.

**See also:** test anchor `tenant_sites:COMM_test_extra_kind` in `tenant_sites/tests/`.

### `tenant_sites:COMM_public_route` [@ANCHOR: tenant_sites:COMM_public_route]

**Purpose:** the hook through which a kind can have a real route. `IrHttp._tenant_public_route(kind, path, method)` in `tenant_sites/models/ir_http.py`.

| Argument | Type | Meaning |
|---|---|---|
| `kind` | str | The request's kind. |
| `path`, `method` | str | The request path and HTTP method. |

**Returns:** `False` by default; an extending module returns `True` for the few paths it serves (parking: the for-sale form post). Everything else on that kind is "not found".

**See also:** test anchor `tenant_sites:COMM_test_public_route` in `tenant_sites/tests/`.

### `parking:COMM_extra_kind` [@ANCHOR: parking:COMM_extra_kind]

**Purpose:** parking's answer to the extra-kind hook. `IrHttp._tenant_extra_kind(host)` in `parking/models/ir_http.py`.

**Returns:** `"parking"` when `parking.domain._lookup(host)` finds a record (exact name, or the bare domain for a `www.` name), as the parking service account; otherwise the parent's answer. This makes the router treat the host as a parked domain, answered from the table.

**See also:** `parking:COMM_test_extra_kind` in `parking/tests/`.

### `parking:COMM_public_route` [@ANCHOR: parking:COMM_public_route]

**Purpose:** parking's answer to the public-route hook. `IrHttp._tenant_public_route(kind, path, method)` in `parking/models/ir_http.py`.

**Returns:** `True` only for kind `parking`, path `/__parking/inquiry` and method `POST`, which is the for-sale contact form; otherwise the parent's answer. Every other route on a parked name stays "not found".

**See also:** `parking:COMM_test_public_route` in `parking/tests/`.

### `tenant_sites:COMM_serve_fallback` [@ANCHOR: tenant_sites:COMM_serve_fallback]

**Purpose:** the last stop for a request that matched no route. `IrHttp._serve_fallback()` in `tenant_sites/models/ir_http.py`.

**Behaviour:** makes sure the request has a `REQUEST_URI` (the website module reads it), classifies the request, then: a non-read-only method on a tenant gets a plain 404; `main` and `tenant` get Odoo's own fallback (pages, 404 page); any other kind goes to `_tenant_serve_other(kind)`.

**Returns:** a response object.

**See also:** test anchor `tenant_sites:COMM_test_serve_fallback` in `tenant_sites/tests/`.

### `tenant_sites:COMM_serve_other` [@ANCHOR: tenant_sites:COMM_serve_other]

**Purpose:** the hook that answers a request of a non-site kind. `IrHttp._tenant_serve_other(kind)` in `tenant_sites/models/ir_http.py`.

**Returns:** the default is a plain, uncached response: status 400 `bad host` for `reject`, otherwise 404 `unknown host`. Parking overrides it to answer parked domains from its table, and unknown hosts too when its policy is `default_page`.

**See also:** test anchor `tenant_sites:COMM_test_serve_other` in `tenant_sites/tests/`.

### `tenant_sites:COMM_plain_response` [@ANCHOR: tenant_sites:COMM_plain_response]

**Purpose:** builds the plain refusal. `IrHttp._tenant_plain_response(body, status)` in `tenant_sites/models/ir_http.py`.

| Argument | Type | Meaning |
|---|---|---|
| `body` | str | Text body. |
| `status` | int | HTTP status. |

**Returns:** a `text/plain; charset=utf-8` werkzeug response with `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Robots-Tag: noindex, nofollow` and an internal marker header `X-Tenant-Sites-Plain` that `_post_dispatch` uses to strip cookies.

**See also:** test anchor `tenant_sites:COMM_test_plain_response` in `tenant_sites/tests/`.

### `tenant_sites:COMM_post_dispatch` [@ANCHOR: tenant_sites:COMM_post_dispatch]

**Purpose:** guarantees a plain refusal carries no cookie. `IrHttp._post_dispatch(response)` in `tenant_sites/models/ir_http.py`.

**Behaviour:** calls the parent, then when the response has the marker header `X-Tenant-Sites-Plain` it empties `Set-Cookie` and removes the marker. Other responses are untouched. This matters because a cookie on an unknown host's 404 could be cached at the edge and replayed to other visitors.

**See also:** test anchor `tenant_sites:COMM_test_post_dispatch` in `tenant_sites/tests/`.

### `tenant_sites:COMM_ingress_problems` [@ANCHOR: tenant_sites:COMM_ingress_problems]

**Purpose:** a guard on Cloudflare tunnel pushes, extending `cloudflare.tunnel._ingress_problems(ingress)` in `tenant_sites/models/cloudflare_tunnel.py`.

| Argument | Type | Meaning |
|---|---|---|
| `ingress` | list of dict | The ingress rules about to be pushed; the last is the catch-all. |

**Returns:** a list of problem strings (empty means the push may go). It adds the tenant static-rule problems, and, when any tenant or parked hostname exists, a problem for every rule that has no hostname and does not go to the catch-all service, because such a rule (for example `^/websocket$` to a daemon port) would send tenant and parked domains to another service. The push action raises `UserError` with the problems.

**See also:** test anchor `tenant_sites:COMM_test_ingress_problems` in `tenant_sites/tests/`.

### `tenant_sites:COMM_tenant_hosts_exist` [@ANCHOR: tenant_sites:COMM_tenant_hosts_exist]

**Purpose:** "does this database serve any tenant hostname?" `tenant.site.host._tenant_hosts_exist()` in `tenant_sites/models/tenant_site_host.py`.

**Returns:** `True` when an active tenant site has a host row. Counts as the service account with `limit=1`. Parking extends it so parked domains count too. It is used by the tunnel guard.

**See also:** test anchor `tenant_sites:COMM_test_tenant_hosts_exist` in `tenant_sites/tests/`.

### `parking:COMM_tenant_hosts_exist` [@ANCHOR: parking:COMM_tenant_hosts_exist]

**Purpose:** parking's extension of the tenant-host check. `TenantSiteHost._tenant_hosts_exist()` in `parking/models/tenant_site_host.py`.

**Returns:** `True` if the parent finds a tenant host, otherwise `True` when the `parking.domain` table has any row (counted as the parking service account), else `False`. The tunnel guard then treats parked domains as tenant hostnames.

**See also:** `parking:COMM_test_tenant_hosts_exist` in `parking/tests/`.

### `tenant_sites:COMM_service_account_acl` [@ANCHOR: tenant_sites:COMM_service_account_acl]

**Purpose:** the access rights of the router's service account, in `tenant_sites/security/security_data.xml` and `security/ir.model.access.csv`. Group `tenant_sites.group_tenant_sites_service` (user `user_tenant_sites_service`) reads `tenant.site`, `tenant.site.host`, `tenant.site.own.host` and `tenant.site.kept.module`, and reads the Odoo models the router needs (`website`, `website.page`, `edge.routing.domain`, `ir.module.module`, `ir.model.data`, `ir.asset`, `res.groups`, users, companies and partners); it can also read, write and create `ir.ui.view` (to hide layout views per website). The manager group may edit the tenant tables. The anonymous user has no access to any of them.

**See also:** test anchor `tenant_sites:COMM_test_service_account_acl` in `tenant_sites/tests/`.

### `tenant_sites:COMM_post_init_hook` [@ANCHOR: tenant_sites:COMM_post_init_hook]

**Purpose:** the install hook, `post_init_hook(env)` in `tenant_sites/hooks.py`. It installs the module's documentation articles by running `ir.module.module._bootstrap_knowledge_docs()` as the `cloudflare.user_cloudflare_waf` service account. It seeds nothing else, because the module changes no behaviour until a tenant site or parked domain exists. **Returns:** nothing; a missing service account propagates as an error and fails the install.

**See also:** test anchor `tenant_sites:COMM_test_post_init_hook` in `tenant_sites/tests/`.
