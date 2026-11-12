# Story: Parking Many Domains in hams.com's Odoo

As a **System Administrator** who owns many domains, I want **the Odoo that already serves hams.com to answer for all
of them** (a parked page, a redirect, a for-sale page, or 410 Gone, the HTTP status for permanently removed, per domain), so that each domain
needs a DNS record and one row, not its own website, and so that nothing but those few pages is ever
reachable from the internet on those names.

## Scenario: A visitor opens a parked domain

The request's Host is read as the client sent it, never from `X-Forwarded-Host` (the header a proxy uses to pass on the original host name), which Odoo's proxy mode
would otherwise substitute (that would let a visitor make one domain answer as another and poison the
edge cache, Cloudflare's cache in front of the site) `tenant_sites:COMM_normalize_host` `tenant_sites:COMM_original_host`. The Host must be a plausible DNS name; IP addresses and
single labels are refused.

Every public request is answered from the `parking.domain` table by the `ir.http` fallback: the router
is told "no route" for everything except the for-sale form post, so the Odoo login, backend,
JSON-RPC, XML-RPC and websocket routes answer exactly like any other path
`tenant_sites:COMM_match_guard` `tenant_sites:COMM_serve_fallback` `parking:COMM_serve_other`.

A parked page is a few kilobytes of self-contained HTML with no script, no cookie and a content
security policy (a browser rule listing what a page may load) that allows none `parking:COMM_render_pages`. A host that is not in the table gets a
plain, non-cacheable 404.

## Scenario: A domain redirects

The target is a fixed absolute http(s) URL without credentials that does not point back at the domain
`parking:COMM_validate_redirect_url`. With "keep the path and query" the request path is appended to
the target's own path; the scheme and host always come from the target, so a crafted path such as
`//evil.example` cannot move the redirect to another host `parking:COMM_redirect_location`.

## Scenario: A domain is for sale

The page carries a contact form. The form posts to one fixed path and is protected by a stateless token bound to the host and to the time
the page was rendered (at least three seconds old, at most a day, so the page may sit in Cloudflare's
cache) `parking:COMM_form_token`. A filled honeypot (a hidden form field that people leave empty and bots fill), a bad token or a token for another host is
answered like a success and stores nothing; one address may send five inquiries an hour
`parking:COMM_inquiry_controller`.

## Scenario: The operator manages the domains

A request counts as a parked domain's only when its hostname is in the table, and a parked domain may not
shadow the main site's or a tenant's hostname (a tenant is another website of the same Odoo; see [tenant_sites.md](tenant_sites.md)) `parking:COMM_domain_not_main`
`parking:COMM_extra_kind`; the parked page answers every path, so the backend, login and API routes
are not reachable there `parking:COMM_public_route` `parking:COMM_serve_other`. Names are
normalized and unique, a redirect target must be valid, and the cache time is bounded
`parking:COMM_domain_constraints`. The public handler runs as one service account that can read the
domains and write inquiries and nothing else `parking:COMM_service_account_acl`.

## Scenario: How each piece behaves

* A page is assembled from escaped text only `parking:COMM_page`; a for-sale page replaces the
  form by a thank-you after a post `parking:COMM_render_for_sale`; a gone page is a plain 410
  `parking:COMM_render_gone`; `robots.txt` follows the domain's indexing switch
  `parking:COMM_robots_txt`.
* The visitor's address is trusted only with Cloudflare's own headers present
  `parking:COMM_client_ip` and is stored only as a keyed hash (a hash made with a secret key, so it cannot be guessed back to the address) `parking:COMM_hash_ip`.
* A form token is checked against the host, the render time and a keyed signature
  `parking:COMM_verify_form_token`; the key is set once at install and never overwritten
  `parking:COMM_post_init_hook` and is read by the handler or the request fails
  `parking:COMM_parking_secret`.
* A domain name is normalized when it is created `parking:COMM_domain_create`, renamed
  `parking:COMM_domain_write` or compared `parking:COMM_domain_normalize_name`; a lookup
  tries the exact name, then the bare domain for `www.` `parking:COMM_domain_lookup`; the
  inquiry count follows the inquiries `parking:COMM_domain_compute_inquiry_count`.
* The public handler gets its database access from one service account
  `parking:COMM_parking_service_env`, looks the host up `parking:COMM_parking_serve`,
  picks the behaviour `parking:COMM_parking_page`, builds every response without a cookie and
  with a cache policy `parking:COMM_parking_response`, and strips what Odoo adds afterwards
  `parking:COMM_post_dispatch`.
* An inquiry is limited per address and in total `parking:COMM_inquiry_rate_limited` and is
  answered like a success even when it was refused `parking:COMM_inquiry_done`.

## Anchor reference

Every anchor of the story has its own entry here (ADR 0113). "The parking service account" is `parking.user_parking_service`.

### `tenant_sites:COMM_normalize_host` [@ANCHOR: tenant_sites:COMM_normalize_host]

**Purpose:** the parking module's way of deciding that a Host header is a usable domain name. `normalize_host(raw)` in `tenant_sites/utils.py`, imported by `parking/utils.py` and used when a domain is saved, looked up and answered.

**Returns:** a lower-case punycode name without port or trailing dot, or `""` for an IP address, an IPv6 literal, a single label, bad characters or more than 253 characters. In parking, `""` means a 400 `bad host` response, and a stored domain name that normalizes to `""` is rejected with a `ValidationError`.

### `tenant_sites:COMM_original_host` [@ANCHOR: tenant_sites:COMM_original_host]

**Purpose:** gives parking the Host as the client sent it. `original_host(environ)` in `tenant_sites/utils.py`; parking passes its result to `normalize_host`.

**Returns:** `environ["werkzeug.proxy_fix.orig"]["HTTP_HOST"]`, else `HTTP_HOST`, else `""`. Odoo's proxy mode would otherwise replace the host with the client-supplied `X-Forwarded-Host`, so one parked domain could be made to answer as another and poison Cloudflare's cache. Parking uses it in `_parking_serve` and in the inquiry controller.

### `tenant_sites:COMM_match_guard` [@ANCHOR: tenant_sites:COMM_match_guard]

**Purpose:** the routing guard that makes a parked domain unreachable except for its one form post. `IrHttp._match(path_info)` in `tenant_sites/models/ir_http.py`.

**Behaviour:** for a parked kind it raises `NotFound` for every path unless `_tenant_public_route(kind, path, method)` is true, and parking's override is true only for `POST /__parking/inquiry`. The Odoo login, backend, JSON-RPC, XML-RPC, JSON-2 and websocket routes therefore answer exactly as any unknown path does. Returns Odoo's match or raises `NotFound`.

### `tenant_sites:COMM_serve_fallback` [@ANCHOR: tenant_sites:COMM_serve_fallback]

**Purpose:** the entry point that hands a parked-domain request to parking. `IrHttp._serve_fallback()` in `tenant_sites/models/ir_http.py`.

**Behaviour:** classifies the request; kinds `main` and `tenant` get Odoo's own fallback; any other kind (parked, unknown, reject) is passed to `_tenant_serve_other(kind)`, which parking overrides. **Returns:** a response. Every public request for a parked host ends here, because the router said "no route" for everything else.

### `parking:COMM_serve_other` [@ANCHOR: parking:COMM_serve_other]

**Purpose:** parking's answer for requests of a non-site kind. `IrHttp._tenant_serve_other(kind)` in `parking/models/ir_http.py`.

| Argument | Type | Meaning |
|---|---|---|
| `kind` | str | The kind chosen by the tenant_sites classifier. |

**Behaviour:** kind `parking` goes to `_parking_serve()` (the table lookup). Kind `unknown` goes to `_parking_serve()` only when the system parameter `parking.unknown_host_policy` is `default_page` (the default is `not_found`); otherwise, and for `reject`, the parent's plain uncached 400 or 404 is returned. The parameter is read as the parking service account.

**Returns:** a werkzeug response with no cookie. Also cited in the operator scenario, where it is the reason a parked page answers every path, and the backend, login and API routes are not reachable there [@ANCHOR: parking:COMM_serve_other]. The request has already been classified and checked by the routing guard before this runs, so this method only chooses between the parking table answer and the plain default refusal.

**See also:** test anchor `parking:COMM_test_serve_other` in `parking/tests/`.

### `parking:COMM_render_pages` [@ANCHOR: parking:COMM_render_pages]

**Purpose:** builds the parked-domain page. `render_parked(host, title="", message="", noindex=True)` in `parking/utils.py`, pure (it also builds the shared page shell `_page`).

| Argument | Type | Meaning |
|---|---|---|
| `host` | str | Domain name; used as the heading and title when no title is set. |
| `title`, `message` | str | Optional operator text; the message defaults to "This domain is registered and not currently in use." and every non-empty line becomes a paragraph. |
| `noindex` | bool | Adds `<meta name="robots" content="noindex,nofollow">`. |

**Returns:** a complete HTML document of a few kilobytes, with inline style only, no script and no cookie. All text is escaped with `html.escape`; the response headers add the content security policy that allows nothing.

**See also:** test anchor `parking:COMM_test_render_pages` in `parking/tests/`.

### `parking:COMM_validate_redirect_url` [@ANCHOR: parking:COMM_validate_redirect_url]

**Purpose:** decides whether an operator-entered redirect target is safe to store. `validate_redirect_url(url, own_host="")` in `parking/utils.py`, pure; used by the domain constraint.

| Argument | Type | Meaning |
|---|---|---|
| `url` | str | The target. |
| `own_host` | str | The domain being configured, to refuse a redirect to itself. |

**Returns:** `""` if acceptable, otherwise the reason: empty or over 2000 characters; spaces or control characters; not parseable; scheme not http or https; no host, a `@` (credentials) or a backslash; host not a valid domain name; same as `own_host`; invalid port.

**See also:** test anchor `parking:COMM_test_validate_redirect_url` in `parking/tests/`.

### `parking:COMM_redirect_location` [@ANCHOR: parking:COMM_redirect_location]

**Purpose:** builds the `Location` header of a redirect. `redirect_location(target, path="/", query="", preserve_path=False)` in `parking/utils.py`, pure.

**Behaviour:** without `preserve_path` the stored target is returned unchanged. With it, the request path (backslashes turned into slashes) and query are appended after the target's own path, percent-quoted with safe sets. The scheme and host always come from the stored target, so a crafted path such as `//evil.example` cannot move the redirect to another host. **Returns:** the URL string; no errors.

**See also:** test anchor `parking:COMM_test_redirect_location` in `parking/tests/`.

### `parking:COMM_form_token` [@ANCHOR: parking:COMM_form_token]

**Purpose:** makes the stateless token embedded in the for-sale form. `form_token(secret, host, now=None)` in `parking/utils.py`, pure.

| Argument | Type | Meaning |
|---|---|---|
| `secret` | str | The server secret `parking.form_secret`. |
| `host` | str | The normalized domain the page is for. |
| `now` | number or None | Time override for tests; default is the clock. |

**Returns:** `"<unix seconds>.<first 32 hex of HMAC-SHA256(secret, host|seconds)>"`. No state is stored, so the page can sit in Cloudflare's cache for up to a day and still post. Treat the secret as confidential.

**See also:** test anchor `parking:COMM_test_form_token` in `parking/tests/`.

### `parking:COMM_inquiry_controller` [@ANCHOR: parking:COMM_inquiry_controller]

**Purpose:** the single public POST of a parking instance, `ParkingController.inquiry(...)` in `parking/controllers/parking_api.py`, route `/__parking/inquiry` (public auth, CSRF off, no session saved).

| Field | Meaning |
|---|---|
| `token` | The form token. |
| `name`, `email`, `message` | Visitor data, cut to 120, 200 and 4000 characters. |
| `website` | The honeypot; any value means a bot. |

**Behaviour:** unknown host, a domain not in `for_sale` behaviour or a missing secret gives 404; a body over 8192 bytes gives 413. A filled honeypot or bad token gets the same success redirect and stores nothing. A missing `@` or empty message gives 400; over the rate limit gives 429. Otherwise it creates a `parking.inquiry` with a keyed hash of the address.

**Returns:** a 303 response to `/?sent=1`.

**See also:** test anchor `parking:COMM_test_inquiry_controller` in `parking/tests/`.

### `parking:COMM_domain_not_main` [@ANCHOR: parking:COMM_domain_not_main]

**Purpose:** keeps a parked domain from shadowing the main site or a tenant. `ParkingDomain._check_not_main_or_tenant()` in `parking/models/parking_domain.py`, a constraint on `name`.

**Behaviour:** for each record raises `ValidationError("%s belongs to the main site or a tenant website.")` if the name matches the main site's patterns (`_own_host_patterns`) or is a tenant host (`_website_id_for_host`). Both lookups run as the tenant service account. This is checked at save time, so the router never has to choose between two owners of one name.

**See also:** test anchor `parking:COMM_test_domain_not_main` in `parking/tests/`.

### `parking:COMM_extra_kind` [@ANCHOR: parking:COMM_extra_kind]

**Purpose:** parking's extension of the router's kind hook. `IrHttp._tenant_extra_kind(host)` in `parking/models/ir_http.py`.

**Returns:** `"parking"` when `parking.domain._lookup(host)` finds a record (exact name, or the bare domain for `www.` when `include_www` is set), run as the parking service account; otherwise the parent's answer. A host in the table is a parked domain; any other host is not.

**See also:** test anchor `parking:COMM_test_extra_kind` in `parking/tests/`.

### `parking:COMM_public_route` [@ANCHOR: parking:COMM_public_route]

**Purpose:** parking's extension of the public-route hook. `IrHttp._tenant_public_route(kind, path, method)` in `parking/models/ir_http.py`.

**Returns:** `True` only for kind `parking`, path `/__parking/inquiry`, method `POST`; else the parent's answer (`False`). Everything else on a parked host, including the backend, login and API routes, is not a route, and the parked page answers every other path through the fallback.

**See also:** test anchor `parking:COMM_test_public_route` in `parking/tests/`.

### `parking:COMM_domain_constraints` [@ANCHOR: parking:COMM_domain_constraints]

**Purpose:** validates a parked domain's settings. `ParkingDomain._check_behavior()` in `parking/models/parking_domain.py`, a constraint on `behavior`, `redirect_url`, `name`, `cache_ttl` and `include_www`.

**Rules:** for behaviour `redirect` the target must pass `validate_redirect_url`, against the domain itself and, with `include_www`, also against `www.<domain>`; the error is `ValidationError("Invalid redirect target for <name>: <reason>")`. `cache_ttl` must be between 0 and 31536000 seconds. Names are normalized and unique through `_normalize_name` and a unique constraint.

**See also:** test anchor `parking:COMM_test_domain_constraints` in `parking/tests/`.

### `parking:COMM_service_account_acl` [@ANCHOR: parking:COMM_service_account_acl]

**Purpose:** the access rights of the public handler, in `parking/security/parking_security.xml` and `ir.model.access.csv`. The handler runs as the service user `parking_service_internal` (group `parking.group_parking_service`, `is_service_account` true).

**Rights:** it can read `parking.domain` and create `parking.inquiry`, nothing else; the anonymous public user has no access to any parking model, and a manager group edits domains. Notes on a domain are visible only to managers (`groups` on the field). **Security:** this is why a bug in a public page cannot read or change anything beyond domains and inquiries.

**See also:** test anchor `parking:COMM_test_service_account_acl` in `parking/tests/`.

### `parking:COMM_page` [@ANCHOR: parking:COMM_page]

**Purpose:** the HTML shell for every parking page. `_page(title, body, noindex, lang="en")` in `parking/utils.py`, pure.

| Argument | Type | Meaning |
|---|---|---|
| `title` | str | Escaped into `<title>`. |
| `body` | str | Already-built, already-escaped HTML for the body. |
| `noindex` | bool | Adds a robots meta tag. |
| `lang` | str | `lang` attribute, escaped. |

**Returns:** a full document with a viewport meta and a small inline stylesheet (with a dark-mode rule). Nothing in it is a script or an external resource; text reaches it only through `html.escape`.

**See also:** test anchor `parking:COMM_test_page` in `parking/tests/`.

### `parking:COMM_render_for_sale` [@ANCHOR: parking:COMM_render_for_sale]

**Purpose:** builds the for-sale page. `render_for_sale(host, token, title="", message="", price_text="", sent=False, noindex=True)` in `parking/utils.py`, pure.

**Behaviour:** heading and message default to "<host> is for sale" and "Interested in this domain? Send an inquiry below."; a price line is shown when `price_text` is set. If `sent` is true the form is replaced by a thank-you; otherwise a POST form to `/__parking/inquiry` holds the hidden `token`, name, email and message fields and a hidden honeypot input named `website`. **Returns:** HTML; all text escaped.

**See also:** test anchor `parking:COMM_test_render_for_sale` in `parking/tests/`.

### `parking:COMM_render_gone` [@ANCHOR: parking:COMM_render_gone]

**Purpose:** the page for a domain whose behaviour is `gone`. `render_gone(host)` in `parking/utils.py`, pure. It returns a `noindex` document titled with the host and a heading "410 Gone" and the text "This address no longer exists." The caller (`_parking_page`) sends it with HTTP status 410, the status for permanently removed. It takes only the host, which is escaped through the page shell, and has no side effects.

**See also:** test anchor `parking:COMM_test_render_gone` in `parking/tests/`.

### `parking:COMM_robots_txt` [@ANCHOR: parking:COMM_robots_txt]

**Purpose:** answers `/robots.txt` for a parked domain. `robots_txt(noindex)` in `parking/utils.py`, pure.

**Returns:** `"User-agent: *\nDisallow: /\n"` when `noindex` is true (the default), else `"User-agent: *\nAllow: /\n"`. `noindex` comes from the domain's "Ask search engines not to index" switch; the same switch adds the `X-Robots-Tag` header and the robots meta tag. Served with status 200 as `text/plain`.

**See also:** test anchor `parking:COMM_test_robots_txt` in `parking/tests/`.

### `parking:COMM_client_ip` [@ANCHOR: parking:COMM_client_ip]

**Purpose:** the visitor's address for rate limiting. `client_ip(environ, headers)` in `parking/utils.py`, pure.

**Returns:** `CF-Connecting-IP` (stripped, cut to 64 characters) only when `CF-Ray` is also present, since every request that came through Cloudflare carries both and the listener is reachable only from cloudflared and loopback; otherwise `REMOTE_ADDR` cut to 64, or `""`. **Security:** a direct request cannot claim another address without also forging `CF-Ray`, and it is the only information about the address that is ever kept, as a keyed hash.

**See also:** test anchor `parking:COMM_test_client_ip` in `parking/tests/`.

### `parking:COMM_hash_ip` [@ANCHOR: parking:COMM_hash_ip]

**Purpose:** stores a visitor's address without keeping it. `hash_ip(secret, ip)` in `parking/utils.py`, pure.

**Returns:** the first 32 hex characters of HMAC-SHA256 keyed with `secret` over the address. The key means the hash cannot be guessed back to an address by trying all addresses. The value goes into `parking.inquiry.ip_hash`, used only to count inquiries per address per hour. No errors; an empty address still hashes.

**See also:** test anchor `parking:COMM_test_hash_ip` in `parking/tests/`.

### `parking:COMM_verify_form_token` [@ANCHOR: parking:COMM_verify_form_token]

**Purpose:** checks the for-sale form token. `verify_form_token(secret, host, token, now=None)` in `parking/utils.py`, pure.

**Returns:** `True` only when the token parses as `<seconds>.<mac>`, its age is between `TOKEN_MIN_AGE` (3 seconds, so an instant bot post fails) and `TOKEN_MAX_AGE` (86400, one day), and the mac equals HMAC-SHA256 over `host|seconds` compared with `hmac.compare_digest` (constant time). Anything malformed gives `False`; it never raises. A token made for another host fails.

**See also:** test anchor `parking:COMM_test_verify_form_token` in `parking/tests/`.

### `parking:COMM_post_init_hook` [@ANCHOR: parking:COMM_post_init_hook]

**Purpose:** seeds the settings the public handler needs, on install. `post_init_hook(env)` in `parking/hooks.py`.

**Behaviour:** sets `parking.form_secret` to a new 64-hex-character `secrets.token_hex(32)` only when unset (an existing secret is never overwritten, which would invalidate cached pages), sets `parking.unknown_host_policy` to `not_found` only when unset, then installs the knowledge articles as the `cloudflare.user_cloudflare_waf` service account. **Returns:** nothing. **Security:** do not copy or log the secret.

**See also:** test anchor `parking:COMM_test_post_init_hook` in `parking/tests/`.

### `parking:COMM_parking_secret` [@ANCHOR: parking:COMM_parking_secret]

**Purpose:** reads the form secret for the handler. `IrHttp._parking_secret(env)` in `parking/models/ir_http.py`.

| Argument | Type | Meaning |
|---|---|---|
| `env` | Environment | The parking service account's environment. |

**Returns:** the value of `parking.form_secret`. **Errors:** `RuntimeError("parking.form_secret is not set; reinstall the parking module")` when empty, so the for-sale page fails loudly rather than issuing tokens signed with an empty key.

**See also:** test anchor `parking:COMM_test_parking_secret` in `parking/tests/`.

### `parking:COMM_domain_create` [@ANCHOR: parking:COMM_domain_create]

**Purpose:** `ParkingDomain.create(vals_list)` in `parking/models/parking_domain.py`, `@api.model_create_multi`. It runs `_normalize_name` on every dict (punycode, lower case) and then creates the records, so a domain can be entered as `Example.COM` or in Unicode. **Returns** the new records. **Errors:** `ValidationError` for a name that is not a valid domain, and the unique-name constraint for a duplicate. The constraints on behaviour and shadowing run after creation.

**See also:** test anchor `parking:COMM_test_domain_create` in `parking/tests/`.

### `parking:COMM_domain_write` [@ANCHOR: parking:COMM_domain_write]

**Purpose:** `ParkingDomain.write(vals)` in `parking/models/parking_domain.py`. It normalizes `name` in `vals` through `_normalize_name` before calling the parent, so a rename is stored in the same form as a new name. **Returns** the parent's boolean. **Errors:** `ValidationError` for an invalid name or a duplicate; the shadowing and behaviour constraints run again after the write.

**See also:** test anchor `parking:COMM_test_domain_write` in `parking/tests/`.

### `parking:COMM_domain_normalize_name` [@ANCHOR: parking:COMM_domain_normalize_name]

**Purpose:** the one normalizer for parking domain names. `ParkingDomain._normalize_name(vals)` in `parking/models/parking_domain.py`.

| Argument | Type | Meaning |
|---|---|---|
| `vals` | dict | Values being written; `name` is replaced in place. |

**Behaviour:** does nothing without `name`; otherwise stores `normalize_host(name)`. **Errors:** `ValidationError("'%s' is not a valid domain name.")` for an IP address, one-word name or bad characters. Called from `create` and `write`; lookups use the same function on the request side.

**See also:** test anchor `parking:COMM_test_domain_normalize_name` in `parking/tests/`.

### `parking:COMM_domain_lookup` [@ANCHOR: parking:COMM_domain_lookup]

**Purpose:** finds the parking record for a host. `ParkingDomain._lookup(host)` in `parking/models/parking_domain.py`.

| Argument | Type | Meaning |
|---|---|---|
| `host` | str | An already normalized host name. |

**Returns:** the record with that exact name; if none and the host starts with `www.`, the record for the bare domain, but only when its `include_www` is set; otherwise an empty recordset. Active records only (the `active` field). One or two indexed searches, run by the caller as the parking service account.

**See also:** test anchor `parking:COMM_test_domain_lookup` in `parking/tests/`.

### `parking:COMM_domain_compute_inquiry_count` [@ANCHOR: parking:COMM_domain_compute_inquiry_count]

**Purpose:** the computed field `inquiry_count` of `parking.domain`, `_compute_inquiry_count()` in `parking/models/parking_domain.py`, `@api.depends("inquiry_ids")`.

**Behaviour:** sets the count to the number of `inquiry_ids` of each domain, so it follows inquiries as they are created or deleted. Returns nothing; not stored. It is shown on the domain form so the operator can see which for-sale domains receive interest.

**See also:** test anchor `parking:COMM_test_domain_compute_inquiry_count` in `parking/tests/`.

### `parking:COMM_parking_service_env` [@ANCHOR: parking:COMM_parking_service_env]

**Purpose:** the environment for all public parking work. `IrHttp._parking_service_env()` in `parking/models/ir_http.py`.

**Returns:** `request.env(user=<id of parking.user_parking_service>)`. **Errors:** the xml id lookup raises if the service user is missing, so a broken install fails instead of falling back to the public user. The anonymous visitor has no access to parking tables; this account can read domains and create inquiries only.

**See also:** test anchor `parking:COMM_test_parking_service_env` in `parking/tests/`.

### `parking:COMM_parking_serve` [@ANCHOR: parking:COMM_parking_serve]

**Purpose:** the main public handler. `IrHttp._parking_serve()` in `parking/models/ir_http.py`.

**Behaviour:** a method other than GET, HEAD or POST gives 405; an invalid host gives 400. It looks the host up as the service account. No record gives 404, or the default page when `parking.unknown_host_policy` is `default_page`. A POST to a known host (outside the inquiry route) gives 405. `/robots.txt` and `/favicon.ico` get a `robots` answer and an empty 204; everything else goes to `_parking_page`.

**Returns:** a response built by `_parking_response`.

**See also:** test anchor `parking:COMM_test_parking_serve` in `parking/tests/`.

### `parking:COMM_parking_page` [@ANCHOR: parking:COMM_parking_page]

**Purpose:** picks the behaviour of a parked domain. `IrHttp._parking_page(host, record, env)` in `parking/models/ir_http.py`.

| Argument | Type | Meaning |
|---|---|---|
| `host` | str | The normalized host. |
| `record` | `parking.domain` or None | `None` means the default page for an unknown host. |
| `env` | Environment | The parking service environment. |

**Behaviour:** `gone` gives 410; `redirect` gives 301 or 302 with `Location` from `redirect_location` (the request path and query when "Keep the path and query" is on); `for_sale` gives the sale page with a fresh form token (the `?sent=1` flag shows the thank-you); anything else gives the parked page.

**See also:** test anchor `parking:COMM_test_parking_page` in `parking/tests/`.

### `parking:COMM_parking_response` [@ANCHOR: parking:COMM_parking_response]

**Purpose:** builds every parking response. `IrHttp._parking_response(body, status, content_type, record)` in `parking/models/ir_http.py`.

**Returns:** a werkzeug response with `nosniff`, `Referrer-Policy: no-referrer` and a content security policy that allows no script (`default-src 'none'`, inline style, form action to itself only, no framing); an internal marker header for the cookie strip. Cache policy: `no-store` for no record or an error status other than 410; otherwise `public, max-age=min(ttl,300), s-maxage=ttl` with the record's `cache_ttl` (halved to at most half a day for for-sale pages so the form token stays valid). `X-Robots-Tag: noindex` when the domain asks for it.

**See also:** test anchor `parking:COMM_test_parking_response` in `parking/tests/`.

### `parking:COMM_post_dispatch` [@ANCHOR: parking:COMM_post_dispatch]

**Purpose:** removes anything Odoo adds to a parking response. `IrHttp._post_dispatch(response)` in `parking/models/ir_http.py`.

**Behaviour:** calls the parent, then when the response carries the internal marker `X-Parking-Response` it empties `Set-Cookie` and removes the marker. A cookie on a cached parking page would be replayed to every visitor, so none may escape. Other responses are untouched. Returns nothing.

**See also:** test anchor `parking:COMM_test_post_dispatch` in `parking/tests/`.

### `parking:COMM_inquiry_rate_limited` [@ANCHOR: parking:COMM_inquiry_rate_limited]

**Purpose:** limits for-sale inquiries. `ParkingController._rate_limited(env, ip_hash)` in `parking/controllers/parking_api.py`.

| Argument | Type | Meaning |
|---|---|---|
| `env` | Environment | The parking service environment. |
| `ip_hash` | str | The keyed hash of the visitor's address. |

**Returns:** `True` when that hash has made 5 or more inquiries in the last hour (`INQUIRY_PER_IP_HOUR`), or all visitors together have made 200 or more (`INQUIRY_GLOBAL_HOUR`). Two count queries, no locks, so two simultaneous posts can each pass at the limit boundary.

**See also:** test anchor `parking:COMM_test_inquiry_rate_limited` in `parking/tests/`.

### `parking:COMM_inquiry_done` [@ANCHOR: parking:COMM_inquiry_done]

**Purpose:** the answer to every accepted or silently dropped inquiry. `ParkingController._done(ir_http, record)` in `parking/controllers/parking_api.py`.

**Returns:** a 303 response with `Location: /?sent=1` built through `_parking_response`, with no cookie. A filled honeypot, a bad token or a token for another host reaches the same answer as success and stores nothing, so a bot cannot tell whether it was accepted. The `record` argument is not otherwise used.

**See also:** test anchor `parking:COMM_test_inquiry_done` in `parking/tests/`.
