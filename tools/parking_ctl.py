#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""parking_ctl: bulk management of the domains served by the Parking tenant (ADR 0105).

    parking_ctl.py validate FILE                 syntax, IDNA, duplicates, tenant/hams.com conflicts
    parking_ctl.py plan FILE [options]           what would be created/updated, and the Cloudflare steps
    parking_ctl.py apply FILE [options] --apply  create or update the parking.domain records
    parking_ctl.py archive FILE --apply          deactivate listed domains (never deletes)
    parking_ctl.py dns FILE --tunnel-id ID [--apply]   proxied CNAMEs for zones in this account
    parking_ctl.py export [--format json|csv]    current records
    parking_ctl.py report                        counts by behaviour

FILE is plain text, one domain per line; `#` starts a comment. After the domain, optional key=value
words (quote values with spaces): behavior=parked|redirect|for_sale|gone  url=https://target/
code=301|302  preserve_path=1  www=0  noindex=0  ttl=3600  title="..."  message="..."  price="..."
Command-line options give the defaults for every line that does not say otherwise.

Everything is a dry run unless --apply is given. Records are matched by normalized name, so running
the same file twice changes nothing. Nothing here deletes a record and nothing pushes the tunnel's
route list: DNS records go through tenant_cloudflare.apply_dns_records (it refuses to modify a
record that already exists with other content), and the tunnel's catch-all rule is a one-time step in
docs/proposals/MULTI_TENANT_ODOO.md.

The Odoo connection reads the parking tenant's admin password (or, with --transport json2, an API key) from a root-only file
(/opt/hams/etc/tenants/parking/admin_password) and never prints it. Run it on the server, as root.
"""

import argparse
import csv
import json
import os
import shlex
import sys
import xmlrpc.client
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import odoo_site_migrate as mig  # noqa: E402
import tenant_lib as lib  # noqa: E402

BEHAVIORS = ("parked", "redirect", "for_sale", "gone")
FIELDS = (
    "behavior", "redirect_url", "redirect_code", "preserve_path", "include_www", "title", "message",
    "price_text", "noindex", "cache_ttl",
)
KEYS = {
    "behavior": "behavior", "url": "redirect_url", "code": "redirect_code",
    "preserve_path": "preserve_path", "www": "include_www", "noindex": "noindex",
    "ttl": "cache_ttl", "title": "title", "message": "message", "price": "price_text",
}
BOOLS = ("preserve_path", "include_www", "noindex")
MAX_BATCH = 200


class InputError(ValueError):
    pass


def parse_bool(text):
    if str(text).lower() in ("1", "true", "yes", "on"):
        return True
    if str(text).lower() in ("0", "false", "no", "off"):
        return False
    raise InputError(f"not a boolean: {text!r}")


def default_values():
    return {
        "behavior": "parked", "redirect_url": False, "redirect_code": "301", "preserve_path": False,
        "include_www": True, "title": False, "message": False, "price_text": False,
        "noindex": True, "cache_ttl": 3600,
    }


# [@ANCHOR: parking_ctl:parse_domain_file]
# Verified by [@ANCHOR: test_parking_ctl:parse_domain_file]
def parse_domain_file(text, defaults=None, reserved=()):
    """Returns (entries, problems). entries: {normalized domain: values dict}. Problems are
    human-readable strings; a file with any problem is never applied."""
    entries, problems = {}, []
    base = default_values()
    base.update(defaults or {})
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            words = shlex.split(line)
        except ValueError as exc:
            problems.append(f"line {number}: {exc}")
            continue
        try:
            domain = lib.normalize_domain(words[0])
        except lib.SpecError as exc:
            problems.append(f"line {number}: {exc}")
            continue
        values = dict(base)
        try:
            for word in words[1:]:
                key, sep, value = word.partition("=")
                if not sep or key not in KEYS:
                    raise InputError(f"unknown option {word!r}")
                field = KEYS[key]
                values[field] = parse_bool(value) if field in BOOLS else (int(value) if field == "cache_ttl" else value)
            problem = check_values(domain, values)
        except (InputError, ValueError) as exc:
            problem = str(exc)
        if problem:
            problems.append(f"line {number} ({domain}): {problem}")
            continue
        if domain in entries:
            problems.append(f"line {number}: duplicate domain {domain} (first on an earlier line)")
            continue
        if domain in reserved:
            problems.append(f"line {number}: {domain} is a host of an Odoo tenant, not a parked domain")
            continue
        entries[domain] = values
    return entries, problems


def check_values(domain, values):
    if values["behavior"] not in BEHAVIORS:
        return f"behavior must be one of {', '.join(BEHAVIORS)}"
    if values["redirect_code"] not in ("301", "302"):
        return "code must be 301 or 302"
    if not 0 <= values["cache_ttl"] <= 31536000:
        return "ttl must be 0..31536000"
    if values["behavior"] == "redirect":
        return check_redirect(domain, values["redirect_url"])
    return ""


def check_redirect(domain, url):
    if not url or not isinstance(url, str) or len(url) > 2000:
        return "redirect needs url=https://..."
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url) or "\\" in url:
        return "url has spaces, control characters or a backslash"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        return "url must be http(s)://host/... without credentials"
    try:
        host = lib.normalize_domain(parts.hostname or "")
        parts.port
    except (lib.SpecError, ValueError):
        return "url host or port is invalid"
    if host == domain:
        return "url redirects to the domain itself"
    return ""


def tenant_hosts(spec_dir):
    hosts = set()
    if os.path.isdir(spec_dir):
        for spec in lib.load_specs(spec_dir):
            hosts.update(spec["domains"])
    return hosts


def dns_records(entries, tunnel_id):
    target = f"{tunnel_id or '<TUNNEL_ID>'}.cfargotunnel.com"
    names = []
    for domain, values in sorted(entries.items()):
        names.append(domain)
        if values["include_www"] and not domain.startswith("www."):
            names.append("www." + domain)
    return [{"type": "CNAME", "name": name, "content": target, "proxied": True} for name in names]


class OdooClient:
    """Thin client for the parking instance (loopback, admin account): XML-RPC, or any transport with
    `call(model, method, args, kwargs)` (the JSON-2 one of odoo_site_migrate)."""

    def __init__(self, url, db, login, password, proxy_factory=None, transport=None):
        self._transport = transport
        if transport is not None:
            return
        self._db, self._password = db, password
        common = proxy_factory(f"{url}/xmlrpc/2/common")
        self._uid = common.authenticate(db, login, password, {})
        if not self._uid:
            raise SystemExit("authentication to the parking instance failed")
        self._models = proxy_factory(f"{url}/xmlrpc/2/object", allow_none=True)

    def call(self, model, method, args, kwargs=None):
        if self._transport is not None:
            return self._transport.call(model, method, args, kwargs)
        return self._models.execute_kw(self._db, self._uid, self._password, model, method, args, kwargs or {})

    def search_read(self, domain, fields):
        return self.call("parking.domain", "search_read", [domain], {"fields": fields, "context": {"active_test": False}})

    def create(self, vals_list):
        return self.call("parking.domain", "create", [vals_list])

    def write(self, ids, vals):
        return self.call("parking.domain", "write", [ids, vals])


def connect(args):
    try:
        with open(args.password_file, "r", encoding="utf-8") as handle:  # audit-ignore-path
            password = handle.read().strip()
    except OSError as exc:
        raise SystemExit(f"cannot read {args.password_file}: {exc} (run as root on the server)")
    if getattr(args, "transport", "xmlrpc") == "json2":
        creds = mig.Credentials(args.url, args.db, args.login, password)
        return OdooClient(args.url, args.db, args.login, password, transport=mig.Json2Transport(creds, min_interval=0.0))
    return OdooClient(args.url, args.db, args.login, password, xmlrpc.client.ServerProxy)


def normalized_existing(client):
    rows = client.search_read([], ["name", "active", *FIELDS])
    return {row["name"]: row for row in rows}


# [@ANCHOR: parking_ctl:diff]
# Verified by [@ANCHOR: test_parking_ctl:diff]
def diff(entries, existing):
    create, update, same = [], [], []
    for domain, values in sorted(entries.items()):
        row = existing.get(domain)
        if row is None:
            create.append((domain, values))
            continue
        changed = {f: values[f] for f in FIELDS if (row.get(f) or False) != (values[f] or False)}
        if not row.get("active", True):
            changed["active"] = True
        if changed:
            update.append((row["id"], domain, changed))
        else:
            same.append(domain)
    return create, update, same


def read_entries(args):
    defaults = {
        "behavior": args.behavior, "redirect_url": args.redirect_url or False,
        "redirect_code": args.redirect_code, "preserve_path": args.preserve_path,
        "title": args.title or False, "message": args.message or False,
        "noindex": not args.index,
    }
    with open(args.file, "r", encoding="utf-8") as handle:  # audit-ignore-path
        text = handle.read()
    reserved = tenant_hosts(args.spec_dir)
    entries, problems = parse_domain_file(text, defaults, reserved)
    return entries, problems


def cmd_validate(args, out):
    entries, problems = read_entries(args)
    for problem in problems:
        out(f"PROBLEM {problem}")
    out(f"{len(entries)} valid domain(s), {len(problems)} problem(s)")
    return 1 if problems else 0


def cloudflare_steps(entries, tunnel_id):
    lines = ["", "Cloudflare steps (nothing here was done):"]
    records = dns_records(entries, tunnel_id)
    lines.append(f"  1. {len(records)} DNS name(s) must point at the tunnel as proxied CNAMEs:")
    for record in records[:10]:
        lines.append(f"       {record['name']} -> {record['content']}")
    if len(records) > 10:
        lines.append(f"       ... and {len(records) - 10} more (parking_ctl.py dns FILE --tunnel-id ID prints all)")
    lines += [
        "  2. A domain whose zone is in this Cloudflare account: `parking_ctl.py dns FILE --tunnel-id ID --apply`",
        "     creates the records (it never changes an existing record). An apex name needs CNAME flattening",
        "     (Cloudflare applies it to a CNAME at the zone root).",
        "  3. A domain whose zone is NOT in this account cannot use the tunnel's cfargotunnel.com name",
        "     (it only proxies records of the same account). Either move the domain's nameservers to Cloudflare",
        "     (free zone) and use step 2, or add it as a Cloudflare for SaaS custom hostname (first 100 free,",
        "     then $0.10 per hostname per month; the owner adds a CNAME and a validation record at their DNS).",
        "  4. The tunnel's final rule must send unmatched hostnames to the parking port; see the design doc,",
        "     section 'Cloudflare', for the one reviewed ingress change.",
    ]
    return lines


def cmd_plan(args, out):
    entries, problems = read_entries(args)
    for problem in problems:
        out(f"PROBLEM {problem}")
    if problems:
        out("nothing planned: fix the problems first")
        return 1
    if args.offline:
        out(f"{len(entries)} domain(s) valid (offline: records not compared)")
    else:
        existing = normalized_existing(connect(args))
        create, update, same = diff(entries, existing)
        out(f"records: {len(create)} to create, {len(update)} to update, {len(same)} unchanged")
        for domain, values in create[:20]:
            out(f"  create {domain}: {values['behavior']}")
        for _id, domain, changed in update[:20]:
            out(f"  update {domain}: {sorted(changed)}")
    for line in cloudflare_steps(entries, args.tunnel_id):
        out(line)
    return 0


def cmd_apply(args, out):
    entries, problems = read_entries(args)
    for problem in problems:
        out(f"PROBLEM {problem}")
    if problems:
        out("refusing to apply a file with problems")
        return 1
    client = connect(args)
    create, update, same = diff(entries, normalized_existing(client))
    out(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(create)} create, {len(update)} update, {len(same)} unchanged")
    if not args.apply:
        out("add --apply to make these changes")
        return 0
    for start in range(0, len(create), MAX_BATCH):
        chunk = create[start:start + MAX_BATCH]
        client.create([dict(values, name=domain) for domain, values in chunk])
        out(f"created {len(chunk)}")
    for record_id, domain, changed in update:
        client.write([record_id], changed)
    out(f"updated {len(update)}")
    return 0


def cmd_archive(args, out):
    entries, problems = read_entries(args)
    if problems:
        for problem in problems:
            out(f"PROBLEM {problem}")
        return 1
    client = connect(args)
    existing = normalized_existing(client)
    ids = [existing[d]["id"] for d in entries if d in existing and existing[d].get("active", True)]
    out(f"{'ARCHIVE' if args.apply else 'DRY RUN: would archive'} {len(ids)} of {len(entries)} listed domain(s)")
    if args.apply and ids:
        client.write(ids, {"active": False})
    return 0


def cmd_dns(args, out, api=None):
    import tenant_cloudflare as cf

    entries, problems = read_entries(args)
    if problems:
        for problem in problems:
            out(f"PROBLEM {problem}")
        return 1
    if api is None:
        creds = cf.read_credentials()
        api = cf.Api(creds["CLOUDFLARE_API_TOKEN"])
    records = dns_records(entries, args.tunnel_id)
    return cf.apply_dns_records(api, records, args.apply, out)


def cmd_export(args, out):
    rows = normalized_existing(connect(args))
    ordered = [rows[name] for name in sorted(rows)]
    if args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(["name", "active", *FIELDS])
        for row in ordered:
            writer.writerow([row["name"], row["active"], *[row.get(f) for f in FIELDS]])
    else:
        out(json.dumps(ordered, indent=2, sort_keys=True))
    return 0


def cmd_report(args, out):
    rows = normalized_existing(connect(args))
    counts = {}
    for row in rows.values():
        key = (row["behavior"], "active" if row.get("active", True) else "archived")
        counts[key] = counts.get(key, 0) + 1
    out(f"{len(rows)} domain(s)")
    for (behavior, state), number in sorted(counts.items()):
        out(f"  {behavior:<10} {state:<9} {number}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:18110")
    parser.add_argument("--db", default="parking")
    parser.add_argument("--login", default="admin")
    parser.add_argument("--password-file", default="/opt/hams/etc/tenants/parking/admin_password",
                        help="the admin password (xmlrpc) or an API key (json2)")
    parser.add_argument("--transport", choices=("xmlrpc", "json2"), default="xmlrpc",
                        help="xmlrpc is deprecated in Odoo 19; json2 needs an API key in the file (default: xmlrpc)")
    parser.add_argument("--spec-dir", default=lib.Paths().spec_dir)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_file(name, func, defaults=True):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--behavior", default="parked", choices=BEHAVIORS)
        p.add_argument("--redirect-url")
        p.add_argument("--redirect-code", default="301", choices=("301", "302"))
        p.add_argument("--preserve-path", action="store_true")
        p.add_argument("--title")
        p.add_argument("--message")
        p.add_argument("--index", action="store_true", help="allow search engines (default: noindex)")
        p.set_defaults(func=func)
        return p

    with_file("validate", cmd_validate)
    p = with_file("plan", cmd_plan)
    p.add_argument("--tunnel-id")
    p.add_argument("--offline", action="store_true", help="do not contact the parking instance")
    p = with_file("apply", cmd_apply)
    p.add_argument("--apply", action="store_true")
    p = with_file("archive", cmd_archive)
    p.add_argument("--apply", action="store_true")
    p = with_file("dns", cmd_dns)
    p.add_argument("--tunnel-id", required=True)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("export")
    p.add_argument("--format", choices=("json", "csv"), default="json")
    p.set_defaults(func=cmd_export)
    sub.add_parser("report").set_defaults(func=cmd_report)
    return parser


def main(argv=None, out=print):
    args = build_parser().parse_args(argv)
    return args.func(args, out)


if __name__ == "__main__":
    sys.exit(main())
