# MASTER 03: Edge Routing & Threat Mitigation

## Status
Accepted (Consolidates ADRs 0037, 0038, 0040, 0041, 0042)

## Context & Philosophy
To protect the synchronous Odoo WSGI web workers from CPU/Memory exhaustion and DDoS attacks, malicious traffic and heavy read loads must be stopped at the network edge (Cloudflare and Nginx) before they ever reach the Python application layer.

## Decisions & Mandates

### 1. Cloudflare Edge Orchestration & Verified Bots
The `cloudflare` module acts as the control plane for the CDN edge.
* **Proactive Caching:** Semi-static routes (Blogs, Classifieds) receive `Cloudflare-CDN-Cache-Control` headers for 24-hour caching. The ORM intercepts edits and pushes invalidations to a background queue for batched cache purging.
* **Verified Bot Allowance:** WAF rules explicitly evaluate `cf.client.bot`. Verified crawlers (Google, Bing) are allowed, while unknown headless scrapers are issued a `managed_challenge` (Turnstile) to verify humanity.

### 2. Edge-Enforced Rate Limiting
Application-level rate limiting inside Python is forbidden. All API endpoint throttling MUST be configured at the Nginx edge using `limit_req_zone` directives.

### 3. Silent Honeypots & Dynamic Tarpitting
Unauthenticated public forms MUST implement visually hidden honeypot `<input>` fields.
* If a bot fills the field, the Python controller immediately returns a fake 200 OK success.
* The controller extracts the bot's IP and pushes it to Redis with a 1-hour TTL.
* An Nginx sidecar daemon pulls these IPs and dynamically generates a `geo` block, applying extreme bandwidth limits (`limit_rate`) to effectively tarpit the malicious actors at the C-level edge.
