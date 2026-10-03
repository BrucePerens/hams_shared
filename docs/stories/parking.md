# Story: Parking Many Domains on One Lightweight Odoo

As a **System Administrator** who owns many domains, I want **one small Odoo instance to answer for all
of them** (a parked page, a redirect, a for-sale page, or 410 Gone, per domain), so that each domain
needs a DNS record and one row, not its own website, and so that nothing but those few pages is ever
reachable from the internet on those names.

## Scenario: A visitor opens a parked domain

The request's Host is read as the client sent it, never from `X-Forwarded-Host`, which Odoo's proxy mode
would otherwise substitute (that would let a visitor make one domain answer as another and poison the
edge cache) [@ANCHOR: parking:normalize_host]. The Host must be a plausible DNS name; IP addresses and
single labels are refused.

Every public request is answered from the `parking.domain` table by the `ir.http` fallback: the router
is told "no route" for everything except the for-sale form post, so the Odoo login, backend,
JSON-RPC, XML-RPC and websocket routes answer exactly like any other path
[@ANCHOR: parking:ir_http_match_guard] [@ANCHOR: parking:serve_fallback].

A parked page is a few kilobytes of self-contained HTML with no script, no cookie and a content
security policy that allows none [@ANCHOR: parking:render_parked]. A host that is not in the table gets a
plain, non-cacheable 404.

## Scenario: A domain redirects

The target is a fixed absolute http(s) URL without credentials that does not point back at the domain
[@ANCHOR: parking:validate_redirect_url]. With "keep the path and query" the request path is appended to
the target's own path; the scheme and host always come from the target, so a crafted path such as
`//evil.example` cannot move the redirect to another host [@ANCHOR: parking:redirect_location].

## Scenario: A domain is for sale

The page carries a contact form [@ANCHOR: parking:render_for_sale]. The form posts to one fixed path
[@ANCHOR: parking:inquiry_path] and is protected by a stateless token bound to the host and to the time
the page was rendered (at least three seconds old, at most a day, so the page may sit in Cloudflare's
cache) [@ANCHOR: parking:form_token]. A filled honeypot, a bad token or a token for another host is
answered like a success and stores nothing; one address may send five inquiries an hour
[@ANCHOR: parking:inquiry_controller].

## Scenario: The operator manages the domains

Only a request from a loopback host (or one named in `parking.admin_hosts`) that carries no
Cloudflare header reaches the backend, so the instance is administered over an SSH tunnel and a visitor
cannot reach it by sending `Host: localhost` to a parked domain. Names are normalized and unique, a
redirect target must be valid, and the cache time is bounded [@ANCHOR: parking:model_constraints]. The
public handler runs as one service account that can read the domains and write inquiries and nothing
else [@ANCHOR: parking:service_account_acl].
