#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cloudflare ingress and DNS planning for Odoo tenants (ADR 0105, MULTI_TENANT_ODOO.md section 6).

Two kinds of use:

1. `plan` is pure and offline. Given the tenant specs and (optionally) the tunnel's CURRENT remote
   configuration saved as JSON, it prints the exact ingress list that should replace it and the DNS
   records to create. It changes nothing and contacts nothing.

2. `fetch-current`, `apply-ingress`, `plan-dns`, `apply-dns` talk to the Cloudflare API with the
   token in ~/.secrets/cloudflare_hams_com.env (CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID), read by
   this script and never printed. They are reviewed-script operations for Bruce to approve:
   every mutating command is dry-run unless `--apply` is given, and `apply-ingress` additionally
   requires `--approved-sha256` equal to the digest `plan` printed for the exact list, and refuses
   when the live configuration version differs from the file the plan was made from. Nothing in
   this file pushes hams_prod's `cloudflare.tunnel.route` rows: the tunnel's rules are edited here,
   as a reviewed whole-list replacement derived from the live list, never from Odoo's rows.

Why the existing rules have to be re-scoped (docs/proposals/MULTI_TENANT_ODOO.md): the live rules
for /websocket, /ws/firehose, /relay_bridge, /ws, /adif and /gdpr_export have no hostname, so they
match on EVERY hostname. Once another zone's hostname reaches the tunnel, `https://perens.com/ws`
would be served by hams_simulated_band. Each such rule is therefore duplicated for `hams.com` and
`*.hams.com`, and the final catch-all (hams_prod's Odoo on 8069) becomes explicit
`hams.com`/`*.hams.com` rules so the new catch-all can go to the parking instance.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tenant_lib as lib  # noqa: E402

HAMS_HOSTS = ("hams.com", "*.hams.com")
ENV_FILE = os.path.expanduser("~/.secrets/cloudflare_hams_com.env")
API = "https://api.cloudflare.com/client/v4"
USER_AGENT = "HamsComTenantTool/1.0 (+https://crawler.hams.com)"


def service_for(spec):
    return f"http://localhost:{spec['http_port']}"


def _is_catch_all(rule):
    return "hostname" not in rule and "path" not in rule


# [@ANCHOR: tenant_cloudflare:build_ingress]
def build_ingress(specs, current):
    """Returns the new ingress list derived from `current` (a list of rules, last one the catch-all).

    Idempotent: building from its own output returns the same list."""
    tenant_hosts = {domain for spec in specs for domain in spec["domains"]}
    if not current:
        raise ValueError("the current ingress list is required to derive a safe replacement")
    if not _is_catch_all(current[-1]):
        raise ValueError("the current list does not end with a catch-all rule; refusing to guess")
    hams_service = None
    body = []
    for rule in current[:-1]:
        if rule.get("hostname") in tenant_hosts:
            continue  # rebuilt below
        body.append(rule)
    final = current[-1]
    scoped = []
    for rule in body:
        if "hostname" in rule or "path" not in rule:
            scoped.append(rule)
            continue
        for host in HAMS_HOSTS:
            copy = dict(rule)
            copy["hostname"] = host
            scoped.append(copy)
    # Keep one rule per (hostname, path): scoping an already scoped list must not duplicate.
    unique, seen = [], set()
    for rule in scoped:
        key = (rule.get("hostname"), rule.get("path"), rule.get("service"))
        if key not in seen:
            seen.add(key)
            unique.append(rule)
    # hams_prod's own Odoo: the current catch-all, or the explicit hams.com rule from an earlier run.
    for rule in unique:
        if rule.get("hostname") == "hams.com" and "path" not in rule:
            hams_service = rule["service"]
    if hams_service is None:
        hams_service = final["service"]
    explicit = [{"hostname": host, "service": hams_service} for host in HAMS_HOSTS]
    present = {(r.get("hostname"), r.get("path")) for r in unique}
    for rule in explicit:
        if (rule["hostname"], None) not in present:
            unique.append(rule)
    parking = next((s for s in specs if s["catch_all"]), None)
    catch_all = {"service": service_for(parking) if parking else "http_status:404"}
    tenants = []
    for spec in sorted(specs, key=lambda s: s["name"]):
        if spec["catch_all"]:
            continue  # parked hostnames arrive through the catch-all, not by name
        for domain in spec["domains"]:
            tenants.append({"hostname": domain, "service": service_for(spec)})
    return tenants + unique + [catch_all]


def ingress_digest(ingress):
    return hashlib.sha256(json.dumps(ingress, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def render_dns(specs, tunnel_id):
    target = f"{tunnel_id or '<TUNNEL_ID>'}.cfargotunnel.com"
    records = []
    for spec in sorted(specs, key=lambda s: s["name"]):
        if spec["catch_all"]:
            continue  # parked domains are added by parking_ctl, one zone at a time
        for domain in spec["domains"]:
            records.append({"type": "CNAME", "name": domain, "content": target, "proxied": True})
    return records


def zone_candidates(domain):
    """The zone names a hostname could live in, longest first (example.co.uk -> two candidates)."""
    labels = domain.split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


def render_plan_text(specs, current, tunnel_id=None):
    lines = ["# Cloudflare plan (printed only; nothing was changed)"]
    if current is None:
        lines.append("# No current configuration given: showing tenant rules and DNS only.")
        lines.append("# Export the live list (tenant_cloudflare.py fetch-current) to see the full replacement.")
        for spec in specs:
            for domain in spec["domains"]:
                lines.append(f"ingress (to be placed FIRST): {domain} -> {service_for(spec)}")
    else:
        rules = current.get("config", current).get("ingress", current) if isinstance(current, dict) else current
        new = build_ingress(specs, rules)
        lines.append(f"# {len(rules)} rules now, {len(new)} after. sha256 of the new list: {ingress_digest(new)}")
        for index, rule in enumerate(new, 1):
            host = rule.get("hostname", "*any*")
            lines.append(f"{index:2d}. host={host:<24} path={rule.get('path', '-'):<22} -> {rule['service']}")
    lines.append("# DNS (same Cloudflare account only; proxied CNAME to the tunnel):")
    for record in render_dns(specs, tunnel_id):
        lines.append(f"  {record['type']} {record['name']} -> {record['content']} proxied={record['proxied']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# API access (reviewed script; dry-run by default)
# --------------------------------------------------------------------------------------------


def read_credentials(path=ENV_FILE):
    values = {}
    with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        if not values.get(key):
            raise SystemExit(f"{key} missing from {path}")
    return values


class Api:
    def __init__(self, token, opener=None):
        self._token = token
        self._open = opener or urllib.request.urlopen

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            API + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
        )
        try:
            with self._open(request, timeout=30) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"Cloudflare API {method} {path}: HTTP {exc.code}") from None
        if not payload.get("success"):
            errors = [e.get("message") for e in payload.get("errors", [])]
            raise SystemExit(f"Cloudflare API {method} {path} failed: {errors}")
        return payload["result"]


def fetch_tunnel_config(api, account_id, tunnel_id):
    return api.call("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations")


def apply_ingress(api, account_id, tunnel_id, specs, live_file, approved_sha, apply, backup_dir, out=print):
    with open(live_file, "r", encoding="utf-8") as handle:  # audit-ignore-path
        planned_from = json.load(handle)
    rules = planned_from["config"]["ingress"]
    new = build_ingress(specs, rules)
    digest = ingress_digest(new)
    out(f"new ingress list: {len(new)} rules, sha256 {digest}")
    live = fetch_tunnel_config(api, account_id, tunnel_id)
    if live.get("version") != planned_from.get("version") or live["config"]["ingress"] != rules:
        raise SystemExit("the live tunnel configuration changed since the plan was made; re-fetch and re-plan")
    if not apply:
        out("dry run: nothing was changed (add --apply and --approved-sha256 <digest above>)")
        return 0
    if approved_sha != digest:
        raise SystemExit("--approved-sha256 does not match this plan's digest; refusing to push")
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    backup = os.path.join(backup_dir, f"tunnel-{tunnel_id}-v{live.get('version')}.json")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(live, handle, indent=2)
    config = dict(live["config"])
    config["ingress"] = new
    api.call("PUT", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", {"config": config})
    out(f"applied; the previous configuration is saved at {backup}")
    return 0


def plan_or_apply_dns(api, specs, tunnel_id, apply, out=print):
    return apply_dns_records(api, render_dns(specs, tunnel_id), apply, out)


def apply_dns_records(api, records, apply, out=print):
    """Creates proxied CNAMEs for hostnames whose zone is in this account. Never modifies a record
    that exists with different content: it reports it and stops for a human. Returns 0, or 2 when
    something needs a human (a conflict, or a name whose zone is not in this account)."""
    exit_code = 0
    for record in records:
        zone = None
        for candidate in zone_candidates(record["name"]):
            found = api.call("GET", f"/zones?name={candidate}")
            if found:
                zone = found[0]
                break
        if zone is None:
            out(f"NOT IN THIS ACCOUNT: {record['name']} (add the zone, or use a custom hostname; see the doc)")
            exit_code = 2
            continue
        existing = api.call("GET", f"/zones/{zone['id']}/dns_records?name={record['name']}")
        if existing:
            same = any(r["type"] == "CNAME" and r["content"] == record["content"] and r.get("proxied") for r in existing)
            out(f"{'ok (already correct)' if same else 'CONFLICT, not touched'}: {record['name']} has "
                f"{[(r['type'], r['content']) for r in existing]}")
            if not same:
                exit_code = 2
            continue
        out(f"{'create' if apply else 'would create'}: {record['type']} {record['name']} -> {record['content']} proxied")
        if apply:
            api.call("POST", f"/zones/{zone['id']}/dns_records", record)
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--spec-dir", default=lib.Paths().spec_dir)
    parser.add_argument("--tunnel-id")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--current")
    p = sub.add_parser("fetch-current")
    p.add_argument("--out", required=True)
    p = sub.add_parser("apply-ingress")
    p.add_argument("--live-file", required=True)
    p.add_argument("--approved-sha256")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup-dir", default=os.path.expanduser("~/.secrets/cloudflare_backups"))
    p = sub.add_parser("plan-dns")
    p = sub.add_parser("apply-dns")
    p.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    specs = lib.load_specs(args.spec_dir)
    if args.command == "plan":
        current = None
        if args.current:
            with open(args.current, "r", encoding="utf-8") as handle:  # audit-ignore-path
                current = json.load(handle)
        print(render_plan_text(specs, current, args.tunnel_id))
        return 0
    if not args.tunnel_id:
        raise SystemExit("--tunnel-id is required for API commands")
    creds = read_credentials()
    api = Api(creds["CLOUDFLARE_API_TOKEN"])
    if args.command == "fetch-current":
        live = fetch_tunnel_config(api, creds["CLOUDFLARE_ACCOUNT_ID"], args.tunnel_id)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(live, handle, indent=2)
        print(f"saved the live configuration (version {live.get('version')}) to {args.out}")
        return 0
    if args.command == "apply-ingress":
        return apply_ingress(api, creds["CLOUDFLARE_ACCOUNT_ID"], args.tunnel_id, specs, args.live_file,
                             args.approved_sha256, args.apply, args.backup_dir)
    return plan_or_apply_dns(api, specs, args.tunnel_id, getattr(args, "apply", False))


if __name__ == "__main__":
    sys.exit(main())
