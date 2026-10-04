# Story: Parking Many Domains in hams.com's Odoo

As a **System Administrator** who owns many domains, I want **the Odoo that already serves hams.com to answer for all
of them** (a parked page, a redirect, a for-sale page, or 410 Gone, per domain), so that each domain
needs a DNS record and one row, not its own website, and so that nothing but those few pages is ever
reachable from the internet on those names.

## Scenario: A visitor opens a parked domain

The request's Host is read as the client sent it, never from `X-Forwarded-Host`, which Odoo's proxy mode
would otherwise substitute (that would let a visitor make one domain answer as another and poison the
edge cache) [@ANCHOR: tenant_sites:COMM_normalize_host] [@ANCHOR: tenant_sites:COMM_original_host]. The Host must be a plausible DNS name; IP addresses and
single labels are refused.

Every public request is answered from the `parking.domain` table by the `ir.http` fallback: the router
is told "no route" for everything except the for-sale form post, so the Odoo login, backend,
JSON-RPC, XML-RPC and websocket routes answer exactly like any other path
[@ANCHOR: tenant_sites:COMM_match_guard] [@ANCHOR: tenant_sites:COMM_serve_fallback] [@ANCHOR: parking:COMM_serve_other].

A parked page is a few kilobytes of self-contained HTML with no script, no cookie and a content
security policy that allows none [@ANCHOR: parking:COMM_render_pages]. A host that is not in the table gets a
plain, non-cacheable 404.

## Scenario: A domain redirects

The target is a fixed absolute http(s) URL without credentials that does not point back at the domain
[@ANCHOR: parking:COMM_validate_redirect_url]. With "keep the path and query" the request path is appended to
the target's own path; the scheme and host always come from the target, so a crafted path such as
`//evil.example` cannot move the redirect to another host [@ANCHOR: parking:COMM_redirect_location].

## Scenario: A domain is for sale

The page carries a contact form. The form posts to one fixed path and is protected by a stateless token bound to the host and to the time
the page was rendered (at least three seconds old, at most a day, so the page may sit in Cloudflare's
cache) [@ANCHOR: parking:COMM_form_token]. A filled honeypot, a bad token or a token for another host is
answered like a success and stores nothing; one address may send five inquiries an hour
[@ANCHOR: parking:COMM_inquiry_controller].

## Scenario: The operator manages the domains

A request counts as a parked domain's only when its hostname is in the table, and a parked domain may not
shadow the main site's or a tenant's hostname [@ANCHOR: parking:COMM_domain_not_main]
[@ANCHOR: parking:COMM_extra_kind]; the parked page answers every path, so the backend, login and API routes
are not reachable there [@ANCHOR: parking:COMM_public_route] [@ANCHOR: parking:COMM_serve_other]. Names are
normalized and unique, a redirect target must be valid, and the cache time is bounded
[@ANCHOR: parking:COMM_domain_constraints]. The public handler runs as one service account that can read the
domains and write inquiries and nothing else [@ANCHOR: parking:COMM_service_account_acl].

## Scenario: How each piece behaves

* A page is assembled from escaped text only [@ANCHOR: parking:COMM_page]; a for-sale page replaces the
  form by a thank-you after a post [@ANCHOR: parking:COMM_render_for_sale]; a gone page is a plain 410
  [@ANCHOR: parking:COMM_render_gone]; `robots.txt` follows the domain's indexing switch
  [@ANCHOR: parking:COMM_robots_txt].
* The visitor's address is trusted only with Cloudflare's own headers present
  [@ANCHOR: parking:COMM_client_ip] and is stored only as a keyed hash [@ANCHOR: parking:COMM_hash_ip].
* A form token is checked against the host, the render time and a keyed signature
  [@ANCHOR: parking:COMM_verify_form_token]; the key is set once at install and never overwritten
  [@ANCHOR: parking:COMM_post_init_hook] and is read by the handler or the request fails
  [@ANCHOR: parking:COMM_parking_secret].
* A domain name is normalized when it is created [@ANCHOR: parking:COMM_domain_create], renamed
  [@ANCHOR: parking:COMM_domain_write] or compared [@ANCHOR: parking:COMM_domain_normalize_name]; a lookup
  tries the exact name, then the bare domain for `www.` [@ANCHOR: parking:COMM_domain_lookup]; the
  inquiry count follows the inquiries [@ANCHOR: parking:COMM_domain_compute_inquiry_count].
* The public handler gets its database access from one service account
  [@ANCHOR: parking:COMM_parking_service_env], looks the host up [@ANCHOR: parking:COMM_parking_serve],
  picks the behaviour [@ANCHOR: parking:COMM_parking_page], builds every response without a cookie and
  with a cache policy [@ANCHOR: parking:COMM_parking_response], and strips what Odoo adds afterwards
  [@ANCHOR: parking:COMM_post_dispatch].
* An inquiry is limited per address and in total [@ANCHOR: parking:COMM_inquiry_rate_limited] and is
  answered like a success even when it was refused [@ANCHOR: parking:COMM_inquiry_done].
