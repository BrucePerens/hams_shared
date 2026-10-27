# Pager Duty: Keeping the Certificate Check Up to Date

An operator who adds or removes a custom domain needs the Let's Encrypt (free certificate authority) readiness check to follow. The `pager_duty` module accepts the new list of domains over a JSON-RPC (remote procedure call) route instead of requiring someone to edit the check by hand.

## The Flow

1. A trusted program (for example a deployment script) POSTs `domains` (a list of domain names) and `api_identity` to `/api/v1/pager_duty/update_domains`.
   [@ANCHOR: pager_duty:update_domains]
2. The route is public, so its only gate is `api_identity`: it must equal the `pager_duty.domain_api_identity` system parameter. A request without `domains` is answered with an "Empty payload" error.
3. A wrong or missing identity is answered "Unauthorized" and counted against the caller's IP address. After 10 failed attempts within 60 seconds, further attempts from that address are refused with "Too many attempts" until the window ends. Correct requests do not count. If Redis is unavailable the counting is skipped.
4. With a valid identity, the work is done by the module's own service account: the target of the first `certbot` (Certbot Readiness) check is set to the comma-joined list of domains, the check is created if none exists, and the daemon's JSON configuration file is rewritten.
