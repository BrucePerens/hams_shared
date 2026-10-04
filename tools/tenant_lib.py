#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Declarative provisioning of single-purpose Odoo tenant instances (ADR 0105).

A tenant is a complete, separate Odoo instance for a low-traffic web site that is NOT hams.com
(perens.com, postopen.org, a parking instance for many parked domains). Each one gets its own
operating-system user, PostgreSQL role and database, filestore, odoo.conf, loopback port and systemd
unit. Nothing is shared with `hams_prod` except the PostgreSQL server process and the machine.

This module is the library; `tenant_ctl.py` is the command line. Everything here is written so that
the same code path serves `--plan` (print what would happen, change nothing) and `--apply`: every
operation is a `Step` with a read-only `done()` check and an `action()`, executed through a `System`
object. The tests run the real step list against a `FakeSystem`.

The only secrets are the tenant's Odoo admin password and master password. They are generated here,
written to root-only files and never printed, logged or passed on a command line. PostgreSQL access is
by peer authentication over the unix socket (OS user `t_<name>` is PostgreSQL role `t_<name>`), so no
database password exists at all.
"""

import argparse  # noqa: F401  (re-exported for tenant_ctl)
import dataclasses
import datetime
import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
MODULE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
SIZE_RE = re.compile(r"^[0-9]+[KMGT]?$")
PORT_LOW, PORT_HIGH = 18000, 18999

# Names a tenant may never have: they would collide with PostgreSQL's own databases or with
# anything that looks like production. "hams" prefixes are refused separately.
RESERVED_NAMES = frozenset(
    {"postgres", "template0", "template1", "odoo", "root", "admin", "tenant", "backup", "test"}
)

# Hostnames and suffixes that belong to hams.com itself. A tenant must never claim one: the tunnel's
# hams.com rules and the Odoo behind them would be shadowed or, worse, receive the tenant's traffic.
RESERVED_HOST_SUFFIXES = ("hams.com",)

# Loopback ports used by hams_prod and its daemons (docs/proposals/PROVISION_PRODUCTION_NOTES.md,
# "Update 2026-10-03"). The tenant range 18000-18999 cannot collide with them; this list is a second
# guard for a spec that bypasses the range check in a future edit.
RESERVED_PORTS = frozenset(
    {22, 53, 3000, 5432, 5672, 6379, 8069, 8070, 8072, 8078, 8080, 8081, 8082, 8765, 8766}
)

# Vanilla Odoo addons a tenant may install with `-i`. Anything else must be an `extra_modules` entry
# from EXTRA_MODULE_ALLOWLIST (copied from hams_open) so that no proprietary hams_com module and no
# module needing the hams daemons can reach a tenant by a typo in a spec.
EXTRA_MODULE_ALLOWLIST = frozenset({"parking", "edge_cache"})

# Tenant systemd units are instances of this template (MANIFEST static_files).
UNIT_TEMPLATE = "hams-tenant@.service"
HOST_CLASS = "odoo_tenants"

DEFAULTS = {
    "description": "",
    "workers": 0,
    "max_cron_threads": 1,
    "gevent_port": None,
    "memory_max": "768M",
    "memory_high": "640M",
    "cpu_quota": "100%",
    "tasks_max": 256,
    "db_maxconn": 10,
    "pg_connection_limit": 14,
    "worker_memory_soft": 536870912,
    "worker_memory_hard": 805306368,
    "modules": [],
    "extra_modules": [],
    "admin_login": "admin",
    "language": "en_US",
    "notes": "",
    "catch_all": False,
    # A public-site tenant (perens.com, postopen.org: NIGHT_PLAN decision 38): no signup, no invitations, no
    # mail. The tunnel answers 404 for the Odoo backend, login and API paths on its public names, so the
    # admin reaches them only through an SSH tunnel to the loopback port.
    "public_site_only": False,
    # A read-only static file server beside the Odoo tenant (static_site_ctl.py): {"port": 18201, "path": "/static/"}.
    # The tunnel sends that path of the tenant's hostnames to the port; everything else goes to Odoo.
    "static_site": None,
}


class SpecError(ValueError):
    """A tenant spec is invalid. Raised, never papered over (fail fast)."""


@dataclasses.dataclass(frozen=True)
class Paths:
    """Every filesystem location the tool touches, so tests can point it at a temp directory."""

    conf_root: str = "/etc/hams-tenants"
    secret_root: str = "/opt/hams/etc/tenants"
    spec_dir: str = "/opt/hams/etc/tenants.d"
    data_root: str = "/var/lib/hams-tenants"
    backup_root: str = "/opt/hams/backups/tenants"
    addons_root: str = "/usr/local/lib/hams-tenant-addons"
    systemd_dir: str = "/etc/systemd/system"
    nft_file: str = "/etc/hams-tenants/firewall.nft"
    odoo_bin: str = "/usr/bin/odoo"
    stock_addons: str = "/usr/lib/python3/dist-packages/odoo/addons"
    hams_open_src: str = "/opt/hams/src/hams_open"
    pg_hba_include_name: str = "hams-tenants.d"

    def conf_dir(self, name):
        return os.path.join(self.conf_root, name)

    def conf_file(self, name):
        return os.path.join(self.conf_dir(name), "odoo.conf")

    def secret_dir(self, name):
        return os.path.join(self.secret_root, name)

    def data_dir(self, name):
        return os.path.join(self.data_root, name)

    def backup_dir(self, name):
        return os.path.join(self.backup_root, name)

    def addons_dir(self, name):
        return os.path.join(self.addons_root, name)

    def dropin_dir(self, name):
        return os.path.join(self.systemd_dir, f"hams-tenant@{name}.service.d")


# --------------------------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------------------------


def normalize_domain(raw):
    """Returns the lowercase punycode form of a hostname or raises SpecError.

    Refuses wildcards, IP literals, a trailing dot, single labels and anything under hams.com."""
    if not isinstance(raw, str) or not raw.strip():
        raise SpecError(f"domain must be a non-empty string: {raw!r}")
    text = raw.strip().lower()
    if text.endswith("."):
        raise SpecError(f"domain must not end with a dot: {raw!r}")
    if "*" in text or "/" in text or ":" in text or "@" in text or " " in text:
        raise SpecError(f"domain must be a bare hostname: {raw!r}")
    try:
        ascii_form = text.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SpecError(f"domain is not valid IDNA: {raw!r} ({exc})") from exc
    if len(ascii_form) > 253:
        raise SpecError(f"domain longer than 253 characters: {raw!r}")
    labels = ascii_form.split(".")
    if len(labels) < 2:
        raise SpecError(f"domain needs at least two labels: {raw!r}")
    if all(label.isdigit() for label in labels):
        raise SpecError(f"domain looks like an IP address: {raw!r}")
    for label in labels:
        if not LABEL_RE.match(label):
            raise SpecError(f"invalid label {label!r} in domain {raw!r}")
    for suffix in RESERVED_HOST_SUFFIXES:
        if ascii_form == suffix or ascii_form.endswith("." + suffix):
            raise SpecError(f"{raw!r} belongs to hams.com and cannot be a tenant domain")
    return ascii_form


def _check_size(field, value):
    if not isinstance(value, str) or not SIZE_RE.match(value):
        raise SpecError(f"{field} must look like 768M or 1G: {value!r}")


def _check_int(field, value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SpecError(f"{field} must be an integer in {low}..{high}: {value!r}")


STATIC_PATH_RE = re.compile(r"^/[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*/$")


def _validate_static_site(value, spec):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"port", "path"}:
        raise SpecError('static_site must be {"port": N, "path": "/static/"}')
    path = value.get("path", "/static/")
    if not isinstance(path, str) or not STATIC_PATH_RE.match(path) or {".", ".."} & set(path.split("/")):
        raise SpecError(f"static_site.path must look like /static/: {path!r}")
    port = value.get("port")
    _check_int("static_site.port", port, PORT_LOW, PORT_HIGH)
    if port in RESERVED_PORTS:
        raise SpecError(f"static_site.port {port} is a hams_prod port")
    if port in (spec.get("http_port"), spec.get("gevent_port")):
        raise SpecError("static_site.port equals the tenant's own port")
    if spec["catch_all"]:
        raise SpecError("a catch_all tenant has no hostnames, so it cannot have a static_site")
    return {"port": port, "path": path}


# [@ANCHOR: tenant_lib:validate_spec]
# Verified by [@ANCHOR: test_tenant_lib:validate_spec]
def validate_spec(raw):
    """Returns a complete, validated copy of a tenant spec (defaults filled in)."""
    if not isinstance(raw, dict):
        raise SpecError("a tenant spec is a JSON object")
    unknown = set(raw) - set(DEFAULTS) - {"name", "domains", "http_port"}
    if unknown:
        raise SpecError(f"unknown spec key(s): {', '.join(sorted(unknown))}")
    spec = dict(DEFAULTS)
    spec.update(raw)
    name = spec.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SpecError(f"name must match {NAME_RE.pattern}: {name!r}")
    if name in RESERVED_NAMES or name.startswith(("hams", "pg_", "t_")):
        raise SpecError(f"name {name!r} is reserved")
    if not isinstance(spec["catch_all"], bool):
        raise SpecError("catch_all must be true or false")
    if not isinstance(spec["public_site_only"], bool):
        raise SpecError("public_site_only must be true or false")
    if spec["public_site_only"] and spec["catch_all"]:
        raise SpecError("a catch_all tenant has no public hostnames to restrict (parking has its own guard)")
    domains = spec.get("domains", [] if spec["catch_all"] else None)
    if not isinstance(domains, list) or (not domains and not spec["catch_all"]):
        raise SpecError("domains must be a non-empty list (only a catch_all tenant may have none)")
    normalized = [normalize_domain(item) for item in domains]
    if len(set(normalized)) != len(normalized):
        raise SpecError("duplicate domain in the spec")
    spec["domains"] = normalized
    for key in ("http_port", "gevent_port"):
        value = spec.get(key)
        if value is None and key == "gevent_port":
            continue
        _check_int(key, value, PORT_LOW, PORT_HIGH)
        if value in RESERVED_PORTS:
            raise SpecError(f"{key} {value} is a hams_prod port")
    if spec["gevent_port"] is not None and spec["gevent_port"] == spec["http_port"]:
        raise SpecError("gevent_port equals http_port")
    _check_int("workers", spec["workers"], 0, 8)
    _check_int("max_cron_threads", spec["max_cron_threads"], 0, 4)
    _check_int("tasks_max", spec["tasks_max"], 32, 4096)
    _check_int("db_maxconn", spec["db_maxconn"], 2, 64)
    _check_int("pg_connection_limit", spec["pg_connection_limit"], 3, 100)
    if spec["pg_connection_limit"] < spec["db_maxconn"] + 2:
        raise SpecError("pg_connection_limit must exceed db_maxconn by at least 2 (cron, shell)")
    if spec["workers"] == 0 and spec["gevent_port"] is not None:
        raise SpecError("gevent_port is meaningless with workers = 0 (websocket is served in-process)")
    if spec["workers"] > 0 and spec["gevent_port"] is None:
        # Odoo's default would be 8072, which is hams_prod's gevent worker. Never leave it implicit.
        raise SpecError("workers > 0 requires an explicit gevent_port (the default 8072 is hams_prod)")
    _check_size("memory_max", spec["memory_max"])
    _check_size("memory_high", spec["memory_high"])
    if not re.match(r"^[0-9]+%$", str(spec["cpu_quota"])):
        raise SpecError(f"cpu_quota must look like 100%: {spec['cpu_quota']!r}")
    for key in ("modules", "extra_modules"):
        value = spec[key]
        if not isinstance(value, list) or not all(
            isinstance(m, str) and MODULE_RE.match(m) for m in value
        ):
            raise SpecError(f"{key} must be a list of module names")
    for module in spec["extra_modules"]:
        if module not in EXTRA_MODULE_ALLOWLIST:
            raise SpecError(
                f"extra module {module!r} is not allow-listed for tenants "
                f"({', '.join(sorted(EXTRA_MODULE_ALLOWLIST))})"
            )
    if not re.match(r"^[A-Za-z0-9_.@-]{1,64}$", spec["admin_login"]):
        raise SpecError("admin_login has unsupported characters")
    spec["static_site"] = _validate_static_site(spec["static_site"], spec)
    if not re.match(r"^[a-z]{2,3}_[A-Z]{2}$", spec["language"]):
        raise SpecError(f"language must look like en_US: {spec['language']!r}")
    return spec


def validate_fleet(specs):
    """Cross-checks a list of validated specs: unique names, domains and ports, one catch-all."""
    seen = {"name": {}, "domain": {}, "port": {}}
    catch_alls = [spec["name"] for spec in specs if spec["catch_all"]]
    if len(catch_alls) > 1:
        raise SpecError(f"only one tenant may be the tunnel catch-all, not {catch_alls}")
    for spec in specs:
        _claim(seen["name"], spec["name"], spec["name"], "name")
        for domain in spec["domains"]:
            _claim(seen["domain"], domain, spec["name"], "domain")
        for key in ("http_port", "gevent_port"):
            if spec.get(key) is not None:
                _claim(seen["port"], spec[key], spec["name"], "port")
        if spec.get("static_site"):
            _claim(seen["port"], spec["static_site"]["port"], spec["name"], "port")


def _claim(table, key, owner, kind):
    if key in table and table[key] != owner:
        raise SpecError(f"{kind} {key!r} is claimed by both {table[key]!r} and {owner!r}")
    table[key] = owner


def load_spec(path):
    with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
        return validate_spec(json.load(handle))


def load_specs(directory):
    specs = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        specs.append(load_spec(path))
    validate_fleet(specs)
    return specs


def base_url(spec):
    if spec["domains"]:
        return f"https://{spec['domains'][0]}"
    return f"http://127.0.0.1:{spec['http_port']}"


def os_user(name):
    return f"t_{name}"


def pg_role(name):
    return f"t_{name}"


# --------------------------------------------------------------------------------------------
# Renderers (pure functions; the tests pin their output)
# --------------------------------------------------------------------------------------------


# [@ANCHOR: tenant_lib:render_odoo_conf]
# Verified by [@ANCHOR: test_tenant_lib:render_odoo_conf]
def render_odoo_conf(spec, paths, admin_passwd_hash):
    name = spec["name"]
    extra = [paths.addons_dir(name)] if spec["extra_modules"] else []
    addons_path = ",".join([paths.stock_addons] + extra)
    lines = [
        "[options]",
        "; Generated by hams_shared/tools/tenant_lib.py. Edits are overwritten by `tenant_ctl apply`.",
        f"db_name = {name}",
        f"db_user = {pg_role(name)}",
        "; No db_password and no db_host: peer authentication over the unix socket.",
        f"dbfilter = ^{name}$",
        "list_db = False",
        f"admin_passwd = {admin_passwd_hash}",
        "http_interface = 127.0.0.1",
        f"http_port = {spec['http_port']}",
        "proxy_mode = True",
        f"data_dir = {paths.data_dir(name)}",
        f"addons_path = {addons_path}",
        "server_wide_modules = base,web",
        f"workers = {spec['workers']}",
        f"max_cron_threads = {spec['max_cron_threads']}",
        f"db_maxconn = {spec['db_maxconn']}",
        "without_demo = True",
        "log_level = info",
        "logfile =",
    ]
    if spec["workers"] > 0:
        lines += [
            f"gevent_port = {spec['gevent_port']}",
            f"limit_memory_soft = {spec['worker_memory_soft']}",
            f"limit_memory_hard = {spec['worker_memory_hard']}",
            f"limit_memory_soft_gevent = {spec['worker_memory_soft']}",
            f"limit_memory_hard_gevent = {spec['worker_memory_hard']}",
            "limit_time_cpu = 60",
            "limit_time_real = 120",
            "limit_request = 4096",
        ]
    return "\n".join(lines) + "\n"


# [@ANCHOR: tenant_lib:render_unit_dropin]
# Verified by [@ANCHOR: test_tenant_lib:render_unit_dropin]
def render_unit_dropin(spec):
    """Per-tenant resource limits on top of the shared sandboxed template unit."""
    return (
        "# Generated by tenant_lib.py from the tenant spec.\n"
        "[Service]\n"
        f"MemoryHigh={spec['memory_high']}\n"
        f"MemoryMax={spec['memory_max']}\n"
        "MemorySwapMax=0\n"
        f"CPUQuota={spec['cpu_quota']}\n"
        f"TasksMax={spec['tasks_max']}\n"
        f"SocketBindAllow=tcp:{spec['http_port']}\n"
        + (f"SocketBindAllow=tcp:{spec['gevent_port']}\n" if spec["gevent_port"] else "")
    )


# [@ANCHOR: tenant_lib:render_pg_hba]
# Verified by [@ANCHOR: test_tenant_lib:render_pg_hba]
def render_pg_hba(spec):
    name = spec["name"]
    return (
        f"# tenant {name}: its own database, its own role, unix socket only (generated).\n"
        "# Odoo's startup connects to the maintenance database `postgres` once to see whether the\n"
        "# database exists (odoo/service/db.py _create_empty_database); without access to it the\n"
        "# server cannot start. That database is empty; the catalogs are readable from any database.\n"
        f"local   postgres,{name}   {pg_role(name)}   peer\n"
    )


PG_HBA_REJECT_FILE = "90-reject-tenants.conf"
PG_HBA_REJECT = (
    "# Every tenant role is a member of hams_tenant. Anything not allowed by a 10-<name>.conf line\n"
    "# above (in particular hams_prod and any other tenant's database) is refused, on every transport.\n"
    "local   all   +hams_tenant   reject\n"
    "host    all   +hams_tenant   0.0.0.0/0   reject\n"
    "host    all   +hams_tenant   ::/0        reject\n"
)


def pg_hba_include_line(include_dir):
    return f"include_dir {include_dir}"


# [@ANCHOR: tenant_lib:render_nft]
# Verified by [@ANCHOR: test_tenant_lib:render_nft]
def render_nft(specs):
    """One nftables table that stops tenant processes opening NEW connections to anything local.

    Selected by the tenant's uid. Loopback replies to cloudflared (established) are allowed, DNS is
    allowed, and everything else addressed to this machine, the WireGuard admin network or the cloud
    metadata address is refused. Outbound internet (outgoing mail through SES) stays open."""
    users = sorted(os_user(spec["name"]) for spec in specs)
    if not users:
        # An empty fleet removes the table (a stale rule keyed to a deleted account's uid would
        # otherwise stay loaded).
        return "# no tenants\ntable inet hams_tenants\ndelete table inet hams_tenants\n"
    uid_set = ", ".join(f'"{user}"' for user in users)
    return (
        "# Generated by tenant_lib.py. Loaded by hams-tenant-firewall.service.\n"
        "table inet hams_tenants\n"
        "delete table inet hams_tenants\n"
        "table inet hams_tenants {\n"
        "  chain output {\n"
        "    type filter hook output priority -10; policy accept;\n"
        f"    meta skuid {{ {uid_set} }} oifname \"lo\" ct state established,related accept\n"
        f"    meta skuid {{ {uid_set} }} oifname \"lo\" udp dport 53 accept\n"
        f"    meta skuid {{ {uid_set} }} oifname \"lo\" tcp dport 53 accept\n"
        f"    meta skuid {{ {uid_set} }} oifname \"lo\" reject\n"
        f"    meta skuid {{ {uid_set} }} ip daddr {{ 10.99.0.0/24, 169.254.0.0/16 }} reject\n"
        "  }\n"
        "}\n"
    )


# [@ANCHOR: tenant_lib:render_pg_sql]
# Verified by [@ANCHOR: test_tenant_lib:render_pg_sql]
def render_pg_sql(spec, phase):
    """SQL for one phase, all identifiers validated by NAME_RE so interpolation is safe."""
    name = spec["name"]
    role = pg_role(name)
    limit = spec["pg_connection_limit"]
    if phase == "group":
        return "CREATE ROLE hams_tenant NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;"
    if phase == "role":
        return (
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            f"NOBYPASSRLS CONNECTION LIMIT {limit} IN ROLE hams_tenant;"
        )
    if phase == "role_limit":
        return f"ALTER ROLE {role} CONNECTION LIMIT {limit};"
    if phase == "database":
        return (
            f"CREATE DATABASE {name} OWNER {role} TEMPLATE template0 ENCODING 'UTF8';"
        )
    if phase == "database_acl":
        return f"REVOKE ALL ON DATABASE {name} FROM PUBLIC; GRANT CONNECT ON DATABASE {name} TO {role};"
    if phase == "extensions":
        return "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS unaccent;"
    raise ValueError(phase)


def render_set_admin_sql(login, password_hash, base_url):
    """Parameters go through psql variables on stdin; the hash is the only sensitive-ish value."""
    return (
        "UPDATE res_users SET login = %s, password = %s WHERE id = 2;\n"
        % (_sql_literal(login), _sql_literal(password_hash))
        + "INSERT INTO ir_config_parameter (key, value, create_date, write_date) VALUES "
        "('web.base.url', %s, now(), now()), ('web.base.url.freeze', 'True', now(), now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = now();\n"
        % _sql_literal(base_url)
    )


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def hash_password(password, rounds=600_000):
    """PBKDF2-SHA512, the scheme Odoo's res.users uses (same as tools/hash_admin_password.py)."""
    from passlib.hash import pbkdf2_sha512  # imported late: only needed when applying

    return pbkdf2_sha512.using(rounds=rounds).hash(password)


def generate_password(length=32):
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --------------------------------------------------------------------------------------------
# System access
# --------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class System:
    """The real system. Tests substitute FakeSystem (test_tenant_lib.py)."""

    def run(self, argv, input_text=None, check=True):
        proc = subprocess.run(
            argv, input=input_text, capture_output=True, text=True, check=False
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"command failed ({proc.returncode}): {redact(argv)}: {proc.stderr.strip()[:500]}"
            )
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def exists(self, path):
        return os.path.exists(path)

    def read_text(self, path):
        try:
            with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
                return handle.read()
        except FileNotFoundError:
            return None

    def write_text(self, path, text, mode, owner):
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        self._chown(tmp, owner)
        os.replace(tmp, path)

    def makedirs(self, path, mode, owner):
        os.makedirs(path, exist_ok=True)
        os.chmod(path, mode)
        self._chown(path, owner)

    def _chown(self, path, owner):
        user, group = owner.split(":")
        shutil.chown(path, user=user, group=group)

    def user_exists(self, user):
        return self.run(["getent", "passwd", user], check=False).returncode == 0

    def copy_tree(self, src, dst):
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "i18n_extra"))
        for root, dirs, files in os.walk(dst):
            os.chmod(root, 0o755)
            for filename in files:
                os.chmod(os.path.join(root, filename), 0o644)

    def run_to_file(self, argv, path):
        """Runs argv with its (binary) stdout going straight into a new 0600 file."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            proc = subprocess.run(argv, stdout=handle, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"command failed ({proc.returncode}): {redact(argv)}: "
                               f"{proc.stderr.decode(errors='replace').strip()[:500]}")

    def run_from_file(self, argv, path):
        """Runs argv with a file as its stdin: root opens the root-only backup, postgres reads the
        pipe (postgres itself cannot open files under the 0700 backup directory)."""
        with open(path, "rb") as handle:  # audit-ignore-path
            proc = subprocess.run(argv, stdin=handle, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"command failed ({proc.returncode}): {redact(argv)}: "
                               f"{proc.stderr.decode(errors='replace').strip()[:500]}")

    def listdir(self, path):
        try:
            return sorted(os.listdir(path))
        except FileNotFoundError:
            return []

    def remove_tree(self, path):
        shutil.rmtree(path, ignore_errors=True)

    def remove_file(self, path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def now(self):
        return datetime.datetime.now(datetime.timezone.utc)


def redact(argv):
    return " ".join("<redacted>" if "password" in str(a).lower() else str(a) for a in argv)


# --------------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Step:
    key: str
    description: str
    action: object
    done: object = None


def psql(system, sql, database="postgres", check=True):
    return system.run(
        ["runuser", "-u", "postgres", "--", "psql", "-X", "-q", "-A", "-t", "-v",
         "ON_ERROR_STOP=1", "-d", database, "-c", sql],
        check=check,
    )


def pg_scalar(system, sql, database="postgres"):
    result = psql(system, sql, database, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _role_exists(system, role):
    return pg_scalar(system, f"SELECT 1 FROM pg_roles WHERE rolname = '{role}'") == "1"


def _db_exists(system, name):
    return pg_scalar(system, f"SELECT 1 FROM pg_database WHERE datname = '{name}'") == "1"


def installed_modules(system, name):
    out = pg_scalar(
        system, "SELECT string_agg(name, ',') FROM ir_module_module WHERE state = 'installed'", name
    )
    return set(out.split(",")) if out else set()


def pg_hba_file(system):
    return pg_scalar(system, "SHOW hba_file")


def pg_hba_errors(system):
    return pg_scalar(system, "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")


# [@ANCHOR: tenant_lib:build_create_steps]
# Verified by [@ANCHOR: test_tenant_lib:build_create_steps]
def build_create_steps(spec, paths, system, apply_firewall=True, start=True, all_specs=None):
    """The ordered, idempotent list of steps that creates (or repairs) one tenant."""
    name = spec["name"]
    user = os_user(name)
    role = pg_role(name)
    conf = paths.conf_file(name)
    secret_dir = paths.secret_dir(name)
    steps = []

    def add(key, description, action, done=None):
        steps.append(Step(key, description, action, done))

    include_dir = None  # resolved lazily from SHOW hba_file

    def hba_dir():
        nonlocal include_dir
        if include_dir is None:
            hba = pg_hba_file(system)
            if not hba:
                raise RuntimeError("cannot determine pg_hba.conf location from PostgreSQL")
            include_dir = os.path.join(os.path.dirname(hba), paths.pg_hba_include_name)
        return include_dir

    # -- operating system -------------------------------------------------------------------
    add(
        "os_user",
        f"create system user and group {user} (no login, home {paths.data_dir(name)})",
        lambda: system.run(
            ["useradd", "--system", "--user-group", "--home-dir", paths.data_dir(name),
             "--no-create-home", "--shell", "/usr/sbin/nologin", user]
        ),
        lambda: system.user_exists(user),
    )
    for key, path, mode, owner in (
        ("dir_data", paths.data_dir(name), 0o750, f"{user}:{user}"),
        ("dir_conf", paths.conf_dir(name), 0o750, f"root:{user}"),
        ("dir_secret", secret_dir, 0o700, "root:root"),
        ("dir_backup", paths.backup_dir(name), 0o700, "root:root"),
    ):
        add(
            key,
            f"directory {path} mode {mode:o} owner {owner}",
            lambda p=path, m=mode, o=owner: system.makedirs(p, m, o),
            lambda p=path: system.exists(p),
        )

    # -- PostgreSQL -------------------------------------------------------------------------
    add(
        "pg_group_role",
        "PostgreSQL group role hams_tenant (NOLOGIN; membership is what pg_hba rejects)",
        lambda: psql(system, render_pg_sql(spec, "group")),
        lambda: _role_exists(system, "hams_tenant"),
    )
    add(
        "pg_role",
        f"PostgreSQL role {role} (LOGIN, no superuser/createdb/createrole, limit "
        f"{spec['pg_connection_limit']} connections, peer authentication, no password)",
        lambda: psql(system, render_pg_sql(spec, "role")),
        lambda: _role_exists(system, role),
    )
    add(
        "pg_role_limit",
        f"PostgreSQL role {role} connection limit {spec['pg_connection_limit']}",
        lambda: psql(system, render_pg_sql(spec, "role_limit")),
        lambda: pg_scalar(
            system, f"SELECT rolconnlimit FROM pg_roles WHERE rolname = '{role}'"
        ) == str(spec["pg_connection_limit"]),
    )
    add(
        "pg_database",
        f"database {name} owned by {role} (template0, UTF8)",
        lambda: psql(system, render_pg_sql(spec, "database")),
        lambda: _db_exists(system, name),
    )
    add(
        "pg_database_acl",
        f"revoke CONNECT on {name} from PUBLIC; grant it to {role} only",
        lambda: psql(system, render_pg_sql(spec, "database_acl")),
        lambda: pg_scalar(
            system,
            "SELECT (d.datacl IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM aclexplode(d.datacl) a WHERE a.grantee = 0))::int "
            f"FROM pg_database d WHERE d.datname = '{name}'",
        ) == "1",
    )
    add(
        "pg_extensions",
        f"pg_trgm and unaccent extensions in {name} (created as postgres; both are trusted)",
        lambda: psql(system, render_pg_sql(spec, "extensions"), name),
        lambda: pg_scalar(
            system, "SELECT count(*) FROM pg_extension WHERE extname IN ('pg_trgm','unaccent')", name
        ) == "2",
    )

    def write_hba():
        directory = hba_dir()
        system.makedirs(directory, 0o755, "postgres:postgres")
        previous = {}
        files = {
            f"10-{name}.conf": render_pg_hba(spec),
            PG_HBA_REJECT_FILE: PG_HBA_REJECT,
        }
        for filename, text in files.items():
            target = os.path.join(directory, filename)
            previous[target] = system.read_text(target)
            system.write_text(target, text, 0o640, "postgres:postgres")
        main = pg_hba_file(system)
        text = system.read_text(main) or ""
        line = pg_hba_include_line(directory)
        if line not in text:
            new_text = insert_pg_hba_include(text, line)
            system.write_text(main + ".hams-tenants.bak", text, 0o640, "postgres:postgres")
            system.write_text(main, new_text, 0o640, "postgres:postgres")
        errors = pg_hba_errors(system)
        if errors != "0":
            for target, old in previous.items():
                if old is None:
                    system.remove_file(target)
                else:
                    system.write_text(target, old, 0o640, "postgres:postgres")
            raise RuntimeError(f"pg_hba.conf has {errors} error(s) after the tenant change; rolled back")
        psql(system, "SELECT pg_reload_conf();")

    def hba_done():
        directory = hba_dir()
        hba_text = system.read_text(pg_hba_file(system)) or ""
        return (
            pg_hba_include_line(directory) in hba_text
            and system.read_text(os.path.join(directory, f"10-{name}.conf")) == render_pg_hba(spec)
            and system.read_text(os.path.join(directory, PG_HBA_REJECT_FILE)) == PG_HBA_REJECT
        )

    add(
        "pg_hba",
        "pg_hba.conf: include_dir hams-tenants.d (one line, before the generic rules), "
        f"10-{name}.conf allows only {role} on {name} by peer, 90-reject refuses the rest for every "
        "tenant; validated through pg_hba_file_rules and rolled back on error, then pg_reload_conf",
        write_hba,
        hba_done,
    )

    # -- addons, secrets, configuration -------------------------------------------------------
    if spec["extra_modules"]:
        for module in spec["extra_modules"]:
            src = os.path.join(paths.hams_open_src, module)
            dst = os.path.join(paths.addons_dir(name), module)
            add(
                f"addon_{module}",
                f"copy hams_open module {module} to {dst} (root-owned, read-only for the tenant)",
                lambda s=src, d=dst: system.copy_tree(s, d),
                None,
            )

    def secrets_done():
        return system.exists(os.path.join(secret_dir, "admin_password")) and system.exists(
            os.path.join(secret_dir, "master_password")
        )

    def make_secrets():
        for filename in ("admin_password", "master_password"):
            target = os.path.join(secret_dir, filename)
            if not system.exists(target):
                system.write_text(target, generate_password() + "\n", 0o600, "root:root")

    add(
        "secrets",
        f"generate admin and master passwords into {secret_dir} (root:root 0600, never printed)",
        make_secrets,
        secrets_done,
    )

    def write_conf():
        master = (system.read_text(os.path.join(secret_dir, "master_password")) or "").strip()
        text = render_odoo_conf(spec, paths, hash_password(master))
        system.write_text(conf, text, 0o640, f"root:{user}")

    def conf_done():
        current = system.read_text(conf)
        if current is None:
            return False
        # The hash is salted, so compare everything except the admin_passwd line.
        return _without_hash(current) == _without_hash(render_odoo_conf(spec, paths, "x"))

    add("odoo_conf", f"write {conf} (root:{user} 0640)", write_conf, conf_done)

    dropin = os.path.join(paths.dropin_dir(name), "10-spec.conf")
    add(
        "unit_dropin",
        f"write {dropin} (MemoryMax {spec['memory_max']}, CPUQuota {spec['cpu_quota']}, "
        f"TasksMax {spec['tasks_max']})",
        lambda: system.write_text(dropin, render_unit_dropin(spec), 0o644, "root:root"),
        lambda: system.read_text(dropin) == render_unit_dropin(spec),
    )
    spec_copy = os.path.join(paths.spec_dir, f"{name}.json")
    spec_text = json.dumps(spec, indent=2, sort_keys=True) + "\n"
    add(
        "spec_copy",
        f"record the validated spec at {spec_copy}",
        lambda: system.write_text(spec_copy, spec_text, 0o644, "root:root"),
        lambda: system.read_text(spec_copy) == spec_text,
    )

    # -- initial install ----------------------------------------------------------------------
    wanted = ["base"] + [m for m in spec["modules"] + spec["extra_modules"] if m != "base"]

    def install_modules():
        have = installed_modules(system, name) if _db_exists(system, name) else set()
        missing = [m for m in wanted if m not in have]
        if not missing:
            return
        system.run(
            ["runuser", "-u", user, "--", paths.odoo_bin, "--config", conf, "-d", name,
             "-i", ",".join(missing), "--without-demo=True", "--stop-after-init", "--no-http",
             "--max-cron-threads=0", "--log-level=warn"]
        )

    def modules_done():
        return set(wanted) <= installed_modules(system, name)

    add(
        "install_modules",
        f"odoo -i {','.join(wanted)} --without-demo=True --stop-after-init as {user} "
        "(no HTTP listener, no cron threads)",
        install_modules,
        modules_done,
    )

    marker = os.path.join(secret_dir, "admin_password.applied")

    def set_admin():
        password = (system.read_text(os.path.join(secret_dir, "admin_password")) or "").strip()
        sql = render_set_admin_sql(
            spec["admin_login"], hash_password(password), base_url(spec)
        )
        system.run(
            ["runuser", "-u", "postgres", "--", "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
             "-d", name, "-f", "-"],
            input_text=sql,
        )
        system.write_text(marker, "applied\n", 0o600, "root:root")

    add(
        "admin_password",
        f"set the Odoo admin login/password hash in {name}.res_users (SQL on stdin; plaintext stays in "
        f"{secret_dir}/admin_password) and web.base.url={base_url(spec)}",
        set_admin,
        lambda: system.exists(marker),
    )

    # -- firewall and service -----------------------------------------------------------------
    fleet = list(all_specs or [spec])
    if all(item["name"] != name for item in fleet):
        fleet.append(spec)
    if apply_firewall:
        nft_text = render_nft(fleet)
        add(
            "firewall",
            f"write {paths.nft_file} (tenant uids cannot open new connections to local services) "
            "and load it with nft -f",
            lambda: (
                system.write_text(paths.nft_file, nft_text, 0o640, "root:root"),
                system.run(["nft", "-f", paths.nft_file]),
            ),
            lambda: system.read_text(paths.nft_file) == nft_text,
        )
    if start:
        add(
            "service",
            f"systemctl daemon-reload; enable --now hams-tenant@{name}.service",
            lambda: (
                system.run(["systemctl", "daemon-reload"]),
                system.run(["systemctl", "enable", "--now", f"hams-tenant@{name}.service"]),
            ),
            lambda: system.run(
                ["systemctl", "is-active", f"hams-tenant@{name}.service"], check=False
            ).stdout.strip() == "active",
        )
    return steps


def _without_hash(text):
    return [line for line in text.splitlines() if not line.startswith("admin_passwd")]


def insert_pg_hba_include(text, line):
    """Puts the include before the first generic `local all all` rule, after the postgres rule."""
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        fields = raw.split("#", 1)[0].split()
        if len(fields) >= 4 and fields[0] == "local" and fields[1] == "all" and fields[2] == "all":
            lines.insert(index, "# hams.com tenants (tenant_ctl): per-tenant allow lines and the reject.")
            lines.insert(index + 1, line)
            return "\n".join(lines) + "\n"
    raise RuntimeError("no generic 'local all all' rule found in pg_hba.conf; refusing to guess")


def execute(steps, apply, out):
    """Runs or prints the steps. Returns the number of steps that were (or would be) executed."""
    todo = 0
    for index, step in enumerate(steps, 1):
        try:
            finished = bool(step.done()) if step.done else False
        except Exception as exc:  # a read-only check that cannot run counts as "not done"
            finished = False
            out(f"  (check for {step.key} could not run: {str(exc)[:120]})")
        label = "skip" if finished else ("run " if apply else "todo")
        out(f"[{index:02d}] {label} {step.key}: {step.description}")
        if finished:
            continue
        todo += 1
        if apply:
            step.action()
    return todo


# --------------------------------------------------------------------------------------------
# Backup, restore test, status
# --------------------------------------------------------------------------------------------


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:  # audit-ignore-path
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# [@ANCHOR: tenant_lib:backup_tenant]
# Verified by [@ANCHOR: test_tenant_lib:backup_tenant]
def backup_tenant(spec, paths, system, keep=7):
    """pg_dump -Fc of the tenant database plus a tar.gz of its filestore, with checksums.

    Written root-only under backup_root/<name>/<UTC stamp>/ ; the oldest sets beyond `keep` are
    removed. Returns the directory."""
    name = spec["name"]
    stamp = system.now().strftime("%Y%m%dT%H%M%SZ")
    target = os.path.join(paths.backup_dir(name), stamp)
    system.makedirs(target, 0o700, "root:root")
    dump = os.path.join(target, "db.dump")
    system.run_to_file(["runuser", "-u", "postgres", "--", "pg_dump", "-Fc", "-d", name], dump)
    filestore = os.path.join(paths.data_dir(name), "filestore", name)
    archive = os.path.join(target, "filestore.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        if os.path.isdir(filestore):
            tar.add(filestore, arcname="filestore")
    os.chmod(archive, 0o600)
    manifest = {
        "tenant": name,
        "created": stamp,
        "files": {
            "db.dump": {"sha256": sha256_file(dump), "bytes": os.path.getsize(dump)},
            "filestore.tar.gz": {"sha256": sha256_file(archive), "bytes": os.path.getsize(archive)},
        },
    }
    system.write_text(os.path.join(target, "MANIFEST.json"), json.dumps(manifest, indent=2) + "\n", 0o600, "root:root")
    sets = sorted(d for d in system.listdir(paths.backup_dir(name)) if re.match(r"^\d{8}T\d{6}Z$", d))
    for old in sets[:-keep] if keep > 0 else []:
        system.remove_tree(os.path.join(paths.backup_dir(name), old))
    return target


def verify_backup(directory):
    """Checks every file named in MANIFEST.json against its sha256. Returns a list of problems."""
    problems = []
    manifest_path = os.path.join(directory, "MANIFEST.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:  # audit-ignore-path
            manifest = json.load(handle)
    except (OSError, ValueError) as exc:
        return [f"MANIFEST.json unreadable: {exc}"]
    for filename, info in manifest.get("files", {}).items():
        path = os.path.join(directory, filename)
        if not os.path.exists(path):
            problems.append(f"{filename} missing")
        elif sha256_file(path) != info["sha256"]:
            problems.append(f"{filename} checksum mismatch")
    return problems


def latest_backup(paths, system, name):
    sets = sorted(d for d in system.listdir(paths.backup_dir(name)) if re.match(r"^\d{8}T\d{6}Z$", d))
    return os.path.join(paths.backup_dir(name), sets[-1]) if sets else None


# [@ANCHOR: tenant_lib:restore_test]
# Verified by [@ANCHOR: test_tenant_lib:restore_test]
def restore_test(spec, paths, system):
    """Restores the newest backup into a scratch database, counts users and installed modules,
    drops the scratch database. Returns a dict; raises if the backup does not restore."""
    name = spec["name"]
    directory = latest_backup(paths, system, name)
    if directory is None:
        raise RuntimeError(f"no backup of {name} to test")
    problems = verify_backup(directory)
    if problems:
        raise RuntimeError(f"backup {directory} is damaged: {'; '.join(problems)}")
    scratch = f"restoretest_{name}"[:63]
    psql(system, f"DROP DATABASE IF EXISTS {scratch};")
    psql(system, f"CREATE DATABASE {scratch} TEMPLATE template0 ENCODING 'UTF8';")
    try:
        system.run_from_file(
            ["runuser", "-u", "postgres", "--", "pg_restore", "--no-owner", "--exit-on-error",
             "-d", scratch],
            os.path.join(directory, "db.dump"),
        )
        users = pg_scalar(system, "SELECT count(*) FROM res_users", scratch)
        modules = pg_scalar(system, "SELECT count(*) FROM ir_module_module WHERE state='installed'", scratch)
        with tarfile.open(os.path.join(directory, "filestore.tar.gz"), "r:gz") as tar:
            members = len(tar.getmembers())
    finally:
        psql(system, f"DROP DATABASE IF EXISTS {scratch};", check=False)
    if not users or int(users) < 1 or not modules or int(modules) < 1:
        raise RuntimeError(f"restore of {directory} produced an empty database")
    return {"backup": directory, "users": int(users), "installed_modules": int(modules),
            "filestore_members": members}


# [@ANCHOR: tenant_lib:status]
# Verified by [@ANCHOR: test_tenant_lib:status]
def tenant_status(spec, paths, system, http_get=None):
    """One dict per tenant: unit state, database size, filestore size, backup age, HTTP probes."""
    name = spec["name"]
    unit = f"hams-tenant@{name}.service"
    active = system.run(["systemctl", "is-active", unit], check=False).stdout.strip() or "unknown"
    size = pg_scalar(system, f"SELECT pg_database_size('{name}')")
    filestore = os.path.join(paths.data_dir(name), "filestore")
    fs_bytes = 0
    for root, _dirs, files in os.walk(filestore):
        for filename in files:
            try:
                fs_bytes += os.path.getsize(os.path.join(root, filename))
            except OSError:
                pass
    backup = latest_backup(paths, system, name)
    age_hours = None
    if backup:
        stamp = datetime.datetime.strptime(os.path.basename(backup), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
        age_hours = round((system.now() - stamp).total_seconds() / 3600, 1)
    probes = {}
    if http_get is not None and active == "active":
        # A catch-all tenant has no domain of its own: probe with the loopback host an operator uses.
        for domain in spec["domains"] or ["localhost"]:
            probes[domain] = http_get(spec["http_port"], domain)
    healthy = active == "active" and (not probes or all(code and code < 500 for code in probes.values()))
    return {
        "name": name,
        "unit": active,
        "domains": spec["domains"],
        "http_port": spec["http_port"],
        "database_bytes": int(size) if size else None,
        "filestore_bytes": fs_bytes,
        "last_backup": backup,
        "backup_age_hours": age_hours,
        "http": probes,
        "healthy": healthy,
    }


# [@ANCHOR: tenant_lib:upgrade_tenant]
# Verified by [@ANCHOR: test_tenant_lib:upgrade_tenant]
def build_upgrade_steps(spec, paths, system):
    """One tenant's module upgrade after an Odoo package upgrade: backup, stop, `odoo -u` as the
    tenant (no HTTP, no cron), start. Run for one tenant at a time; the next only when the
    previous one answers."""
    name = spec["name"]
    unit = f"hams-tenant@{name}.service"
    modules = ",".join(["base"] + [m for m in spec["modules"] + spec["extra_modules"] if m != "base"])
    return [
        Step("backup", f"back up {name} before the upgrade", lambda: backup_tenant(spec, paths, system)),
        Step("stop", f"systemctl stop {unit}", lambda: system.run(["systemctl", "stop", unit])),
        Step(
            "upgrade",
            f"odoo -u {modules} --stop-after-init as {os_user(name)}",
            lambda: system.run(
                ["runuser", "-u", os_user(name), "--", paths.odoo_bin, "--config", paths.conf_file(name),
                 "-d", name, "-u", modules, "--stop-after-init", "--no-http", "--max-cron-threads=0",
                 "--log-level=warn"]
            ),
        ),
        Step("start", f"systemctl start {unit}", lambda: system.run(["systemctl", "start", unit])),
    ]


# [@ANCHOR: tenant_lib:delete_tenant]
# Verified by [@ANCHOR: test_tenant_lib:delete_tenant]
def build_delete_steps(spec, paths, system, all_specs=None):
    """Steps that remove a tenant. A final backup comes first and cannot be skipped by this builder;
    the backup directory itself is never removed."""
    name = spec["name"]
    user = os_user(name)
    role = pg_role(name)
    steps = []
    steps.append(Step("final_backup", f"final backup of {name}", lambda: backup_tenant(spec, paths, system)))
    steps.append(
        Step(
            "stop",
            f"systemctl disable --now hams-tenant@{name}.service",
            lambda: system.run(["systemctl", "disable", "--now", f"hams-tenant@{name}.service"], check=False),
        )
    )
    steps.append(Step("drop_db", f"DROP DATABASE {name}", lambda: psql(system, f"DROP DATABASE IF EXISTS {name} WITH (FORCE);")))
    steps.append(Step("drop_role", f"DROP ROLE {role}", lambda: psql(system, f"DROP ROLE IF EXISTS {role};")))

    def remove_files():
        for path in (paths.conf_dir(name), paths.secret_dir(name), paths.data_dir(name),
                     paths.addons_dir(name), paths.dropin_dir(name)):
            system.remove_tree(path)
        system.remove_file(os.path.join(paths.spec_dir, f"{name}.json"))
        hba = pg_hba_file(system)
        if hba:
            system.remove_file(os.path.join(os.path.dirname(hba), paths.pg_hba_include_name, f"10-{name}.conf"))
            psql(system, "SELECT pg_reload_conf();")

    steps.append(Step("remove_files", f"remove configuration, secrets, data and addons of {name} (backups stay)", remove_files))
    steps.append(Step("remove_user", f"userdel {user}", lambda: system.run(["userdel", user], check=False)))
    remaining = [s for s in (all_specs or []) if s["name"] != name]
    nft_text = render_nft(remaining)
    steps.append(
        Step(
            "firewall",
            "rewrite the tenant firewall table without this tenant",
            lambda: (system.write_text(paths.nft_file, nft_text, 0o640, "root:root"),
                     system.run(["nft", "-f", paths.nft_file])),
        )
    )
    return steps
