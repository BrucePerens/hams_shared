# ADR 0110: A Self-Hosted Form Shield Instead of a Third-Party Captcha

## Status
Accepted (Bruce, 2026-10-14: "We need a captcha of some kind on that page", with the standing preference for no third-party tracking).

## Context

Content-violation reports on hams.com are filed by anonymous visitors through a public POST (`/website/report_violation`). Between
2026-09-23 and 2026-10-14 eight arrived from programs: five "Hi, I wanted to know your price" in five languages, one random string with a
malformed Gmail address, one probe with the text "Test". The honeypot field the form had shipped for some time was wired up on 2026-09-28
(#516) and the last two reports arrived after that, from programs that left it empty, so a honeypot alone is not enough. The only other
limit, added the same day, counted `request.remote_addr`, which behind the Cloudflare tunnel is always the loopback address: every visitor
shared one counter.

Odoo ships `google_recaptcha` (installed on hams1, unconfigured: no keys, so inert; `website` depends on it, so it cannot be uninstalled)
and `website_cf_turnstile`. Both make every visitor's browser load a script from a third party and tell that party who they are. The site
self-hosts its assets and its Content-Security-Policy allows no external script; Bruce prefers no third-party tracking.

## Decision

**1. A new hams_open module, `form_shield`, used by every public form that creates a record or sends mail.** Not `user_websites`: that
module pulls in blogs and the knowledge base, and `ham_init` (request an invite) must not depend on it.

**2. Proof of work, bound to the form, with a plain-text question as the fallback.** The server signs a challenge (HMAC-SHA256 over version,
kind, issue time, expiry, difficulty, nonce and a keyed hash of the visitor's /24 or /48). The browser finds the smallest counter such that
SHA-256 of `<challenge>:<form id>:<counter>` has `difficulty` leading zero bits, in a Web Worker served as a plain same-origin static file
(`worker-src`/`script-src` `'self'`; no `blob:`), with no interaction. The form id is in the hash, not the challenge, so one challenge fetched
when a page loads can be used for whichever form the visitor ends up sending. Default difficulty 19 bits. A browser without scripts or workers
is sent to a page with one arithmetic question in words, per visitor, readable by a screen reader. No image CAPTCHA.

**3. The challenge is fetched by script, by POST, with `Cache-Control: no-store`.** The pages that carry the forms are served from Cloudflare's
edge cache, so nothing per visitor can be rendered into them. The server enforces the minimum time-to-submit from the challenge's issue time
(3 s; 6 s for the question), not from page render.

**4. State is Redis only, every key expiring.** A used challenge's nonce is `SET NX EX`; rate limits are `INCR` counters per network (per user for a
member), per target and per form. Keys hold a keyed hash of the network prefix, never an address. If Redis is unreachable the verdict is
"unavailable" (HTTP 503): the shield is never skipped, which is the failure a spammer would wait for.

**5. Programs are answered like successes; people are told what to do.** A filled honeypot, a forged signature, a wrong solution or a replay
looks like success and stores nothing. An expired challenge, a changed network, a too-fast post, a wrong answer, or a rate limit tells the
visitor what happened (the last with 429).

**6. Members get the light check.** A signed-in member skips the proof of work (their pages are never edge-cached, and they are accountable)
but not the honeypots or the limits.

**7. Odoo's own forms are covered through Odoo's own seam.** Every stock public form asks `new ReCaptcha().getToken(action)` and the server calls
`ir.http._verify_request_recaptcha_token(action)`. `form_shield` patches the first so that the token is its own answer (no Google script
loads) and overrides the second for `website_form`, `website_event_registration`, `website_mass_mailing_subscribe` and `website_mail_follow`.
`login` is left out until Bruce says otherwise.

**8. Counters, not logs.** `form.shield.rejection` keeps one number per day, form and reason (including `accepted`), purged after 90 days. It
holds no address, email or text, so the privacy policy needs one sentence, not a new category.

## Consequences

- No visitor contacts a third party for spam protection. The Privacy Policy no longer needs to say that Cloudflare's bot check may be used on forms.
- A determined attacker can pay the proof of work (about a million hashes) per submission. The limits make that costly per network and per
  target; this raises the price of spam, it does not make it impossible. The counters show what is still getting through.
- A visitor on a very old phone waits a few seconds for the check while typing; it never blocks typing.
- Each new public form is three lines and a template, and gets a counter in the back office for free.
