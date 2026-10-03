#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""tenant_ctl: the coordinator's tool for Odoo tenant instances (ADR 0105).

    tenant_ctl.py validate SPEC...               check specs and the whole fleet, change nothing
    tenant_ctl.py list                           tenants that exist, with unit state
    tenant_ctl.py create SPEC [--apply]          print the plan (default) or apply it; idempotent
    tenant_ctl.py status [NAME|--all] [--json]   unit, database, filestore, backup age, HTTP probes
    tenant_ctl.py health --all                   exit 1 if any tenant is down or answers 5xx
    tenant_ctl.py backup NAME|--all [--apply]    pg_dump + filestore archive with checksums
    tenant_ctl.py restore-test NAME|--all        restore the newest backup into a scratch database
    tenant_ctl.py upgrade NAME [--apply]         backup, stop, odoo -u, start (one tenant at a time)
    tenant_ctl.py delete NAME --confirm-delete NAME [--apply]
    tenant_ctl.py cloudflare-plan [--current FILE]   ingress rules and DNS records (prints only)

`create`, `backup` and `delete` print what they would do unless `--apply` is given. Applying needs
root and a host designated the `odoo_tenants` host class (`provision.py --host-class odoo_tenants`,
or HAMS_HOST_CLASSES=odoo_tenants): a test host never gets tenants. Secrets are never printed.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tenant_lib as lib  # noqa: E402

USER_AGENT = "HamsTenantHealth/1.0 (+local probe)"


def _out(line):
    print(line)


def http_probe(port, host):
    """Status code of GET / on the tenant's loopback port with the given Host header; 0 on error.
    Redirects are not followed: a 301 to the canonical host is a healthy answer."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/", headers={"Host": host, "User-Agent": USER_AGENT}
    )
    try:
        with opener.open(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return 0


def _require_apply_allowed(system):
    if os.geteuid() != 0:
        raise SystemExit("--apply needs root (sudo)")
    try:
        import infrastructure

        classes = infrastructure.host_classes()
    except ImportError as exc:
        raise SystemExit(f"cannot check the host class: {exc}")
    if lib.HOST_CLASS not in classes:
        raise SystemExit(
            f"this host is not designated {lib.HOST_CLASS}: refusing to create tenants here "
            "(provision.py --host-class odoo_tenants)"
        )


def _specs_for(args, paths):
    existing = lib.load_specs(paths.spec_dir) if os.path.isdir(paths.spec_dir) else []
    if getattr(args, "all", False):
        return existing
    chosen = [spec for spec in existing if spec["name"] == args.name]
    if not chosen:
        raise SystemExit(f"no tenant named {args.name!r} in {paths.spec_dir}")
    return chosen


def cmd_validate(args, paths, system):
    specs = [lib.load_spec(path) for path in args.specs]
    lib.validate_fleet(specs)
    for spec in specs:
        _out(f"ok {spec['name']}: {', '.join(spec['domains'])} port {spec['http_port']} "
             f"workers {spec['workers']}")
    return 0


def cmd_list(args, paths, system):
    specs = lib.load_specs(paths.spec_dir) if os.path.isdir(paths.spec_dir) else []
    for spec in specs:
        state = system.run(
            ["systemctl", "is-active", f"hams-tenant@{spec['name']}.service"], check=False
        ).stdout.strip()
        _out(f"{spec['name']:<20} {state or 'unknown':<10} port {spec['http_port']} "
             f"{','.join(spec['domains'])}")
    return 0


def cmd_create(args, paths, system):
    spec = lib.load_spec(args.spec)
    existing = lib.load_specs(paths.spec_dir) if os.path.isdir(paths.spec_dir) else []
    others = [item for item in existing if item["name"] != spec["name"]]
    lib.validate_fleet(others + [spec])
    if args.apply:
        _require_apply_allowed(system)
    steps = lib.build_create_steps(
        spec, paths, system, apply_firewall=not args.no_firewall, start=not args.no_start,
        all_specs=others + [spec],
    )
    _out(f"{'APPLY' if args.apply else 'PLAN (nothing is changed; add --apply)'}: tenant {spec['name']}")
    todo = lib.execute(steps, args.apply, _out)
    _out(f"{todo} step(s) {'executed' if args.apply else 'to do'}")
    if args.apply:
        _out(f"admin password: {paths.secret_dir(spec['name'])}/admin_password (root only; not printed)")
    return 0


def cmd_status(args, paths, system):
    rows = [lib.tenant_status(spec, paths, system, http_probe) for spec in _specs_for(args, paths)]
    if args.json:
        _out(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        _out(
            f"{row['name']:<18} unit={row['unit']:<9} db={_mb(row['database_bytes'])} "
            f"files={_mb(row['filestore_bytes'])} backup_age_h={row['backup_age_hours']} "
            f"http={row['http']} healthy={row['healthy']}"
        )
    return 0


def _mb(value):
    return "n/a" if value is None else f"{value / 1048576:.1f}MB"


def cmd_health(args, paths, system):
    rows = [lib.tenant_status(spec, paths, system, http_probe) for spec in _specs_for(args, paths)]
    bad = [row for row in rows if not row["healthy"]]
    stale = [row for row in rows if row["backup_age_hours"] is None or row["backup_age_hours"] > 36]
    for row in bad:
        _out(f"UNHEALTHY {row['name']}: unit={row['unit']} http={row['http']}")
    for row in stale:
        _out(f"BACKUP STALE {row['name']}: age_hours={row['backup_age_hours']}")
    return 1 if bad or stale else 0


def cmd_backup(args, paths, system):
    specs = _specs_for(args, paths)
    if not args.apply:
        for spec in specs:
            _out(f"would back up {spec['name']} to {paths.backup_dir(spec['name'])}/<UTC stamp>/")
        return 0
    _require_apply_allowed(system)
    failures = 0
    for spec in specs:
        try:
            directory = lib.backup_tenant(spec, paths, system)
            problems = lib.verify_backup(directory)
            if problems:
                raise RuntimeError("; ".join(problems))
            _out(f"backed up {spec['name']}: {directory}")
        except Exception as exc:  # report every tenant, then fail the run
            failures += 1
            _out(f"BACKUP FAILED {spec['name']}: {exc}")
    return 1 if failures else 0


def cmd_restore_test(args, paths, system):
    failures = 0
    for spec in _specs_for(args, paths):
        try:
            result = lib.restore_test(spec, paths, system)
            _out(f"restore ok {spec['name']}: {json.dumps(result, sort_keys=True)}")
        except Exception as exc:
            failures += 1
            _out(f"RESTORE TEST FAILED {spec['name']}: {exc}")
    return 1 if failures else 0


def cmd_upgrade(args, paths, system):
    spec = _specs_for(args, paths)[0]
    if args.apply:
        _require_apply_allowed(system)
    steps = lib.build_upgrade_steps(spec, paths, system)
    _out(f"{'APPLY' if args.apply else 'PLAN (nothing is changed; add --apply)'}: upgrade {spec['name']}")
    lib.execute(steps, args.apply, _out)
    if args.apply:
        row = lib.tenant_status(spec, paths, system, http_probe)
        _out(f"after the upgrade: unit={row['unit']} http={row['http']} healthy={row['healthy']}")
        return 0 if row["healthy"] else 1
    return 0


def cmd_delete(args, paths, system):
    spec = _specs_for(args, paths)[0]
    if args.confirm_delete != spec["name"]:
        raise SystemExit(
            f"refusing: pass --confirm-delete {spec['name']} (exactly the tenant name) to delete it"
        )
    if args.apply:
        _require_apply_allowed(system)
    steps = lib.build_delete_steps(spec, paths, system, all_specs=lib.load_specs(paths.spec_dir))
    _out(f"{'APPLY' if args.apply else 'PLAN (nothing is changed; add --apply)'}: delete {spec['name']}")
    lib.execute(steps, args.apply, _out)
    return 0


def cmd_cloudflare_plan(args, paths, system):
    import tenant_cloudflare

    specs = lib.load_specs(paths.spec_dir) if os.path.isdir(paths.spec_dir) else []
    if args.spec_dir:
        specs = lib.load_specs(args.spec_dir)
    current = None
    if args.current:
        with open(args.current, "r", encoding="utf-8") as handle:  # audit-ignore-path
            current = json.load(handle)
    _out(tenant_cloudflare.render_plan_text(specs, current))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)
    p = sub.add_parser("validate")
    p.add_argument("specs", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("create")
    p.add_argument("spec")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--plan", action="store_true", help="the default; accepted for clarity")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--no-firewall", action="store_true")
    p.set_defaults(func=cmd_create)

    for name, func in (("status", cmd_status), ("health", cmd_health), ("backup", cmd_backup),
                       ("restore-test", cmd_restore_test)):
        p = sub.add_parser(name)
        p.add_argument("name", nargs="?")
        p.add_argument("--all", action="store_true")
        if name == "status":
            p.add_argument("--json", action="store_true")
        if name == "backup":
            p.add_argument("--apply", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("upgrade")
    p.add_argument("name")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("delete")
    p.add_argument("name")
    p.add_argument("--confirm-delete")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("cloudflare-plan")
    p.add_argument("--current", help="JSON file: the tunnel's current configuration (GET result)")
    p.add_argument("--spec-dir")
    p.set_defaults(func=cmd_cloudflare_plan)
    return parser


def main(argv=None, system=None, paths=None):
    args = build_parser().parse_args(argv)
    if args.command in ("status", "health", "backup", "restore-test") and not (args.all or args.name):
        raise SystemExit("give a tenant name or --all")
    paths = paths or lib.Paths()
    system = system or lib.System()
    return args.func(args, paths, system)


if __name__ == "__main__":
    sys.exit(main())
