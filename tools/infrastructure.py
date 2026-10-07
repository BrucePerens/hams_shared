#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Infrastructure Blueprint & Provisioning Engine
Serves as the Single Source of Truth for test.py and provision.py.
Supports environment scoping, lifecycle hooks, and precise runtime mount states.
"""

import compileall
import contextlib
import glob
import grp
import json
import logging
import multiprocessing
import os
import pwd
import tempfile
import struct
import re
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import secrets
import string
import base64
import hashlib
import urllib.parse
from datetime import datetime

_logger = logging.getLogger(__name__)


def _pg_version_sort_key(path):
    """
    Extracts the numeric PostgreSQL major-version directory component from a
    path like /usr/lib/postgresql/14/bin/psql (or the older X.Y form, e.g.
    9.6/bin/psql) and returns it as a tuple of ints for correct numeric
    ordering -- a bare lexicographic sort of these paths ranks "9.6" above
    "14" (string '9' > '1'), so `sorted(paths)[-1]` would silently pick the
    ancient 9.x binary over a genuinely newer major version whenever both
    happen to be installed side by side (e.g. mid-upgrade, or a box that
    accumulated packages across a distro upgrade without purging the old
    cluster). A version segment that doesn't parse as dot-separated integers
    sorts lowest, so it never wins over a well-formed version by accident.
    """
    version_str = path.split(os.sep)[-3]
    try:
        return tuple(int(part) for part in version_str.split("."))
    except ValueError:
        return (-1,)


# [@ANCHOR: infrastructure:get_pg_bin]
def get_pg_bin(name):
    """Locates PostgreSQL binaries dynamically across installed versions."""
    paths = glob.glob(f"/usr/lib/postgresql/*/bin/{name}")
    if paths:
        return sorted(paths, key=_pg_version_sort_key)[-1]
    res = shutil.which(name)
    if not res:
        for p in [f"/usr/bin/{name}", f"/usr/local/bin/{name}"]:
            if os.path.exists(p):
                return p
        raise FileNotFoundError(f"Could not find PostgreSQL binary: {name}")
    return res


def get_os_identifier():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.strip().split("=")[1].strip('"').lower()
    except OSError as e:
        _logger.debug("Ignored OSError reading /etc/os-release: %s", e)
    return "ubuntu"


def get_os_codename():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("VERSION_CODENAME="):
                    return line.strip().split("=")[1].strip('"').lower()
    except OSError as e:
        _logger.debug("Ignored OSError reading /etc/os-release: %s", e)
    return "jammy"


@contextlib.contextmanager
def micro_privilege(username):
    """
    Temporarily drops Effective privileges to the specified user using setresuid/setresgid.
    Restores Root privileges securely upon exiting the context block.

    Also drops (and restores) the process's supplementary group list. setresuid/setresgid alone
    only change the real/effective/saved uid and gid -- the separate supplementary-groups list
    (os.getgroups()) is untouched by either call, so a naive uid/gid-only drop leaves the
    "unprivileged" persona still carrying the original (root) process's group memberships for the
    whole yield block, defeating containment for anything gated on group membership (e.g. a group
    that owns sensitive files). Supplementary groups must be dropped *before* setresgid/setresuid
    (dropping the real/effective uid away from 0 first would lose the CAP_SETGID needed to change
    the group list at all) and restored only *after* both are set back to root.
    """
    if os.geteuid() != 0:
        yield
        return

    user_info = pwd.getpwnam(username)
    target_uid = user_info.pw_uid
    target_gid = user_info.pw_gid

    orig_ruid, orig_euid, orig_suid = os.getresuid()
    orig_rgid, orig_egid, orig_sgid = os.getresgid()
    orig_groups = os.getgroups()
    target_groups = os.getgrouplist(username, target_gid)

    try:
        os.setgroups(target_groups)
        os.setresgid(orig_rgid, target_gid, orig_sgid)
        os.setresuid(orig_ruid, target_uid, orig_suid)
        yield
    finally:
        os.setresuid(orig_ruid, orig_euid, orig_suid)
        os.setresgid(orig_rgid, orig_egid, orig_sgid)
        os.setgroups(orig_groups)


def _ensure_local_hostname(text, env_vars):
    """Fill `{LOCAL_HOSTNAME}` for a manifest entry that uses it (the /etc/hosts template).

    The /etc/hosts template used to list only localhost and the service aliases, so a host
    provisioned from it could no longer resolve its own name: every `sudo` then printed
    "unable to resolve host <name>" (found on hams1, 2026-10-03). Debian's convention is
    `127.0.1.1 <hostname>`.
    """
    if "{LOCAL_HOSTNAME}" in text and "LOCAL_HOSTNAME" not in env_vars:
        env_vars["LOCAL_HOSTNAME"] = socket.gethostname()


def format_env(text, env_vars):
    """Substitute `{VAR}`-style placeholders in `text` from `env_vars`.

    Real fix, 2026-09-12 (hams_shared/tools/ 326-finding discovery): this used to catch
    KeyError and silently return `text` unformatted, with a comment (and a now-corrected
    test) claiming that was needed so "a hook with an unresolved placeholder shouldn't
    crash the whole provisioning run." Checked directly against every real call site
    (grep for `format_env(` -- there are exactly four, all inside provision_static_files):
    none of them format a hook's own template text at all -- hooks are plain functions run
    AFTER a static file is written, never passed through format_env. All four real call
    sites format a `static_files` MANIFEST entry's `path`, `src`, `url`, or `content`, and
    every `{VAR}` any such entry actually references (DOMAIN, PDNS_API_KEY, HAMS_COM_DIR,
    HAMS_COMMUNITY_DIR, DEB_CODENAME, DEB_TARGET_ARCH_CPU) is unconditionally populated in
    env_vars before provision_static_files ever runs (DOMAIN/PDNS_API_KEY at their own
    fail-fast checks; HAMS_COM_DIR/HAMS_COMMUNITY_DIR unconditionally set, with fallback
    defaults, right before every provision_static_files call; DEB_CODENAME/
    DEB_TARGET_ARCH_CPU populated inline in provision_static_files itself just before use).
    A KeyError here therefore always means a real authoring bug (a typo'd variable name, or
    the one edge case where the `dpkg-architecture` probe for DEB_TARGET_ARCH_CPU failed) --
    silently returning the unformatted template used to mean writing a real file to disk
    with a literal, unresolved `{VAR}` in its path or body while reporting success, exactly
    the "no-bricking... but also no silent wrong state" failure shape hams-fail-fast-
    philosophy exists to prevent. write_env_files() elsewhere in this same file already
    gets this right for the analogous case (`if k in env_vars`, skipping rather than
    KeyError-catching) -- this now matches that convention instead of contradicting it.
    """
    if not text:
        return ""
    return text.format(**(env_vars or {}))


def safe_remove(path):
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            _logger.debug("OSError removing file: %s", e)


def apply_permissions(path, owner_str, mode_int, recursive=False):
    """chown and chmod `path`. With `recursive`, everything below `path` is also re-owned (not
    re-moded; symlinks are re-owned themselves and never followed), so that files an earlier run
    left behind under another account become the new owner's. Used for a daemon family's own state
    directories (MANIFEST "recursive_owner")."""
    uid, gid = -1, -1
    if owner_str:
        try:
            user, group = owner_str.split(":")
            uid = pwd.getpwnam(user).pw_uid
            gid = grp.getgrnam(group).gr_gid
        except KeyError as e:  # burn-ignore-os-account-probe
            _logger.warning("User/Group %s not found: %s", owner_str, e)

    def _apply(p):
        try:
            if uid != -1 and gid != -1:
                os.chown(p, uid, gid)
            if mode_int is not None:
                os.chmod(p, mode_int)
        except OSError as e:
            _logger.debug("Failed chown/chmod on %s: %s", p, e)

    _apply(path)
    if recursive and uid != -1 and gid != -1 and os.path.isdir(path) and not os.path.islink(path):
        for current, dirnames, filenames in os.walk(path, followlinks=False):
            for name in dirnames + filenames:
                try:
                    os.lchown(os.path.join(current, name), uid, gid)
                except OSError as e:
                    _logger.debug("Failed lchown on %s: %s", os.path.join(current, name), e)


# [@ANCHOR: infrastructure:hook_failure_tracking]
# Bug-hunt fix (2026-09-18, from night_shift_todo
# provisioning-silent-hook-failures-summary-328e3e45.md): several
# provisioning hooks and inline steps below (hook_install_kopia_binary,
# download_file, hook_generate_ssl, hook_build_rust_daemons, the RabbitMQ
# bindings step, etc.) deliberately swallow their own failures with
# `except Exception` + a log line, on the theory that one optional step
# failing shouldn't abort a whole provisioning run. That theory is right,
# but until now nothing ever surfaced those swallowed failures anywhere a
# human or an automated caller would actually see them -- provisioning
# reported plain success even when, say, the kopia backup binary never
# got installed. This module-level list plus the three functions below
# it are the "middle ground" the to-do asked for: collect every non-fatal
# failure as it happens, then print one impossible-to-miss summary at the
# end of the run without aborting it.
_hook_failures = []


def reset_hook_failures():
    """Clears the run-scoped list of recorded non-fatal hook failures.
    Call this once at the start of a provisioning run (provision_environment
    does) so failures from an earlier run -- or an earlier test -- don't
    leak into this run's summary."""
    _hook_failures.clear()


def record_hook_failure(name, exc):
    """Records a non-fatal failure for the end-of-run summary rendered by
    render_hook_failure_summary(). `name` identifies the failed step (a
    hook function's name, or a short label for an inline provisioning
    step such as "rabbitmq_bindings"); `exc` is the exception that was
    swallowed. Call sites keep their own existing _logger.warning call
    too -- this only adds the step to the summary, it doesn't replace
    per-call logging."""
    _hook_failures.append((name, str(exc)))


def get_hook_failures():
    """Returns a shallow copy of the failures recorded so far this run."""
    return list(_hook_failures)


def render_hook_failure_summary():
    """Builds the prominent end-of-run banner listing every hook failure
    recorded via record_hook_failure() since the last reset_hook_failures()
    call, or returns None if there were none. Provisioning does not abort
    for these (see hook_install_kopia_binary and friends), but this makes
    them impossible to miss in the run's own output even though the run
    continued."""
    if not _hook_failures:
        return None
    lines = [
        "=" * 72,
        "[!] PROVISIONING DEGRADED -- {} non-fatal step(s) failed and were "
        "skipped:".format(len(_hook_failures)),
    ]
    for name, msg in _hook_failures:
        lines.append(f"    - {name}: {msg}")
    lines.append("=" * 72)
    return "\n".join(lines)


def print_hook_failure_summary():
    """Prints render_hook_failure_summary()'s banner via _logger.error (so
    it survives logging configs that suppress INFO) if there were any
    recorded failures. Returns True if the run is degraded (there was at
    least one), False otherwise -- callers use this to decide on a
    DEGRADED exit code."""
    summary = render_hook_failure_summary()
    if summary is None:
        return False
    for line in summary.splitlines():
        _logger.error(line)
    return True


# [@ANCHOR: infrastructure:provision_plan]
# provision.py --plan (2026-10-03, after the round-2 production runbook found that a full
# `provision.py --hold-odoo` on hams1 would upgrade held packages, restart PostgreSQL, rotate a
# password and revert deployed daemons, with no way to see that beforehand). While a plan is active,
# every step that would change the host records what it would do here instead of doing it:
# provision.py's run_sys prints commands rather than running them, and each helper below that writes
# a file, creates a directory, links a unit or runs a hook checks _plan() first. Read-only probes
# (dpkg-query, apt-cache, psql SELECTs, pwd/grp lookups) still run, so the plan reflects the host.
# Plan lines never carry secret values: env files list their keys, credential files only their path.
_PLAN = None


class ProvisionPlan:
    def __init__(self):
        self.actions = []

    def record(self, kind, detail):
        self.actions.append((kind, detail))
        print(f"PLAN {kind}: {detail}", flush=True)


def _planning():
    return _PLAN is not None


def _plan(kind, detail):
    """Records `kind: detail` and returns True while a plan is active (the caller then skips the
    real action); returns False, recording nothing, during a real run."""
    if _PLAN is None:
        return False
    _PLAN.record(kind, detail)
    return True


@contextlib.contextmanager
def planning():
    """Activates plan mode for the duration of the block and yields the ProvisionPlan."""
    global _PLAN
    previous = _PLAN
    _PLAN = ProvisionPlan()
    try:
        yield _PLAN
    finally:
        _PLAN = previous


# [@ANCHOR: infrastructure:redact_command]
def redact_command(cmd):
    """A printable copy of a provisioning command line with password values masked: psql
    `-v <name>=<value>` variables whose name mentions "pass", "key" or "secret", and the password argument of
    `rabbitmqctl add_user|change_password <user> <password>`. Used for plan output and run logs."""
    out = [str(arg) for arg in cmd]
    for i, arg in enumerate(out):
        name, sep, _value = arg.partition("=")
        if sep and i > 0 and out[i - 1] == "-v" and any(w in name.lower() for w in ("pass", "key", "secret")):
            out[i] = f"{name}=<redacted>"
    if len(out) >= 4 and os.path.basename(out[0]) == "rabbitmqctl" and out[1] in ("add_user", "change_password"):
        out[3] = "<redacted>"
    return out


def _file_state(path, new_content):
    """'new', 'changed' or 'unchanged' for writing new_content (str) to path."""
    if not os.path.exists(path):
        return "new"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "unchanged" if f.read() == new_content else "changed"
    except (OSError, UnicodeDecodeError):
        return "changed"


def download_file(url, path, mode, env_vars):
    ua = env_vars.get(
        "SYSTEM_USER_AGENT",
        "HamsComSyncDaemon/1.0 (+https://crawler.hams.com)",
    )
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = response.read()
    except Exception as e:  # audit-ignore-catch-all
        _logger.warning("Network partition fallback safety hit fetching %s: %s", url, e)
        record_hook_failure(f"download_file:{url}", e)
        # A failed fetch must never destroy a copy that is already installed: on 2026-09-21 a
        # timed-out fetch truncated the good PostgreSQL package key to zero bytes, so every later
        # apt step failed until the next run. Keep the existing file and report the failure.
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return
        data = b""

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, mode)
    with open(fd, "wb") as f:
        f.write(data)


def hook_generate_ssl(env_vars, dest_dir, path, run_cmd_func):
    domain = env_vars.get("DOMAIN", "localhost")
    ssl_dir = path
    fullchain = os.path.join(ssl_dir, "fullchain.pem")
    privkey = os.path.join(ssl_dir, "privkey.pem")
    lotw = os.path.join(ssl_dir, "lotw_root.pem")
    if not os.path.exists(fullchain):
        try:
            run_cmd_func(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-nodes",
                    "-days",
                    "3650",
                    "-newkey",
                    "rsa:2048",
                    "-keyout",
                    privkey,
                    "-out",
                    fullchain,
                    "-subj",
                    f"/C=US/ST=CA/L=SF/O=Hams/CN={domain}",
                ]
            )
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("Failed to generate SSL certs: %s", e)
            record_hook_failure("hook_generate_ssl", e)
        if os.path.exists(fullchain):
            shutil.copy2(fullchain, lotw)


def hook_clear_pycache(env_vars, dest_dir, path, run_cmd_func):
    pycache = path
    daemons = (
        os.path.join(dest_dir, "opt/hams/daemons") if dest_dir else "/opt/hams/daemons"
    )
    if os.path.exists(pycache):
        for item in os.listdir(pycache):
            item_path = os.path.join(pycache, item)
            (
                shutil.rmtree(item_path, ignore_errors=True)
                if os.path.isdir(item_path)
                else safe_remove(item_path)
            )
    if os.path.isdir(daemons):
        compileall.compile_dir(daemons, quiet=1)


def hook_install_odoo_key(env_vars, dest_dir, path, run_cmd_func):
    out = (
        os.path.join(dest_dir, "usr/share/keyrings/odoo-archive-keyring.gpg")
        if dest_dir
        else "/usr/share/keyrings/odoo-archive-keyring.gpg"
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run_cmd_func(["gpg", "--dearmor", "-o", out, "--yes", path])
    safe_remove(path)


def hook_install_pg_key(env_vars, dest_dir, path, run_cmd_func):
    out = (
        os.path.join(dest_dir, "usr/share/keyrings/postgresql-keyring.gpg")
        if dest_dir
        else "/usr/share/keyrings/postgresql-keyring.gpg"
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run_cmd_func(["gpg", "--dearmor", "-o", out, "--yes", path])
    safe_remove(path)


# Debian's own dpkg-architecture CPU names -> kopia's own GitHub release
# asset-name suffix (github.com/kopia/kopia/releases -- kopia-<version>-linux-<suffix>.tar.gz).
# The two naming schemes only coincide by accident for arm64; amd64 vs x64 do not match at
# all. See _kopia_release_arch's own docstring for the real bug this closes.
_KOPIA_ARCH_SUFFIX_BY_DEB_ARCH = {
    "amd64": "x64",
    "arm64": "arm64",
    "armhf": "arm",
}


def _kopia_release_arch(env_vars):
    """
    Resolves this machine's real CPU architecture to kopia's own GitHub
    release asset-name suffix (e.g. "x64" for amd64, "arm64" for arm64,
    "arm" for armhf) -- NOT the same string as Debian's own
    dpkg-architecture naming, which this function starts from but then maps.

    Real bug found and fixed 2026-09-13 hardware-qualifying pi500-1 (a
    Raspberry Pi 500, aarch64): the kopia download URL and this hook's own
    internal tar-extraction path were both hardcoded to
    "kopia-0.23.1-linux-x64" (an x86_64 binary) unconditionally, regardless
    of the real machine architecture. `tar`/`chmod` both "succeeded" (they
    don't care what architecture a file is), but the installed
    `/usr/bin/kopia` was a genuine x86_64 ELF binary on an aarch64 box --
    confirmed directly (`file /usr/bin/kopia` reports "x86-64", and running
    it fails with "Exec format error") -- a silent failure that would only
    have surfaced later, whenever kopia was actually invoked for a real
    backup, not at provisioning time. Never caught on the dev box because
    it's genuinely x86_64, so the hardcoded suffix happened to be correct
    there by coincidence.
    """
    if "DEB_TARGET_ARCH_CPU" not in env_vars:
        res = subprocess.run(
            ["dpkg-architecture", "-q", "DEB_TARGET_ARCH_CPU"],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            env_vars["DEB_TARGET_ARCH_CPU"] = res.stdout.strip()
    deb_arch = env_vars.get("DEB_TARGET_ARCH_CPU", "")
    suffix = _KOPIA_ARCH_SUFFIX_BY_DEB_ARCH.get(deb_arch)
    if suffix is None:
        raise RuntimeError(
            f"No known kopia release asset for Debian architecture {deb_arch!r} -- "
            "refusing to guess and silently install a binary for the wrong "
            "architecture (see _kopia_release_arch's own docstring for why that's "
            "a real, previously-hit bug, not a hypothetical one). Add a real "
            "mapping entry to _KOPIA_ARCH_SUFFIX_BY_DEB_ARCH once kopia publishes "
            "a release for this architecture, or confirm one already exists under "
            "a different name at https://github.com/kopia/kopia/releases."
        )
    return suffix


def hook_install_kopia_binary(env_vars, dest_dir, path, run_cmd_func):
    try:
        kopia_arch = env_vars.get("KOPIA_ARCH") or _kopia_release_arch(env_vars)
        target_dir = os.path.join(dest_dir, "usr/bin") if dest_dir else "/usr/bin"
        os.makedirs(target_dir, exist_ok=True)
        run_cmd_func(
            [
                "tar",
                "-xzf",
                path,
                "-C",
                target_dir,
                "--strip-components=1",
                f"kopia-0.23.1-linux-{kopia_arch}/kopia",
            ]
        )
        run_cmd_func(["chmod", "+x", os.path.join(target_dir, "kopia")])
    except Exception as e:  # audit-ignore-catch-all
        _logger.warning("Kopia binary install failed: %s", e)
        record_hook_failure("hook_install_kopia_binary", e)
    safe_remove(path)


def hook_install_wkhtmltopdf(env_vars, dest_dir, path, run_cmd_func):
    """Installs the real, patched-Qt wkhtmltopdf build Odoo's own PDF reports need
    (report.render_qweb_pdf, e.g. Event: Registration Confirmation's attached ticket PDF -- a real
    production failure found live 2026-09-29, "Unable to find Wkhtmltopdf on this system"). Debian
    dropped the `wkhtmltopdf` package from its own repos entirely (confirmed live: `apt-cache
    policy wkhtmltopdf` shows no candidate at all on Debian 13/trixie) -- Odoo's own documentation
    points at this exact project's own GitHub releases instead, never a distro package, since a
    plain unpatched build can't render some layouts Odoo relies on ("with patched qt" in `wkhtmltopdf
    --version` confirms the right one installed). No Debian-13/trixie-specific build exists yet
    (project last released 2023-05-22); the bookworm (Debian 12) amd64 build installs and runs
    cleanly on trixie -- verified live, including a real report render, not just `--version`.
    Installed via `apt-get install -y <path>`, not a bare `dpkg -i`, so apt resolves the package's
    own declared Depends (xfonts-75dpi, xfonts-base, and the ordinary shared libraries already on
    any Odoo box) automatically instead of leaving them missing."""
    try:
        run_cmd_func(["apt-get", "install", "-y", path])
    except Exception as e:  # audit-ignore-catch-all
        _logger.warning("wkhtmltopdf install failed: %s", e)
        record_hook_failure("hook_install_wkhtmltopdf", e)
    safe_remove(path)


# [@ANCHOR: infrastructure:hook_create_pdns_sqlite_schema]
_PDNS_SQLITE_SCHEMA_PATH = "/usr/share/pdns-backend-sqlite3/schema/schema.sqlite3.sql"


def hook_create_pdns_sqlite_schema(env_vars, dest_dir, path, run_cmd_func, db_name="pdns.sqlite3",
                                   hook_name="hook_create_pdns_sqlite_schema"):
    """Creates the main PowerDNS instance's gsqlite3 database (empty:
    zones are created via the API, not by this schema load) from the
    pdns-backend-sqlite3 package's own reference schema. Found missing
    live on hams1, 2026-09-22: nothing in this codebase ever created
    this file, so pdns.service could bind port 53 (no schema needed for
    that) but its API returned 404 for every zone -- there was no
    database for it to have created any in. Idempotent: skips if the
    file already exists, matching every other hook here."""
    db_path = os.path.join(path, db_name)
    if os.path.exists(db_path):
        return
    schema = _PDNS_SQLITE_SCHEMA_PATH
    if not os.path.exists(schema):
        _logger.warning("pdns-backend-sqlite3's own schema file is missing: %s", schema)
        record_hook_failure(
            hook_name,
            FileNotFoundError(schema),
        )
        return
    try:
        # run_cmd_func passes **kwargs straight through to subprocess.run(),
        # which takes a real file object for stdin -- not a path.
        with open(schema, "rb") as schema_fh:
            run_cmd_func(["sqlite3", db_path], stdin=schema_fh)
        apply_permissions(db_path, "pdns:pdns", 0o664)
    except Exception as e:  # audit-ignore-catch-all
        _logger.warning("Failed to create PowerDNS sqlite schema: %s", e)
        record_hook_failure(hook_name, e)


def hook_create_callbook_sqlite_schema(env_vars, dest_dir, path, run_cmd_func):
    """The one PowerDNS that owns port 53 loads the callbook zone's database as its second
    backend, so the file must exist with the PowerDNS schema before pdns.service starts: a
    backend whose tables are missing makes lookups fail for every zone. callbook_dns_export
    fills it (publish() replaces rows in an existing database). Same schema, same idempotence.
    A host without pdns-backend-sqlite3 (a test host) has no schema file and needs no database:
    skipped quietly here, the main hook already reports a missing schema where pdns is used."""
    if not os.path.exists(_PDNS_SQLITE_SCHEMA_PATH):
        return
    hook_create_pdns_sqlite_schema(
        env_vars, dest_dir, path, run_cmd_func,
        db_name="callbook.sqlite3", hook_name="hook_create_callbook_sqlite_schema",
    )


# [@ANCHOR: infrastructure:hook_daemons_perms]
def hook_daemons_perms(env_vars, dest_dir, path, run_cmd_func):
    """Hands the freshly copied daemon tree to hams_com and makes it world-readable.

    Only entries still owned by root (what the copy just wrote, as root) change owner. A plain
    `chown -R` used to take /opt/hams/daemons/event_ai_enrichment/, ticket_triage_agent/ and
    backup_worker/pgbackrest_sidecar.py away from odoo, which owns them on purpose on hams1 (found
    by the round-2 production runbook, 2026-10-03). -h changes a symlink itself, never its target."""
    target = path
    if os.path.exists(target):
        run_cmd_func(
            ["find", "-P", target, "-user", "root", "-exec", "chown", "-h", "hams_com:hams_com", "{}", "+"]
        )
        run_cmd_func(["chmod", "-R", "a+rX", target])


# Where the buffering front end's site file lives, and what Debian's nginx package leaves enabled.
NGINX_TUNNEL_ORIGIN_CONF = "/etc/nginx/hams/hams-tunnel-origin.conf"
NGINX_SITES_ENABLED = "/etc/nginx/sites-enabled"
# Sites that must NOT stay enabled next to the front end: Debian's own `default` listens on 0.0.0.0:80
# (the Debian package enables and starts it on install), and `acme-only.conf` was a hand-made port-80
# Let's Encrypt challenge site from 2026-09-21 that nothing provisions. Port 80 is closed on hams1.
NGINX_SITES_TO_DISABLE = ("default", "acme-only.conf")


# [@ANCHOR: infrastructure:hook_enable_nginx_front_end]
def hook_enable_nginx_front_end(env_vars, dest_dir, path, run_cmd_func):
    """Turns on the loopback buffering reverse proxy in front of Odoo (hams_com
    nginx/prod/hams-tunnel-origin.conf, 127.0.0.1:8085, the Cloudflare tunnel's catch-all).

    Odoo's prefork HTTP workers cut any send that stalls for 2 seconds, which truncated 35 MB
    downloads to a browser behind the tunnel; a buffering proxy takes Odoo's reply at full speed.
    Found and installed by hand on hams1, 2026-10-05; this makes a fresh provision reproduce it.

    Links the site into sites-enabled, removes the sites that would bind a public port (see
    NGINX_SITES_TO_DISABLE), refuses to go further unless `nginx -t` passes, then enables nginx at
    boot and reloads (or starts) it. A failure is recorded as a degraded step, never raised."""
    sites_enabled = os.path.join(dest_dir, NGINX_SITES_ENABLED.lstrip("/")) if dest_dir else NGINX_SITES_ENABLED
    link = os.path.join(sites_enabled, "hams-tunnel-origin.conf")
    if not os.path.exists(path):
        _logger.warning("nginx front end not enabled: %s was not installed (no hams_com checkout?)", path)
        return
    try:
        os.makedirs(sites_enabled, exist_ok=True)
        for name in NGINX_SITES_TO_DISABLE:
            stale = os.path.join(sites_enabled, name)
            if os.path.lexists(stale):
                os.remove(stale)
        # The link target is the on-host path even when dest_dir stages the tree elsewhere.
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(NGINX_TUNNEL_ORIGIN_CONF, link)
        if dest_dir:
            return
        run_cmd_func(["/usr/sbin/nginx", "-t"])
        run_cmd_func(["systemctl", "enable", "nginx.service"])
        run_cmd_func(["systemctl", "reload-or-restart", "nginx.service"])
    except (OSError, subprocess.CalledProcessError) as e:
        _logger.warning("Could not enable the nginx front end: %s", e)
        record_hook_failure("hook_enable_nginx_front_end", e)


# Rust-crate daemons under daemons/ with no Python entry point -- each of
# these needs a real compiled release binary at target/release/<crate_name>
# before its systemd unit's ExecStart can run. Runs before
# hook_daemons_perms in the same directory entry's hook list so the
# perms fixup below also covers the freshly built binaries.
RUST_DAEMON_CRATES = [
    "hams_data_relay",
    "hams_relay_bridge",
    "hams_simulated_band",
    # hams_com daemons/hams_auth_gateway: the auth.hams.com certificate login gateway (LoTW
    # client certificates over TLS 1.2, TCP 443). See its README and hams_com
    # docs/deploy/LOTW_CA_CHAIN.md section 7.
    "hams_auth_gateway",
    # hams_com daemons/shack_console: builds the library and `shack_console_server`, the loopback static server
    # for https://hams.com/console (unit shack-console.service).
    "shack_console",
]


def hook_build_rust_daemons(env_vars, dest_dir, path, run_cmd_func):
    for crate in RUST_DAEMON_CRATES:
        manifest_path = os.path.join(path, crate, "Cargo.toml")
        if os.path.exists(manifest_path):
            try:
                # `--target-dir` pins the binary to <crate>/target/release/, where the systemd
                # units' ExecStart expects it; an inherited CARGO_TARGET_DIR (e.g. via `sudo -E`
                # from a Claude session, whose hams_com settings.json sets a shared build cache)
                # would otherwise strand it elsewhere.
                target_dir = os.path.join(path, crate, "target")
                run_cmd_func(["cargo", "build", "--release", "--manifest-path", manifest_path, "--target-dir", target_dir])
            except Exception as e:  # audit-ignore-catch-all
                _logger.warning("Rust daemon build failed for %s: %s", crate, e)
                record_hook_failure(f"hook_build_rust_daemons:{crate}", e)


# [@ANCHOR: infrastructure:build_cloudflared_ffi]
# Builds hams_open/daemons/cloudflared-ffi/libcloudflared.so for this host's CPU.
#
# Why. The `cloudflare` module's tests (TestCloudflareTunnelDaemon: edge traffic parsing,
# unauthorized bypass, websocket traffic) start the Go local HTTPS simulator through
# cloudflare/utils/cloudflare_daemon.py, which ctypes.CDLL-loads
# daemons/cloudflared-ffi/libcloudflared.so (the path is relative to that module, so the library
# must sit inside the tree under test). The library is a compiled, per-architecture artifact. It
# used to be committed to git as an x86-64 binary, so an arm64 test host (mac1's Lima VM, the
# Jetsons, the Pi) either had no library or received the dev box's x86-64 file and failed with
# "wrong ELF class". It is now built on the host that needs it, from the in-repo source.
#
# Network. The module is stdlib-only Go (empty go.sum, no `require`), so the build needs the OS
# package golang-1.24-go (Debian 13 and Ubuntu 24.04 ship it; an apt_packages entry) and a C
# compiler, and nothing else. The build environment is pinned so no step can reach a network:
# GOTOOLCHAIN=local, GOFLAGS=-mod=readonly, GOPROXY=off; -trimpath and -buildvcs=false keep build
# paths and VCS state out of the output. tools/build_cloudflared_ffi.py is the command-line entry.
CFFI_LIB_NAME = "libcloudflared.so"
# Debian and Ubuntu install golang-1.24-go here; it is not on PATH when golang-go is a different
# version (Ubuntu 24.04's golang-go is 1.22).
CFFI_GO_CANDIDATES = ("/usr/lib/go-1.24/bin/go", "/usr/local/go/bin/go")
CFFI_APT_PACKAGE = "golang-1.24-go"

# ELF e_machine values for the CPUs hams.com hosts run on.
CFFI_ELF_MACHINES = {62: "x86_64", 183: "aarch64", 40: "armv7l", 243: "riscv64"}
CFFI_PLATFORM_ALIASES = {"amd64": "x86_64", "arm64": "aarch64", "armv8l": "armv7l"}


def cloudflared_ffi_default_dir():
    """hams_open/daemons/cloudflared-ffi, found from this file's real location
    (hams_open/hams_shared/tools/)."""
    here = os.path.dirname(os.path.realpath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "daemons", "cloudflared-ffi"))


def cffi_host_machine():
    machine = platform.machine().lower()
    return CFFI_PLATFORM_ALIASES.get(machine, machine)


def cffi_elf_machine(path):
    """The CPU an ELF shared object was built for ("x86_64", "aarch64", ...), or None when the
    file is not a little-endian 64-bit-or-32-bit ELF object."""
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return None
    if len(head) < 20 or head[:4] != b"\x7fELF" or head[5] != 1:
        return None
    return CFFI_ELF_MACHINES.get(struct.unpack("<H", head[18:20])[0], "unknown")


def cffi_library_status(ffi_dir):
    """(ok, reason). ok means the library exists, was built for this CPU and is not older than
    its Go source; reason says what is wrong otherwise."""
    lib = os.path.join(ffi_dir, CFFI_LIB_NAME)
    if not os.path.exists(lib):
        return False, f"{lib} does not exist"
    built_for = cffi_elf_machine(lib)
    if built_for is None:
        return False, f"{lib} is not a readable ELF shared object"
    if built_for != cffi_host_machine():
        return False, f"{lib} was built for {built_for}, this host is {cffi_host_machine()}"
    lib_mtime = os.path.getmtime(lib)
    for src in ("main.go", "go.mod"):
        src_path = os.path.join(ffi_dir, src)
        if os.path.exists(src_path) and os.path.getmtime(src_path) > lib_mtime:
            return False, f"{lib} is older than {src}"
    return True, ""


def cffi_go_version(go):
    out = subprocess.run([go, "version"], capture_output=True, text=True, check=False).stdout
    match = re.search(r"go(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def cffi_required_go(ffi_dir):
    """The `go` directive of the module's go.mod, as (major, minor)."""
    with open(os.path.join(ffi_dir, "go.mod"), encoding="utf-8") as f:
        match = re.search(r"^go (\d+)\.(\d+)", f.read(), re.MULTILINE)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def cffi_find_go(ffi_dir):
    """The first installed Go that satisfies go.mod, or None."""
    need = cffi_required_go(ffi_dir)
    for go in [shutil.which("go")] + [c for c in CFFI_GO_CANDIDATES if os.path.exists(c)]:
        if go and cffi_go_version(go) >= need:
            return go
    return None


def cffi_build_env(base_env, cache_dir):
    env = dict(base_env)
    env.update({
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "-mod=readonly",
        "GOPROXY": "off",
        "CGO_ENABLED": "1",
        "GOCACHE": cache_dir,
        "GOPATH": os.path.join(cache_dir, "gopath"),
    })
    return env


def cffi_build(ffi_dir, run=subprocess.run):
    """Builds the library into ffi_dir (and the header beside it). Raises RuntimeError with the
    fix when a prerequisite is missing; the library is replaced only after a successful build."""
    if not os.path.exists(os.path.join(ffi_dir, "main.go")):
        raise RuntimeError(f"{ffi_dir}/main.go not found")
    go = cffi_find_go(ffi_dir)
    if go is None:
        need = ".".join(str(n) for n in cffi_required_go(ffi_dir))
        raise RuntimeError(
            f"no Go >= {need} installed; run: sudo apt-get install -y {CFFI_APT_PACKAGE} build-essential"
        )
    if shutil.which("gcc") is None:
        raise RuntimeError("no C compiler (cgo needs one); run: sudo apt-get install -y build-essential")
    with tempfile.TemporaryDirectory(prefix="cloudflared-ffi-build-") as work:
        out = os.path.join(work, CFFI_LIB_NAME)
        cmd = [go, "build", "-trimpath", "-buildvcs=false", "-buildmode=c-shared", "-o", out, "."]
        res = run(cmd, cwd=ffi_dir, env=cffi_build_env(os.environ, os.path.join(work, "cache")),
                  capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed:\n{res.stdout}{res.stderr}")
        if cffi_elf_machine(out) != cffi_host_machine():
            raise RuntimeError(f"built {out} is for {cffi_elf_machine(out)}, expected {cffi_host_machine()}")
        # Same directory first, then an atomic rename, so a reader never sees a partial file.
        for name in (CFFI_LIB_NAME, "libcloudflared.h"):
            tmp = os.path.join(ffi_dir, "." + name + ".new")
            shutil.copyfile(os.path.join(work, name), tmp)
            os.chmod(tmp, 0o755 if name == CFFI_LIB_NAME else 0o644)
            os.replace(tmp, os.path.join(ffi_dir, name))
    return os.path.join(ffi_dir, CFFI_LIB_NAME)




# [@ANCHOR: infrastructure:hook_build_cloudflared_ffi]
def hook_build_cloudflared_ffi(env_vars, dest_dir, path, run_cmd_func):
    """Builds hams_open/daemons/cloudflared-ffi/libcloudflared.so for this host's CPU.

    The `cloudflare` module's tunnel-daemon tests load that library from inside the tree under
    test (cloudflare/utils/cloudflare_daemon.py), and it used to be a committed x86-64 binary that
    arm64 test hosts could not load. The build is local and offline: stdlib-only Go source, the
    Debian/Ubuntu `golang-1.24-go` package (an apt_packages entry), no module or toolchain
    download. See tools/build_cloudflared_ffi.py. Never starts or contacts a tunnel."""
    community_dir = (env_vars or {}).get("HAMS_COMMUNITY_DIR")
    if not community_dir:
        return
    ffi_dir = os.path.join(community_dir, "daemons", "cloudflared-ffi")
    if not os.path.isdir(ffi_dir):
        return
    ok, _reason = cffi_library_status(ffi_dir)
    if ok:
        return
    try:
        _logger.info("[*] Built %s", cffi_build(ffi_dir))
    except (RuntimeError, OSError) as e:
        _logger.warning("libcloudflared.so build failed: %s", e)
        record_hook_failure("hook_build_cloudflared_ffi", e)


# [@ANCHOR: infrastructure:migrate_signing_key]
# hams_com's privilege-isolated signer daemons (daemons/relay_signer,
# daemons/subcarrier_signer) each own one
# Ed25519 key that used to live
# in /var/lib/odoo, 0600 odoo:odoo -- readable by any code running as the
# odoo OS user. Once the daemon's dedicated account and 0700 directory
# exist, the key moves there. Strict on purpose: the old key is deleted
# only after the new copy is written, owned by the daemon's account and
# stamped with the old file's mtime (each daemon derives the key's public
# "created at" from that mtime). If any of that fails the old key stays
# where it was and the failure is raised to the hook, so nothing is lost.
def _migrate_signing_key(old_path, new_path, owner_str):
    """Move one signing key from old_path to new_path, owned by owner_str
    (user:group) at mode 0600. Returns True if a key was moved. A no-op
    when there is no old key (a fresh server: the daemon generates its own
    on first start). When the new key already exists, an identical old
    copy left by an interrupted earlier run is removed; a different one is
    left alone and reported, because removing it could lose a key."""
    if not os.path.exists(old_path):
        return False
    with open(old_path, "rb") as f:  # audit-ignore-path
        raw = f.read()
    if os.path.exists(new_path):
        with open(new_path, "rb") as f:  # audit-ignore-path
            current = f.read()
        if current != raw:
            msg = f"{new_path} exists and differs from {old_path}"
            raise FileExistsError(msg)
        os.remove(old_path)
        return False
    user, group = owner_str.split(":")
    uid = pwd.getpwnam(user).pw_uid
    gid = grp.getgrnam(group).gr_gid
    old_stat = os.stat(old_path)
    tmp_path = new_path + ".migrating"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, uid, gid)
        os.write(fd, raw)
        os.fsync(fd)
    except BaseException:  # audit-ignore-catch-all
        # Remove the partial copy and re-raise unconditionally.
        os.close(fd)
        os.remove(tmp_path)
        raise
    os.close(fd)
    times = (old_stat.st_atime, old_stat.st_mtime)
    os.utime(tmp_path, times)
    os.rename(tmp_path, new_path)
    os.remove(old_path)
    return True


def _run_signing_key_migration(hook_name, key_filename, owner_str, dest_dir, path):
    old_path = os.path.join("/var/lib/odoo", key_filename)
    if dest_dir:
        old_path = os.path.join(dest_dir, old_path.lstrip("/"))
    new_path = os.path.join(path, key_filename)
    try:
        if _migrate_signing_key(old_path, new_path, owner_str):
            _logger.info("Migrated signing key %s to %s.", old_path, new_path)
    except (OSError, KeyError) as e:
        _logger.warning("%s: signing key not migrated: %s", hook_name, e)
        record_hook_failure(hook_name, e)


def hook_migrate_subcarrier_signing_key(env_vars, dest_dir, path, run_cmd_func):
    _run_signing_key_migration(
        "hook_migrate_subcarrier_signing_key",
        "hams_subcarrier_signing_ed25519.key",
        "hams_subcarrier_signer:hams_subcarrier_signer",
        dest_dir,
        path,
    )


# [@ANCHOR: infrastructure:shared_odoo_account_ratchet]
# Every systemd unit below whose [Service] section says `User=odoo` shares one OS account with the
# Odoo server and with every other unit on this list. daemon_key_manager gives each daemon its own
# 0600 key file under /opt/hams/etc/keys/, but every one of those files is owned by `odoo`, so any
# daemon running as `odoo` can read every other daemon's credential: the per-daemon keys give no
# isolation between these units. The units that already run under their own account
# (hams_subcarrier_signer, hams_relay_signer,
# hams_relay_ca, pdns, hams-auth) are the pattern a fix would extend.
#
# Bruce decided (2026-10-03, NIGHT_PLAN decision 86): a dedicated OS account per daemon family, fleet-wide,
# in phases, each rehearsed before any live change. The plan, the families and the phases are in hams_com
# docs/proposals/DAEMON_OS_ISOLATION_PLAN.md. Phase 1 moved ncvec.sync (FAMILY_ACCOUNT_UNITS below). This
# list is the countdown: test_infrastructure.py fails when a unit runs as `odoo` without being listed here
# (a new daemon must get its own account or be added here in a reviewed change), and when a listed unit no
# longer runs as `odoo` (remove it, so the list only shrinks as units move to their own accounts).
SHARED_ODOO_ACCOUNT_UNITS = {
    "adif.ingress.service": (
        "Writes the hams_com 770 directories adif_queue, adif_uploads and failed_input, which Odoo (a hams_com member) also "
        "uses. Moving it needs those directories' ownership and the Odoo side's access redesigned: not mechanical (plan section 13)."
    ),
    "adif.processor.service": (
        "Same directories as adif.ingress (adif_queue, adif_uploads, failed_input, hams_com 770, shared with Odoo): "
        "migrates together with it, after the ownership redesign (plan section 13)."
    ),
    "backup.worker.service": (
        "Writes /opt/hams/etc/keys today, so a family account cannot hold it until its key-directory write access is removed; "
        "its own phase, the pgbackrest sidecar (ADR-0103) already took the root part (plan section 13)."
    ),
    "code.review.sweep.service": (
        "Writes into a repository checkout (/opt/hams/hams_com/docs/code_review_reports) that does not exist on hams1; "
        "opt-in, linked but never enabled in production, so there is no live exposure to close first (plan section 13)."
    ),
    "credential.touch.timer.service": (
        "The agent-account shape: touches the AI agents' home directories through the Claude Code CLI wrapper, so it cannot "
        "take NoNewPrivileges and needs its own review before it can leave the shared account (plan section 13)."
    ),
    # AI correction of scraped hamfest listings; the model runs as hams_event_agent (see the unit's comment).
    "event.ai.enrichment.service": (
        "The agent-account shape (supervises a model that runs as hams_event_agent through the CLI wrapper, so no "
        "NoNewPrivileges); needs the same review as ticket.triage before it moves."
    ),
    # The key bootstrapper writes every daemon's key file, so it runs as the account that owns
    # them (odoo); it stays on this list in the final state too (the plan's Phase 5).
    "hams.daemon.keys.service": (
        "Privileged by design and permanent: it writes every daemon's key file (daemon_key_manager), so it runs as the "
        "account that owns the key directories and is a member of every family group. Hardened instead: see "
        "SANDBOX_BASELINE_EXEMPTIONS."
    ),
    "hams.data.relay.service": (
        "Relay and auth behavior: Bruce reviews and merges changes to the relay units himself (night rules), so it is not "
        "migrated unattended (plan section 13)."
    ),
    "hams.relay.bridge.service": (
        "Relay and auth behavior: Bruce reviews and merges changes to the relay units himself (night rules), so it is not "
        "migrated unattended (plan section 13)."
    ),
    "hams.simulated.band.service": (
        "Its credentials in /etc/hams-band are hand-placed odoo-owned files outside the key flow; an account change needs "
        "those files provisioned in the MANIFEST first (plan section 13)."
    ),
    "hams.simulated.bots.service": (
        "Credentials in /etc/hams-bots are hand-placed odoo-owned files outside the key flow, and the bots write "
        "/opt/hams/cache/whisper; both need MANIFEST ownership before an account change (plan section 13)."
    ),
    "hams.simulated.observer.service": (
        "Credentials in /etc/hams-bots are hand-placed odoo-owned files outside the key flow, and the observer writes "
        "/opt/hams/cache/whisper; migrates with the bots (plan section 13)."
    ),
    "stray.odoo.shell.detector.service": (
        "Privileged by design and permanent: it inspects other processes of the odoo account (a one-off odoo shell left "
        "running), which an account of its own, or ProtectProc=invisible, would hide from it."
    ),
    # Supervises the AI ticket-triage pass; the model itself runs as the dedicated nologin
    # hams_ai_agent account (see the unit's own comment). Same shape as credential.touch.timer.
    "ticket.triage.event.service": (
        "The agent-account shape: supervises the triage model that runs as hams_ai_agent through the CLI wrapper, so no "
        "NoNewPrivileges; needs its own review (plan section 13)."
    ),
    "ticket.triage.service": (
        "The agent-account shape: supervises the triage model that runs as hams_ai_agent through the CLI wrapper, so no "
        "NoNewPrivileges; needs its own review (plan section 13)."
    ),
}


def _is_server_unit(entry):
    """True for a unit file of the hams.com server (the prod or test environment). A unit of another host,
    such as the FCC sync on pi500-1, which has its own User=, is not part of the account ratchets."""
    return bool({"prod", "test"} & set(entry.get("environments", ["prod", "test"])))


def systemd_units_running_as(manifest, user):
    """Return the base names of the manifest's systemd .service files whose User= is `user`."""
    names = set()
    for entry in manifest["static_files"]:
        path = entry["path"]
        if not path.endswith(".service") or not _is_server_unit(entry):
            continue
        for line in (entry.get("content") or "").splitlines():
            if line.strip() == f"User={user}":
                names.add(os.path.basename(path))
                break
    return names


# [@ANCHOR: infrastructure:family_account_units]
# The units that have left the shared `odoo` account for a daemon family's own account (hams_com
# docs/proposals/DAEMON_OS_ISOLATION_PLAN.md). Each migrated unit is held to three rules by
# test_infrastructure.py: it runs as, and in the group of, its account; its ReadWritePaths= names only
# directories that account owns in this MANIFEST (a daemon account is in hams_com so that it can reach
# /opt/hams, and hams_com owns directories the family must not write, such as the ADIF queue; the
# sandbox's read-only mount is what keeps it out of them, so that list must stay narrow); and it loads
# only the environment files its daemon uses (every other file carries secrets the daemon never needed).
#
# `agent_sudo` names the one documented exception to those rules: a unit whose job is to reach the Claude
# Code CLI through `sudo -u <that account> run-hams-ai-agent-claude.sh`. `NoNewPrivileges=true` makes the
# kernel refuse sudo's uid switch (the reason credential.touch.timer and ticket.triage keep it off), so
# such a unit omits NoNewPrivileges, narrows CapabilityBoundingSet to CAP_SETUID/CAP_SETGID, and has the
# agent's home as its only ReadWritePaths= (the CLI, running as the agent, writes its session state
# there; the daemon's own account never writes it). The account's whole reach into the agent is one
# sudoers.d line naming the one wrapper. test_infrastructure.py holds such a unit to exactly that shape.
FAMILY_ACCOUNT_UNITS = {
    "ncvec.sync.service": {
        "user": "hamsd_ncvec_sync",
        "environment_files": frozenset({"common.env"}),
    },
    "pota.sync.service": {
        "user": "hamsd_activator_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "sota.sync.service": {
        "user": "hamsd_activator_sync",
        "environment_files": frozenset({"common.env"}),
    },
    "amsat.tle.sync.service": {
        "user": "hamsd_satellite_sync",
        "environment_files": frozenset({"common.env"}),
    },
    "event.cover.image.sync.service": {
        "user": "hamsd_event_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "arrl.hamfests.sync.service": {
        "user": "hamsd_event_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "electronicsfleamarket.sync.service": {
        "user": "hamsd_event_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "monthly.events.recheck.service": {
        "user": "hamsd_event_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "radio.history.events.sync.service": {
        "user": "hamsd_event_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "rac.events.sync.service": {
        "user": "hamsd_event_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "sm3cer.contest.sync.service": {
        "user": "hamsd_event_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "wa7bnm.contest.sync.service": {
        "user": "hamsd_event_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "au.acma.sync.service": {
        "user": "hamsd_country_sync",
        "environment_files": frozenset({"common.env"}),
    },
    "au.callsign.sync.service": {
        "user": "hamsd_country_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "br.anatel.sync.service": {
        "user": "hamsd_country_sync",
        "environment_files": frozenset({"common.env"}),
    },
    "de.bnetza.sync.service": {
        "user": "hamsd_country_sync",
        "environment_files": frozenset({"common.env"}),
    },
    "ised.canada.sync.service": {
        "user": "hamsd_country_sync",
        "environment_files": frozenset({"common.env"}),
    },
    "nz.rsm.sync.service": {
        "user": "hamsd_country_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "uk.ofcom.sync.service": {
        "user": "hamsd_country_sync",
        "environment_files": frozenset({"common.env"}),
    },
    "fcc.uls.sync.service": {
        "user": "hamsd_country_sync",
        "environment_files": frozenset({"common.env"}),
    },
    "au.pii.sync.service": {
        "user": "hamsd_au_pii",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "callbook.geo.enrich.service": {
        "user": "hamsd_callbook_geo",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "hamcall.idx.sync.service": {
        "user": "hamsd_hamcall_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "club.web.search.discovery.service": {
        "user": "hamsd_club_search",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "ses.inbound.mail.ingest.service": {
        "user": "hamsd_mail_ingest",
        "environment_files": frozenset({"common.env", "aws.env"}),
        # aws.env is placed by a person (the IAM identity's keys), not generated by provisioning.
        "operator_env_files": frozenset({"aws.env"}),
        "no_state": True,
    },
    "noaa-swpc-sync.service": {
        "user": "hamsd_space_weather",
        "environment_files": frozenset({"common.env"}),
    },
    "aprs.is.sync.service": {
        "user": "hamsd_aprs_sync",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "ham.dx.daemon.service": {
        "user": "hamsd_dx_daemon",
        "environment_files": frozenset({"common.env"}),
        "no_state": True,
    },
    "qrz.scraper.service": {
        "user": "hamsd_qrz_scraper",
        "environment_files": frozenset({"common.env", "rabbitmq.env"}),
        "no_state": True,
    },
    "gdpr.csv.export.service": {
        "user": "hamsd_gdpr_export",
        "environment_files": frozenset({"common.env", "redis.env"}),
        "no_state": True,
    },
    "pdns.sync.service": {
        "user": "hamsd_dns_sync",
        "environment_files": frozenset({"common.env", "pdns.env", "rabbitmq.env"}),
        "no_state": True,
    },
    # callbook.dns.export writes PowerDNS's own database directory, which belongs to `pdns`: `group_write` names the one path it may
    # write that its account does not own, the group the unit takes through SupplementaryGroups= (never account membership), and
    # test_infrastructure.py checks that directory is that group's, setgid and group-writable, and that the unit names the group.
    "callbook.dns.export.service": {
        "user": "hamsd_dns_export",
        "environment_files": frozenset({"common.env", "pdns.env"}),
        "group_write": {"/var/lib/powerdns/callbook": "pdns"},
    },
    "dx.firehose.service": {
        "user": "hamsd_dx_firehose",
        "environment_files": frozenset({"common.env", "db_app.env"}),
        "no_state": True,
        "no_key": True,
    },
    "club.crawl.service": {
        "user": "hamsd_club_crawl",
        "environment_files": frozenset({"common.env"}),
        "agent_sudo": "hams_ai_agent",
    },
    # The Web Bot Auth key directory publisher (daemons/web_bot_auth.py `refresh`): signs the public key directory with its
    # own key and writes the bundle hams.com serves. It holds no Odoo key and loads no environment file; odoo is in its group
    # only to READ the bundle (`odoo_reads`).
    "web.bot.auth.directory.service": {
        "user": "hamsd_web_bot_auth",
        "environment_files": frozenset(),
        "no_key": True,
        "odoo_reads": True,
    },
}


# [@ANCHOR: infrastructure:sandbox_baseline]
# The ADR-0070 sandbox every server unit carries, each directive with the one value that satisfies it
# (None: any value, because the empty `CapabilityBoundingSet=` is the strictest setting and the narrowed
# CAP_SETUID CAP_SETGID of an agent_sudo unit is the documented exception). A unit that cannot carry a directive is
# listed in SANDBOX_BASELINE_EXEMPTIONS with exactly the directives it lacks and the reason; test_infrastructure.py fails
# when a unit lacks a directive without being listed, and when a listed unit has since gained it (the list only shrinks).
SANDBOX_BASELINE = {
    "ProtectSystem": {"strict"},
    "ProtectHome": {"read-only", "true", "yes", "tmpfs"},
    "PrivateTmp": {"true", "yes"},
    "NoNewPrivileges": {"true", "yes"},
    "PrivateDevices": {"true", "yes"},
    "RestrictAddressFamilies": None,
    "CapabilityBoundingSet": None,
}

_ROOT_HOST_UNIT = (
    "A root unit that administers the host itself (%s), so it cannot give up the filesystem, device and capability "
    "access the sandbox removes. Not a daemon family: it holds no per-daemon key, and the isolation plan does not apply to it."
)
_B2_UNIT = (
    "A root unit that %s. It already has ProtectSystem=strict and ProtectHome, and a narrow ReadWritePaths; which of "
    "NoNewPrivileges, PrivateDevices, RestrictAddressFamilies and an empty CapabilityBoundingSet the kopia and database tooling "
    "tolerates has not been measured, so they stay exempt until the backup rehearsal measures them (docs/proposals/DAEMON_OS_ISOLATION_PLAN.md, Phase 5)."
)
_AGENT_UNIT = (
    "The agent-account shape: it reaches the Claude Code CLI through `sudo -u <agent> run-hams-ai-agent-claude.sh`, and "
    "NoNewPrivileges=true makes the kernel refuse sudo's uid switch. It keeps CapabilityBoundingSet to CAP_SETUID and CAP_SETGID "
    "and writes only the agent's home (FAMILY_ACCOUNT_UNITS, `agent_sudo`)."
)
SANDBOX_BASELINE_EXEMPTIONS = {
    "hams-pycache.service": (
        frozenset(SANDBOX_BASELINE),
        _ROOT_HOST_UNIT % "it compiles the bytecode of all of /opt/hams and re-owns /opt/hams/pycache to hams_com",
    ),
    "system-startup.service": (
        frozenset(SANDBOX_BASELINE),
        _ROOT_HOST_UNIT % "it issues `systemctl start` for the timed daemons at boot, which needs control of systemd"
    ),
    "hams-pgbackrest-backup.service": (
        frozenset(SANDBOX_BASELINE),
        _ROOT_HOST_UNIT % "the privileged pgbackrest sidecar of ADR-0103: PostgreSQL's data directory is 0700 postgres:postgres, so it runs as root and drops to postgres itself",
    ),
    "hams.db.local.backup.service": (
        frozenset(SANDBOX_BASELINE),
        _ROOT_HOST_UNIT % "it runs pg_dump as the postgres account through runuser, which needs root",
    ),
    "hams-tenant-firewall.service": (
        frozenset(SANDBOX_BASELINE),
        _ROOT_HOST_UNIT % "it loads nftables rules, which needs CAP_NET_ADMIN; the tenant design is retired (ADR 0106) and the unit goes with it",
    ),
    "hams-tenant-health.service": (
        frozenset(SANDBOX_BASELINE),
        _ROOT_HOST_UNIT % "tenant_ctl.py health checks every tenant's unit, HTTP answer and backup age as root",
    ),
    "hams-tenant-backup.service": (
        frozenset(SANDBOX_BASELINE),
        _ROOT_HOST_UNIT % "tenant_ctl.py backup dumps every tenant's database and filestore as root",
    ),
    "hams-tenant-restore-test.service": (
        frozenset(SANDBOX_BASELINE),
        _ROOT_HOST_UNIT % "tenant_ctl.py restore-test restores into a scratch database as root",
    ),
    "hams-b2-backup.service": (
        frozenset({"NoNewPrivileges", "PrivateDevices", "RestrictAddressFamilies", "CapabilityBoundingSet"}),
        _B2_UNIT % "backs up files that belong to other accounts and database dumps to Backblaze B2",
    ),
    "hams-b2-restore-test.service": (
        frozenset({"NoNewPrivileges", "PrivateDevices", "RestrictAddressFamilies", "CapabilityBoundingSet"}),
        _B2_UNIT % "restores sampled files and one database",
    ),
    "hams-b2-backup-check.service": (
        frozenset({"NoNewPrivileges", "PrivateDevices", "RestrictAddressFamilies", "CapabilityBoundingSet"}),
        _B2_UNIT % "checks the age of the last good backup and restore test",
    ),
    "hams.daemon.keys.service": (
        frozenset({"ProtectSystem"}),
        "ProtectSystem=full, not strict: the unit runs an `odoo shell` (filestore, Odoo logs and data, every key family's directory) "
        "and a read-only tree would have to enumerate all of them. /usr, /boot and /etc are read-only; it can never be what stops a "
        "key being written. It carries every other baseline directive and no capability.",
    ),
    "credential.touch.timer.service": (frozenset({"NoNewPrivileges"}), _AGENT_UNIT),
    "ticket.triage.service": (frozenset({"NoNewPrivileges"}), _AGENT_UNIT),
    "ticket.triage.event.service": (frozenset({"NoNewPrivileges"}), _AGENT_UNIT),
    "event.ai.enrichment.service": (frozenset({"NoNewPrivileges"}), _AGENT_UNIT),
    "club.crawl.service": (frozenset({"NoNewPrivileges"}), _AGENT_UNIT),
}


def systemd_unit_sandbox_gaps(manifest, unit_name):
    """The SANDBOX_BASELINE directives the MANIFEST unit file `unit_name` lacks: absent, or set to a value that does not
    satisfy the baseline. A directive written with an empty value counts as present (the empty CapabilityBoundingSet= drops
    every capability). The last assignment wins, as in systemd."""
    for entry in manifest["static_files"]:
        if os.path.basename(entry["path"]) != unit_name or not _is_server_unit(entry):
            continue
        text = entry.get("content") or ""
        service = text.split("[Service]", 1)[1] if "[Service]" in text else ""
        service = service.split("\n[", 1)[0]
        set_values = {}
        for line in service.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            set_values[key.strip()] = value.strip()
        gaps = set()
        for directive, accepted in SANDBOX_BASELINE.items():
            if directive not in set_values or (accepted is not None and set_values[directive] not in accepted):
                gaps.add(directive)
        return gaps
    raise KeyError(unit_name)


def systemd_unit_directive_values(manifest, unit_name, directive):
    """Every value of `directive` in the [Service] text of the MANIFEST unit file `unit_name`, one
    entry per whitespace-separated word for the path-list directives, in file order."""
    for entry in manifest["static_files"]:
        if os.path.basename(entry["path"]) != unit_name or not _is_server_unit(entry):
            continue
        values = []
        for line in (entry.get("content") or "").splitlines():
            line = line.strip()
            if line.startswith(f"{directive}="):
                values.extend(line.split("=", 1)[1].split())
        return values
    raise KeyError(unit_name)


MANIFEST = {
    "system_accounts": [
        {
            "user": "hams_com",
            "group": "hams_com",
            "home": "/opt/hams",
            "shell": "/bin/bash",
            "add_to_users": ["odoo"],
            "environments": ["prod", "test"],
        },
        {
            # The traversal group of the daemon family accounts (DAEMON_OS_ISOLATION_PLAN.md, Phase 2). A
            # family account is NOT in hams_com (whose group access reads the ADIF queue, uploads and
            # every other hams_com directory); each directory it must pass through carries an ACL entry
            # for this group with execute only (MANIFEST directories "acl"). No account lives here: the
            # entry exists to create the group, and each family account lists it in member_of.
            "user": "hams_traverse",
            "group": "hams_traverse",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "environments": ["prod", "test"],
        },
        {
            # daemons/ncvec_sync, Phase 1 of the per-family daemon accounts (hams_com
            # docs/proposals/DAEMON_OS_ISOLATION_PLAN.md): the one unit that runs as this account is
            # ncvec.sync, which writes only the three directories below. odoo joins this group so
            # daemon_key_manager can hand the group its key file (an unprivileged process may chgrp to
            # a group it belongs to and may not chown) and so odoo can read what the daemon writes.
            # member_of hams_com is what lets the account reach /opt/hams (0750), as pdns needs; the
            # unit's own ReadWritePaths= keeps it out of every hams_com-writable directory.
            "user": "hamsd_ncvec_sync",
            "group": "hamsd_ncvec_sync",
            "home": "/opt/hams/spool/ncvec",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/web_bot_auth.py: the publisher of the Web Bot Auth key directory that hams.com serves at
            # /.well-known/http-message-signatures-directory (night_shift_todo register-the-sync-daemons-as-a-cloudflare-signed-agent).
            # It alone holds the directory-signing private key, so a compromised Odoo worker can stop serving the
            # directory and cannot sign one. odoo joins this group to read the bundle it writes (never to write it).
            # Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_web_bot_auth",
            "group": "hamsd_web_bot_auth",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/club_crawl (hams_com docs/proposals/AUTOMATIC_CLUB_CRAWL.md), the automatic crawl of
            # every club website for repeaters and events. Its own account instead of `odoo`, so a hostile
            # page that got the crawler's Python process to misbehave could reach one API key (its own,
            # club_crawl_service_internal.key, which odoo hands the group) and nothing else: not the other
            # daemons' keys, not the database, Redis or RabbitMQ credentials. It writes no state on disk at
            # all (the crawl ledger and every per-site state are in Odoo). odoo joins this group for the
            # key hand-off (chgrp, never chown); member_of hams_com is what lets the account reach /opt/hams.
            # No home directory is created. Its one reach into another account is the sudoers.d grant to
            # run-hams-ai-agent-claude.sh as hams_ai_agent (club-crawl-claude-sandbox below); see
            # FAMILY_ACCOUNT_UNITS for why that makes it the one unit with an `agent_sudo` exception.
            "user": "hamsd_club_crawl",
            "group": "hamsd_club_crawl",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/pota_sync and daemons/sota_sync, Phase 2 of the per-family daemon accounts (hams_com
            # docs/proposals/DAEMON_OS_ISOLATION_PLAN.md): the two reference-data syncs that share activator_data_service_internal.key.
            # odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on
            # the directories it passes through are all it has, so it cannot read the ADIF queue.
            "user": "hamsd_activator_sync",
            "group": "hamsd_activator_sync",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/amsat_tle_sync, Phase 2 of the per-family daemon accounts (hams_com
            # docs/proposals/DAEMON_OS_ISOLATION_PLAN.md): the AMSAT TLE sync and its own key (satellite_sync_service_internal.key).
            # odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on
            # the directories it passes through are all it has.
            "user": "hamsd_satellite_sync",
            "group": "hamsd_satellite_sync",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/event_sync: the five event and contest syncs that share event_sync_service_internal.key. All are one-shot units that
            # fetch a public page or feed and push the result to Odoo; none writes a file. odoo joins this group for the key hand-off
            # (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_event_sync",
            "group": "hamsd_event_sync",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # The country callbook syncs (au.acma, au.callsign, br.anatel, de.bnetza, ised.canada, nz.rsm, uk.ofcom, fcc.uls), Phase 3 of the
            # per-family daemon accounts (hams_com docs/proposals/DAEMON_OS_ISOLATION_PLAN.md). They share callbook_sync_service_internal.key, so one account per
            # key is the boundary. au.pii.sync (personal data) is in its own account, hamsd_au_pii. odoo joins this group for the key hand-off
            # (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_country_sync",
            "group": "hamsd_country_sync",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/au_pii_sync: the Australian register's personal data. Its own account and its own key file (a second registry row for the
            # same Odoo service user as the country syncs, so the key can be rotated and revoked on its own) mean the country syncs cannot read this
            # unit's key or anything it writes. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_au_pii",
            "group": "hamsd_au_pii",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/ham_callbook_geo_enrich: the Census geocoder enrichment of callbook records. One key (callbook_geo_service_internal.key), one
            # account. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_callbook_geo",
            "group": "hamsd_callbook_geo",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/hamcall_idx_sync: the presence sync of HamCall's licensed hamcall.idx. The account owns /opt/hams/hamcall, where the licensed
            # file lives, so no other account (and not hams_com) reads it. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_hamcall_sync",
            "group": "hamsd_hamcall_sync",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/club_web_search_discovery: the grounded club search, which spends real Gemini money on each run (opt-in unit). One key
            # (club_web_search_discovery_service_internal.key), one account. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_club_search",
            "group": "hamsd_club_search",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/ses_inbound_mail_ingest: the inbound mail reader. It holds AWS credentials (aws.env, read by systemd, which the account itself
            # never opens), so it has its own account and key (mail_ingest_service_internal.key). odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_mail_ingest",
            "group": "hamsd_mail_ingest",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/noaa_swpc_sync: the NOAA space-weather poller, a long-running daemon. One key (space_weather_service.key), one account. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_space_weather",
            "group": "hamsd_space_weather",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/aprs_is_sync: the APRS-IS client, a long-running daemon. One key (aprs_sync_service_internal.key), one account. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_aprs_sync",
            "group": "hamsd_aprs_sync",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/ham_dx_daemon: the DX cluster telnet client, a long-running daemon. One key (dx_daemon_service.key), one account. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_dx_daemon",
            "group": "hamsd_dx_daemon",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/qrz_scraper: the QRZ scraper, a long-running RabbitMQ consumer. One key (onboarding_service_internal.key), one account. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_qrz_scraper",
            "group": "hamsd_qrz_scraper",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/gdpr_csv_export: the GDPR export builder, which handles members' personal data. Its own account and key
            # (gdpr_export_service_internal.key) mean no other daemon reads the key or anything the unit holds. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_gdpr_export",
            "group": "hamsd_gdpr_export",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/pdns_sync: the PowerDNS sync, a long-running RabbitMQ consumer that holds the PowerDNS API key. One key
            # (dns_api_service_internal.key), one account. odoo joins this group for the key hand-off (chgrp, never chown). Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_dns_sync",
            "group": "hamsd_dns_sync",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/callbook_dns_export: reads the masked callbook view over RPC with its own key (callbook_dns_export_service_internal.key)
            # and rewrites the callbook zone's SQLite database that PowerDNS serves. One key, one account. odoo joins this group for the key
            # hand-off (chgrp, never chown). The account is NOT a member of the `pdns` group: the unit alone names it (SupplementaryGroups=pdns),
            # so the group is held only while the export runs and only for its one writable directory, never by a login or any other process.
            "user": "hamsd_dns_export",
            "group": "hamsd_dns_export",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # daemons/dx_firehose: the live DX WebSocket, which holds a database connection (LISTEN/NOTIFY) as the application role and no Odoo key.
            # Its own account, and db_app.env instead of db.env, so it never holds the PostgreSQL superuser password. It has no key file, so odoo is not
            # in its group. Not in hams_com: the traversal group plus the ACL entries on the directories it passes through are all it has.
            "user": "hamsd_dx_firehose",
            "group": "hamsd_dx_firehose",
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod", "test"],
        },
        {
            # hams_com daemons/subcarrier_signer: the one account that can read the
            # subcarrier-attestation signing key (its 0700 directory below). odoo joins this
            # group only to reach the daemon's socket in /run/hams_subcarrier_signer (0750);
            # group membership gives it nothing on the 0700 key directory.
            "user": "hams_subcarrier_signer",
            "group": "hams_subcarrier_signer",
            "home": "/opt/hams/etc/subcarrier_signer",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "environments": ["prod", "test"],
        },
        {
            # hams_com daemons/relay_signer: same shape, for the relay-bridge Noise
            # attestation / transmit-grant key (a forged DENIAL could take a licensed
            # operator off the air).
            "user": "hams_relay_signer",
            "group": "hams_relay_signer",
            "home": "/opt/hams/etc/relay_signer",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "environments": ["prod", "test"],
        },
        # hams_com daemons/relay_ca (docs/proposals/CLOUD_HSM_CA_SIGNING.md): the three CA signer daemons, run on hams1
        # (the "ca_signer" host class, prod only) as ONE account each, so that a flaw in one can reach only its own Google
        # service-account key and its own state. Each account's 0700 home holds that signer's Google service-account key
        # file (gcp_service_account.json, 0600, written there by cloudkms_setup.py), signer.env, its SQLite state and its
        # hash-chained audit log. odoo joins the relay and identity groups only to reach their sockets in /run; nobody joins
        # the capability root's group (root alone may ask it).
        {
            "user": "hams_relay_ca",
            "group": "hams_relay_ca",
            "home": "/opt/hams/etc/relay_ca",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            "user": "hams_identity_ca",
            "group": "hams_identity_ca",
            "home": "/opt/hams/etc/identity_ca",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            "user": "hams_capability_ca",
            "group": "hams_capability_ca",
            "home": "/opt/hams/etc/capability_ca",
            "shell": "/usr/sbin/nologin",
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            # night_shift_todo/medium/service-account-creation-and-sudoers-sandboxes-are-
            # untracked-infra-c3a9f714.md: hand-provisioned on hams1 with no tracked source
            # anywhere -- confirmed directly, 2026-10-02 (`id hams_ai_agent`): uid 995, gid 986,
            # own dedicated group, no supplementary group membership, `nologin` shell (it is
            # never interactively logged into; `odoo` reaches it only through the narrow
            # sudoers.d grants below). Runs the ticket-triage `agy` import tool and its own
            # Claude Code CLI session -- see the `hams_ai_agent-mcp-servers`,
            # `odoo-agy-sandbox`, and `odoo-ai-agent-claude-sandbox` static_files entries below
            # for the matching sudoers.d grants, and the already-tracked
            # `run-hams-ai-agent-claude.sh`/`credential.touch.timer` entries elsewhere in this
            # MANIFEST for what runs under it. Prod-only: this account has no reason to exist on
            # the dev box's own local `hams_dev` environment.
            "user": "hams_ai_agent",
            "group": "hams_ai_agent",
            "home": "/home/hams_ai_agent",
            "shell": "/usr/sbin/nologin",
            "environments": ["prod"],
        },
        {
            # Sibling account, same gap, same reasoning as hams_ai_agent immediately above --
            # confirmed directly on hams1, 2026-10-02: uid 994, gid 985, own dedicated group, no
            # supplementary group membership, `nologin` shell. Deliberately a separate account
            # from hams_ai_agent (see agents/skills/local-resources/SKILL.md and
            # night_shift_todo/high/agy-shared-mcp-blanket-permission-crosses-repeater-import-
            # and-ticket-triage-b6e91f2a.md) so a compromised event-enrichment session, which
            # fetches untrusted third-party club-website content, can never pivot into
            # ticket-triage's own tools or data. See the `hams_event_agent-mcp-servers`,
            # `odoo-event-agent-sandbox`, and `odoo-event-agent-claude-sandbox` static_files
            # entries below for its matching sudoers.d grants. Prod-only, same reasoning as
            # hams_ai_agent above.
            "user": "hams_event_agent",
            "group": "hams_event_agent",
            "home": "/home/hams_event_agent",
            "shell": "/usr/sbin/nologin",
            "environments": ["prod"],
        },
        {
            # hams_com daemons/hams_auth_gateway (the auth.hams.com certificate login gateway):
            # its own unprivileged account, the only user that can read the TLS key and the
            # token secret under /etc/hams/auth (both 0640, group hams-auth). Binds TCP 443
            # through CAP_NET_BIND_SERVICE in its own unit (hams-auth-gateway.service below),
            # never as root. No other account joins its group. Its only other group is
            # hams_traverse: hams1's /opt/hams is mode 750 with an execute-only ACL for that group, and
            # the binary lives at /opt/hams/daemons/hams_auth_gateway/target/release, so without it
            # systemd cannot exec the service (the same finding as shack-console.service's, 2026-10-05).
            # It is not in hams_com. Prod-only: auth.hams.com exists only on the production server.
            "user": "hams-auth",
            "group": "hams-auth",
            "home": "/etc/hams/auth",
            "shell": "/usr/sbin/nologin",
            "member_of": ["hams_traverse"],
            "not_member_of": ["hams_com"],
            "environments": ["prod"],
        },
        {
            # hams_com daemons/hams_turn (turn.hams.com, the members-only TURN server, run by coturn): its own
            # unprivileged account, the only user that can read the TLS key and the shared secret under
            # /etc/hams/turn (0640, group hams-turn). Binds 443/udp and 443/tcp on one public address through
            # CAP_NET_BIND_SERVICE in hams-turn.service. It belongs to no other group: the binary is
            # /usr/bin/turnserver, which is world-executable, so it needs no traversal into /opt/hams. Prod-only.
            "user": "hams-turn",
            "group": "hams-turn",
            "home": "/etc/hams/turn",
            "shell": "/usr/sbin/nologin",
            "not_member_of": ["hams_com"],
            "environments": ["prod"],
        },
    ],
    "directories": [
        {
            # The pagerduty maintenance flag lives here (/etc/pagerduty/maintenance, written by root only, mode 0644).
            # 0755 so the monitors, which run as other users with a read-only /etc, can read it. See hams_open's
            # pager_duty/daemon/pagerduty_maintenance.py.
            "path": "/etc/pagerduty",
            "owner": "root:root",
            "provision_mode": "755",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "acl": ["g:hams_traverse:--x"],
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/etc",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "acl": ["g:hams_traverse:--x"],
            "environments": ["prod", "test"],
        },
        {
            # 0710, group hams_com: odoo has full access; the group may traverse and not list. A daemon
            # account in its own group (hamsd_<family>, a member of hams_com) can open the one key file
            # daemon_key_manager gave that group, and cannot list this directory or open any other
            # daemon's 0600 file. daemon_key_manager keeps this mode on every write
            # (KEY_ROOT_DIR_MODE in its models/key_registry.py).
            "path": "/opt/hams/etc/keys",
            "owner": "odoo:hams_com",
            "provision_mode": "710",
            "daemon_family_shared": True,
            "runtime_mount": "rw",
            "acl": ["g:hams_traverse:--x"],
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the ncvec_sync family (daemons/ncvec_sync, hamsd_ncvec_sync): owned by odoo, group = the one account that
            # consumes the keys in it, 0750 (Bruce, NIGHT_PLAN 226). odoo writes; the group may traverse and
            # read; nobody else may enter. The group is a group odoo is a member of: the account entry above
            # lists odoo in add_to_users, so provision.py adds odoo as a supplementary member of the group
            # (an unprivileged process may chgrp to a group it belongs to, and may not chown to another
            # user). The membership takes effect only after odoo.service and hams.daemon.keys.service
            # restart. daemon_key_manager re-asserts this owner, group and mode on every key write
            # (KEY_GROUP_DIR_MODE in its models/key_registry.py); this entry creates the directory first.
            "path": "/opt/hams/etc/keys/ncvec_sync",
            "owner": "odoo:hamsd_ncvec_sync",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the club_crawl family (daemons/club_crawl, hamsd_club_crawl): owned by odoo, group = the one account that
            # consumes the keys in it, 0750 (Bruce, NIGHT_PLAN 226). odoo writes; the group may traverse and
            # read; nobody else may enter. The group is a group odoo is a member of: the account entry above
            # lists odoo in add_to_users, so provision.py adds odoo as a supplementary member of the group
            # (an unprivileged process may chgrp to a group it belongs to, and may not chown to another
            # user). The membership takes effect only after odoo.service and hams.daemon.keys.service
            # restart. daemon_key_manager re-asserts this owner, group and mode on every key write
            # (KEY_GROUP_DIR_MODE in its models/key_registry.py); this entry creates the directory first.
            "path": "/opt/hams/etc/keys/club_crawl",
            "owner": "odoo:hamsd_club_crawl",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the activator_sync family (hamsd_activator_sync): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/activator_sync",
            "owner": "odoo:hamsd_activator_sync",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the satellite_sync family (hamsd_satellite_sync): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/satellite_sync",
            "owner": "odoo:hamsd_satellite_sync",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the event_sync family (hamsd_event_sync): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/event_sync",
            "owner": "odoo:hamsd_event_sync",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the country_sync family (hamsd_country_sync): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/country_sync",
            "owner": "odoo:hamsd_country_sync",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the au_pii family (hamsd_au_pii): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/au_pii",
            "owner": "odoo:hamsd_au_pii",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the callbook_geo family (hamsd_callbook_geo): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/callbook_geo",
            "owner": "odoo:hamsd_callbook_geo",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the hamcall_sync family (hamsd_hamcall_sync): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/hamcall_sync",
            "owner": "odoo:hamsd_hamcall_sync",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the club_search family (hamsd_club_search): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/club_search",
            "owner": "odoo:hamsd_club_search",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the mail_ingest family (hamsd_mail_ingest): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/mail_ingest",
            "owner": "odoo:hamsd_mail_ingest",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the space_weather family (hamsd_space_weather): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/space_weather",
            "owner": "odoo:hamsd_space_weather",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the aprs_sync family (hamsd_aprs_sync): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/aprs_sync",
            "owner": "odoo:hamsd_aprs_sync",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the dx_daemon family (hamsd_dx_daemon): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/dx_daemon",
            "owner": "odoo:hamsd_dx_daemon",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the qrz_scraper family (hamsd_qrz_scraper): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/qrz_scraper",
            "owner": "odoo:hamsd_qrz_scraper",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the gdpr_export family (hamsd_gdpr_export): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/gdpr_export",
            "owner": "odoo:hamsd_gdpr_export",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the dns_sync family (hamsd_dns_sync): owned by odoo, group = the one account that consumes
            # the keys in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/dns_sync",
            "owner": "odoo:hamsd_dns_sync",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Key directory of the dns_export family (hamsd_dns_export): owned by odoo, group = the one account that consumes
            # the key in it, 0750 (Bruce, NIGHT_PLAN 226); see the ncvec_sync entry above for the full reasoning.
            "path": "/opt/hams/etc/keys/dns_export",
            "owner": "odoo:hamsd_dns_export",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # backup_management/daemon/main.py's _run_pgbackrest_via_sidecar():
            # the backup worker daemon's own unit (NoNewPrivileges=true, ProtectSystem=strict)
            # cannot perform a real pgbackrest backup itself -- PostgreSQL's own
            # data directory is 0700 postgres:postgres. This spool directory is
            # how it hands that one operation off to hams-pgbackrest-backup's
            # privileged, unsandboxed sidecar instead of granting itself
            # standing root/postgres filesystem access. odoo:odoo 700: the sidecar runs as root before
            # its own internal `runuser -u postgres`, so it can read this
            # directory regardless of its mode: root is not bound by
            # permission bits.
            "path": "/opt/hams/backup_requests",
            "owner": "odoo:odoo",
            "provision_mode": "700",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # b2_backup host class (hams1): the non-secret configuration plus the root-only repository password
            # and B2 key file the operator places here. root:root 700 on purpose: the credentials must not sit in
            # an odoo-owned directory such as /opt/hams/etc/keys, where odoo could replace the file.
            "path": "/opt/hams/etc/b2_backup",
            "owner": "root:root",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "host_class": "b2_backup",
            "environments": ["prod"],
        },
        {
            # b2_backup state: kopia cache and config, staging of database dumps, status files, restore scratch.
            "path": "/var/lib/hams-b2-backup",
            "owner": "root:root",
            "provision_mode": "700",
            "runtime_mount": "rw",
            "host_class": "b2_backup",
            "environments": ["prod"],
        },
        {
            # hams_com daemons/subcarrier_signer: the private Ed25519 key, 0700, owned by the
            # dedicated account alone. The odoo OS user must have no path into this directory;
            # that is the point of the daemon. The hook moves a key generated by the older
            # in-Odoo code out of /var/lib/odoo.
            "path": "/opt/hams/etc/subcarrier_signer",
            "owner": "hams_subcarrier_signer:hams_subcarrier_signer",
            "provision_mode": "700",
            # Odoo never writes here (or reads; mode 0700 forbids it).
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_migrate_subcarrier_signing_key],
        },
        {
            # The public key, its creation time and the operator-created "revoked" marker are
            # not secret: a separate, world-readable sibling (never a child of the 0700
            # directory) that the daemon writes and Odoo reads directly.
            "path": "/opt/hams/etc/subcarrier_signer_public",
            "owner": "hams_subcarrier_signer:hams_subcarrier_signer",
            "provision_mode": "755",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            # hams_com daemons/relay_signer: same shape as subcarrier_signer above, holding the hams.com
            # capability issuer key. (The Ed25519 attestation key it used to hold is gone with the Noise
            # attestation, NIGHT_PLAN 131, 147; so is the hook that migrated it.)
            "path": "/opt/hams/etc/relay_signer",
            "owner": "hams_relay_signer:hams_relay_signer",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/etc/relay_signer_public",
            "owner": "hams_relay_signer:hams_relay_signer",
            "provision_mode": "755",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        # hams_com daemons/relay_ca, ca_signer host class (hams1): each signer's private state directory (its Google
        # service-account key file, signer.env, state.sqlite3, audit.jsonl; 0700, its own account only) and, for the
        # relay and identity signers, a public directory for its two CA certificates.
        {
            "path": "/opt/hams/etc/relay_ca",
            "owner": "hams_relay_ca:hams_relay_ca",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/etc/identity_ca",
            "owner": "hams_identity_ca:hams_identity_ca",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/etc/capability_ca",
            "owner": "hams_capability_ca:hams_capability_ca",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            # Retired separate tenant instances (hams_shared/tools/tenant_lib.py, ADR 0106), odoo_tenants host class only: the
            # per-tenant odoo.conf directories (each root:t_<name> 0750 once a tenant exists).
            "path": "/etc/hams-tenants",
            "owner": "root:root",
            "provision_mode": "755",
            "runtime_mount": "ro",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # Per-tenant admin and master passwords (root-only, never printed) and a copy of each
            # validated spec's secrets directory. /opt/hams is 0750 hams_com, so no tenant can reach it.
            "path": "/opt/hams/etc/tenants",
            "owner": "root:root",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # The validated spec of every tenant that exists (no secrets); tenant_ctl list reads it.
            "path": "/opt/hams/etc/tenants.d",
            "owner": "root:root",
            "provision_mode": "755",
            "runtime_mount": "ro",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # One directory per tenant, owned by that tenant's own account (data_dir and filestore).
            "path": "/var/lib/hams-tenants",
            "owner": "root:root",
            "provision_mode": "755",
            "runtime_mount": "rw",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # pg_dump and filestore archives of each tenant, root-only. Cover this directory in the
            # kopia snapshot set (docs/proposals/MULTI_TENANT_ODOO.md, "Backups").
            "path": "/opt/hams/backups/tenants",
            "owner": "root:root",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # Root-owned copies of the few hams_open modules a tenant may load (parking). Read-only
            # for the tenant, outside /opt/hams (which a tenant account cannot traverse).
            "path": "/usr/local/lib/hams-tenant-addons",
            "owner": "root:root",
            "provision_mode": "755",
            "runtime_mount": "ro",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # The relay CA certificates (relay_root.pem, relay_issuing.pem): public. ca_signer only.
            "path": "/opt/hams/etc/relay_ca_public",
            "owner": "hams_relay_ca:hams_relay_ca",
            "provision_mode": "755",
            "runtime_mount": "ro",
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            # The identity CA certificates (identity_root.pem, identity_operating.pem): public. ca_signer only.
            "path": "/opt/hams/etc/identity_ca_public",
            "owner": "hams_identity_ca:hams_identity_ca",
            "provision_mode": "755",
            "runtime_mount": "ro",
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            # hams_com ham_relay_bridge's client of the relay signer (models/relay_ca_client.py),
            # on every host that runs Odoo: the pinned PUBLIC relay_issuing.pem and an optional
            # client.json naming the signer's socket. Holds no CA key and no credential, ever.
            "path": "/opt/hams/etc/relay_ca_client",
            "owner": "odoo:odoo",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        # /opt/hams/nginx and its self-signed ssl/ pair: unused. nginx is not a public
        # front end; ingress is the Cloudflare Tunnel plus hams_auth_gateway on
        # auth.hams.com (hams_com docs/proposals/PROVISION_PRODUCTION_NOTES.md, "Update
        # 2026-10-03"). The only nginx on hams1 is the loopback buffering proxy on
        # 127.0.0.1:8085 ("Update 2026-10-05"), which needs no certificate. Nothing on
        # hams1 reads these files.
        {
            "path": "/opt/hams/nginx",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/nginx/ssl",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod"],
            "post_provision_hooks": [hook_generate_ssl],
        },
        {
            "path": "/opt/hams/odoo",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            # Execute-only traversal for the family accounts (like /opt/hams/downloads): uk.ofcom.sync
            # starts Chromium from the shared browser install below and must pass through here to reach
            # it. Nothing else in the cache is opened up: every other directory under it keeps its own
            # hams_com-only mode.
            "path": "/opt/hams/cache",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "acl": ["g:hams_traverse:--x"],
            "environments": ["prod", "test"],
        },
        {
            # The Playwright browser install shared by the daemons that drive a browser. A family account
            # (uk.ofcom.sync, hamsd_country_sync) reaches it by the same execute-only grant and READS the
            # world-readable browser files; it never writes here (a launch does not write to the install),
            # so it is not in that unit's ReadWritePaths= (a path the account cannot write is a
            # sandbox hole for nothing; test_infrastructure.py pins this).
            "path": "/opt/hams/cache/ms-playwright",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "acl": ["g:hams_traverse:--x"],
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/cache/whisper",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Speech models for the simulated-band bots (MANIFEST["model_files"]). Read-only at runtime:
            # root's provisioning fetch is the only writer.
            "path": "/opt/hams/models",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            # Speech models for the simulated-band bots (MANIFEST["model_files"]). Read-only at runtime:
            # root's provisioning fetch is the only writer.
            "path": "/opt/hams/models/piper",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            # Speech models for the simulated-band bots (MANIFEST["model_files"]). Read-only at runtime:
            # root's provisioning fetch is the only writer.
            "path": "/opt/hams/models/faster-whisper-tiny.en",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/pycache",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_clear_pycache],
        },
        {
            "path": "/opt/hams/spool",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "acl": ["g:hams_traverse:--x"],
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/adif_queue",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/adif_uploads",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # ncvec.sync's data directory (HAMS_NCVEC_DATA_DIR) and, below, the two directories
            # smart_download() makes for the daemon name "ncvec_sync": owned by the daemon's own
            # account (hamsd_ncvec_sync, Phase 1), 0750 so odoo, in its group, can read and not write.
            # recursive_owner moves files an earlier run left here as odoo to the account.
            "path": "/opt/hams/spool/ncvec",
            "owner": "hamsd_ncvec_sync:hamsd_ncvec_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Parent of the per-family Web Bot Auth key directories below: traversal only (execute for hams_traverse), so a family
            # account and the directory publisher can reach exactly the paths they are given.
            "path": "/opt/hams/spool/web_bot_auth_keys",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "acl": ["g:hams_traverse:--x"],
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_activator_sync account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync",
            "owner": "hamsd_activator_sync:hamsd_activator_sync",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_callbook_geo account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_callbook_geo",
            "owner": "hamsd_callbook_geo:hamsd_callbook_geo",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_club_crawl account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_club_crawl",
            "owner": "hamsd_club_crawl:hamsd_club_crawl",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_club_search account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_club_search",
            "owner": "hamsd_club_search:hamsd_club_search",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_country_sync account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_event_sync account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync",
            "owner": "hamsd_event_sync:hamsd_event_sync",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_ncvec_sync account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_ncvec_sync",
            "owner": "hamsd_ncvec_sync:hamsd_ncvec_sync",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_qrz_scraper account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_qrz_scraper",
            "owner": "hamsd_qrz_scraper:hamsd_qrz_scraper",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_satellite_sync account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_satellite_sync",
            "owner": "hamsd_satellite_sync:hamsd_satellite_sync",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the hamsd_space_weather account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/hamsd_space_weather",
            "owner": "hamsd_space_weather:hamsd_space_weather",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Web Bot Auth key of the odoo account: <name>.key (0600, only this account reads it) and <name>.jwk (0644, public). Mode 755 so
            # the publisher (hamsd_web_bot_auth, through the traversal parent above) can read the .jwk by its exact path; the key file
            # itself is unreadable to every other account.
            "path": "/opt/hams/spool/web_bot_auth_keys/odoo",
            "owner": "odoo:odoo",
            "provision_mode": "755",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # The Web Bot Auth publisher's private key (publisher.key, 0600: odoo, in the group, cannot read it), the public JWKs
            # (jwks/) and the signed bundle hams.com serves (directory.json, 0644 world-readable content, in a 0750 directory only the
            # group may enter).
            "path": "/opt/hams/spool/web_bot_auth",
            "owner": "hamsd_web_bot_auth:hamsd_web_bot_auth",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/ncvec_sync",
            "owner": "hamsd_ncvec_sync:hamsd_ncvec_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Wake-up spool for the event-driven AI ticket triage (Bruce, NIGHT_PLAN decision 222).
            # hams_helpdesk (inside odoo.service, which already has /opt/hams/spool read-write) drops
            # `ticket-<id>.json` here after a ticket commits; ticket.triage.path watches it and starts
            # ticket.triage.event.service, which consumes the files. odoo:odoo 0700: only the Odoo server
            # writes and only the triage supervisor (also odoo) reads; systemd's path unit runs as root and
            # is not bound by the mode. Prod only: the units that use it are prod only.
            "path": "/opt/hams/spool/ticket_triage",
            "owner": "odoo:odoo",
            "provision_mode": "700",
            "runtime_mount": "rw",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/failed_input",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "acl": ["g:hams_traverse:--x"],
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads/ncvec_sync",
            "owner": "hamsd_ncvec_sync:hamsd_ncvec_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/sota_sync",
            "owner": "hamsd_activator_sync:hamsd_activator_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads/sota_sync",
            "owner": "hamsd_activator_sync:hamsd_activator_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/amsat_tle_sync",
            "owner": "hamsd_satellite_sync:hamsd_satellite_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads/amsat_tle_sync",
            "owner": "hamsd_satellite_sync:hamsd_satellite_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/au_acma_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads/au_acma_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/br_anatel_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/de_bnetza_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads/de_bnetza_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads/ised_canada_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/uk_ofcom_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads/uk_ofcom_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/fcc_uls_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads/fcc_uls_sync",
            "owner": "hamsd_country_sync:hamsd_country_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/noaa_swpc_sync",
            "owner": "hamsd_space_weather:hamsd_space_weather",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads/noaa_swpc_sync",
            "owner": "hamsd_space_weather:hamsd_space_weather",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/test",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["test"],
        },
        {
            "path": "/etc/odoo",
            "owner": "odoo:odoo",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        # hams_com daemons/hams_auth_gateway: its configuration directory. Holds gateway.toml,
        # auth.crt/auth.key (installed by the certbot deploy hook) and gateway_token_secret,
        # none of which is in this public repository -- installing them stays a release step
        # (hams_com docs/proposals/PROVISION_PRODUCTION_NOTES.md, "the gateway replaces nginx on
        # auth.hams.com"). Readable only by root and the hams-auth group. Deliberately no
        # "runtime_mount" key: these files belong to the gateway alone and must never be
        # mounted into any other runtime.
        {
            "path": "/etc/hams/auth",
            "owner": "root:hams-auth",
            "provision_mode": "750",
            "environments": ["prod"],
        },
        {
            # The pinned ARRL LoTW trust anchors (gateway.toml's [[anchor.certificate]] files,
            # copied from hams_com nginx/prod/lotw_ca/ at release time).
            "path": "/etc/hams/auth/anchors/arrl_lotw",
            "owner": "root:hams-auth",
            "provision_mode": "750",
            "environments": ["prod"],
        },
        {
            # The pinned hams.com member-identity trust anchors (gateway.toml's hams_member
            # [[anchor.certificate]] files: Root A, Root B, the Identity Root and the Identity
            # Issuing CA, copied from hams_com daemons/relay_ca/public_certs/ at release time by
            # hams_com daemons/hams_auth_gateway/tools/install_config.py).
            "path": "/etc/hams/auth/anchors/hams_member",
            "owner": "root:hams-auth",
            "provision_mode": "750",
            "environments": ["prod"],
        },

        # hams_com daemons/hams_turn: coturn's configuration directory. Holds turnserver.conf (with the shared secret),
        # turn.crt and turn.key, none of which is in this public repository: the install steps in hams_com
        # docs/runbooks/TURN_GO_LIVE.md write them. Readable only by root and the hams-turn group, and never mounted
        # into any other runtime (no "runtime_mount" key).
        {
            "path": "/etc/hams/turn",
            "owner": "root:hams-turn",
            "provision_mode": "750",
            "environments": ["prod"],
        },

        {
            "path": "/var/lib/odoo/backups",
            "owner": "odoo:hams_com",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/tmp/odoo_test_home",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["test"],
        },
        {
            "path": "/var/log/redis",
            # redis-server's package creates this redis:adm 2750; provisioning used to reset it
            # to a world-readable redis:redis 755 (round-2 production runbook, 2026-10-03). It
            # is now created only if missing and an existing one is left as packaged.
            "preserve_existing": True,
            "owner": "redis:redis",
            "provision_mode": "755",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/var/lib/redis",
            "owner": "redis:redis",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/var/log/rabbitmq",
            "owner": "rabbitmq:rabbitmq",
            "provision_mode": "755",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/var/lib/rabbitmq",
            "owner": "rabbitmq:rabbitmq",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/var/log/postgresql",
            "owner": "postgres:postgres",
            "provision_mode": "755",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            # Moved here from "static_files" (bug found live on hams1,
            # 2026-09-23, mid-release: this entry has no content/src/url --
            # it exists purely for its post_provision_hooks, and belongs in
            # "directories", the list execute_hooks() actually iterates for
            # exactly that case (see execute_hooks' own for-loop). Sitting in
            # "static_files" instead meant provision_static_files() tried to
            # treat its bare path as a file to write content into, crashing
            # with IsADirectoryError the moment /var/lib/powerdns already
            # existed as a real directory (which it always does in
            # practice -- see the comment below on why). Created by the
            # pdns-server/pdns-backend-sqlite3 packages' postinst, so this
            # entry doesn't create the directory itself -- only its
            # post_provision_hooks. Found live on hams1, 2026-09-22: nothing
            # in this codebase ever created pdns.sqlite3 (the main instance's
            # gsqlite3 database, distinct from the callbook one below), so
            # pdns.service could bind port 53 but its REST API returned 404
            # for every zone -- no database existed for it to have created
            # any zone in.
            "path": "/var/lib/powerdns",
            "owner": "pdns:pdns",
            "provision_mode": "755",
            "runtime_mount": "rw",
            "environments": ["prod"],
            "post_provision_hooks": [hook_create_pdns_sqlite_schema],
        },
        {
            # Moved here from "static_files" alongside its own sibling
            # /var/lib/powerdns entry above -- same real bug, same crash
            # (found live on hams1, 2026-09-23, immediately after fixing the
            # first one): no content/src/url, so provision_static_files()
            # tried to os.open() this already-real directory too. This one
            # has no post_provision_hooks at all, so it was purely
            # misplaced -- apply_production_directories() (which DOES
            # iterate "directories") is now what creates/chowns/chmods it,
            # a safe os.makedirs(..., exist_ok=True) no-op since
            # the callbook schema hook below creates it.
            "path": "/var/lib/powerdns/callbook",
            "owner": "pdns:pdns",
            # setgid (leading 2): callbook_dns_export runs as User=hamsd_dns_export with
            # SupplementaryGroups=pdns, not User=pdns, so a file it creates
            # here only lands group=pdns if this directory's own group is
            # inherited -- setgid is what makes that automatic. Without it,
            # new files land group=hamsd_dns_export and pdns.service (running
            # as User=pdns Group=pdns, no supplementary groups) gets zero
            # access to its own database. Found live on hams1, 2026-09-22,
            # after fixing the group ownership by hand and still hitting
            # "attempt to write a readonly database" on every publish after
            # the first, once a WAL-mode connection had already been opened.
            "provision_mode": "2775",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_create_callbook_sqlite_schema],
        },
        {
            # Moved here from "static_files" -- same bug as the two pdns
            # entries above (found in the same pass, 2026-09-23, checking
            # for other static_files entries with no content/src/url before
            # they became the NEXT production crash): no
            # post_provision_hooks either, purely misplaced.
            # Licensed HamCall index (ADR-0092): hand-delivered by K6BP, never published,
            # used only to validate callsigns behind the scenes. Operator steps are in
            # daemons/hamcall_idx_sync/README.md ("Placing the licensed file").
            # Owned by hamsd_hamcall_sync (Phase 3 of the daemon accounts): the one daemon that reads the file runs as that
            # account, and no other account, hams_com included, can. recursive_owner re-owns a file an operator already placed.
            "path": "/opt/hams/hamcall",
            "owner": "hamsd_hamcall_sync:hamsd_hamcall_sync",
            "provision_mode": "750",
            "recursive_owner": True,
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            # hams_ai_agent's home directory -- see this MANIFEST's own "system_accounts" entry
            # for the account itself. `provision_system_accounts()`'s `useradd` call deliberately
            # never passes `-m` (matches the existing convention for every other account here,
            # e.g. hams_com, which also gets their home directories from a
            # "directories" entry rather than from useradd itself), so without this entry
            # re-running provisioning against a fresh host would create the account but never its
            # home directory. Mode and ownership confirmed directly on hams1, 2026-10-02
            # (`find /home/hams_ai_agent -maxdepth 0`): 0700, owned by the account itself -- rw
            # because the account's own Claude Code CLI session writes its refreshed
            # `.credentials.json` here (see credential.touch.timer's own ReadWritePaths).
            "path": "/home/hams_ai_agent",
            "owner": "hams_ai_agent:hams_ai_agent",
            "provision_mode": "700",
            "runtime_mount": "rw",
            "environments": ["prod"],
        },
        {
            # Sibling entry, same reasoning as hams_ai_agent's home directory immediately above.
            # Mode and ownership likewise confirmed directly on hams1, 2026-10-02.
            "path": "/home/hams_event_agent",
            "owner": "hams_event_agent:hams_event_agent",
            "provision_mode": "700",
            "runtime_mount": "rw",
            "environments": ["prod"],
        },
    ],
    "env_groups": {
        "db.env": [
            "DB_NAME",
            "POSTGRES_PASSWORD",
            "DB_PASS",
            "DB_HOST",
            "DB_PORT",
            "DB_USER",
        ],
        # The application database role only (not the PostgreSQL superuser password db.env also holds), for the
        # one daemon that connects to the database itself (dx.firehose). A host that has only db.env gets this file cut
        # from it by provision.py --daemon-family; a full run writes it from the same values.
        "db_app.env": ["DB_NAME", "DB_PASS", "DB_HOST", "DB_PORT", "DB_USER"],
        "pdns.env": [
            "PDNS_API_KEY",
            "PDNS_API_URL",
            "PDNS_ZONE_NAMESERVERS",
            "PDNS_PERSONAL_PARENT_ZONE",
        ],
        "odoo.env": [
            "ODOO_ADMIN_PASSWORD",
            "ODOO_SERVICE_PASSWORD",
            "ODOO_URL",
            "CLOUDFLARE_API_TOKEN",
            "CLOUDFLARE_ZONE_ID",
        ],
        "rabbitmq.env": ["RMQ_PASS", "RABBITMQ_HOST", "RMQ_PORT", "RMQ_USER"],
        # REDIS_USERNAME/REDIS_PASSWORD: the production Redis ACL user (see _redis_acl_include_content).
        # REDIS_URL carries the same credentials for hams_data_relay (Rust), which reads only a URL.
        "redis.env": ["REDIS_HOST", "REDIS_PORT", "REDIS_USERNAME", "REDIS_PASSWORD", "REDIS_URL"],
        "bridge.env": ["BRIDGE_API_KEY", "BRIDGE_STUN_BIND"],
        "smtp.env": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS"],
        # The values a daemon family's own unit needs and that are not secret, so that unit can load this
        # file and not core.env / db.env / odoo.env (which carry HAMS_CRYPTO_KEY, POSTGRES_PASSWORD,
        # ODOO_ADMIN_PASSWORD, CLOUDFLARE_API_TOKEN and more). The same keys also stay in their original
        # files, so no existing unit changes; the duplicates go when the last unit that needs them moves.
        "common.env": [
            "DOMAIN",
            "SYSTEM_USER_AGENT",
            "ODOO_URL",
            "DB_NAME",
        ],
        "core.env": [
            "DOMAIN",
            "SYSTEM_USER_AGENT",
            "SYSADMIN_EMAILS",
            "HAMS_CRYPTO_KEY",
            "CLOUDFLARE_TUNNEL_TOKEN",
            "PYTHONPYCACHEPREFIX",
            "DX_FIREHOSE_WS_PORT",
            "GEMINI_API_KEY",
            "GEMINI_MODEL",
            "PLAYWRIGHT_BROWSERS_PATH",
            "HAMS_PROVISION_MODE",
        ],
    },
    "static_files": [
        {
            "path": "/etc/apt/sources.list.d/odoo.list",
            "content": "deb [signed-by=/usr/share/keyrings/odoo-archive-keyring.gpg] https://nightly.odoo.com/19.0/nightly/deb/ ./\n",
            "owner": "root:root",
            "mode": "644",
            "environments": ["early_prod"],
        },
        {
            # Bug found live on hams1, 2026-09-22: this file must be named
            # to match systemd_pdns_override's `--config-name=gsqlite3`
            # (PowerDNS resolves that to `<config-dir>/pdns-<name>.conf`,
            # confirmed empirically against this same box's callbook
            # instance -- a dash, not the underscore this path used to
            # have, which meant pdns_server could never find this file at
            # all regardless of the (also missing, until now)
            # systemd_pdns_override pointing --config-dir at its directory.
            #
            # Public authoritative DNS (Bruce's decision 2026-10-03, hams_com
            # night_shift_questions/answered/personal-dns-zone-nameservers-and-
            # delegation-74b5b2fa.md): Cloudflare delegates u.{DOMAIN} (every
            # personal '<alias>.u.{DOMAIN}' zone) to ns1/ns2.{DOMAIN}, which are
            # this server, so port 53 here answers the internet. This file does
            # not open the firewall -- nothing in infrastructure.py manages ufw --
            # so on a host with ufw the operator runs `ufw allow 53/udp` and
            # `ufw allow 53/tcp` by hand. default-soa-content gives every zone
            # pdns_sync creates a real SOA instead of PowerDNS's built-in
            # 'a.misconfigured.dns.server.invalid'; existing zones keep theirs.
            # IPv4 only for now: listening on '::' too would fail pdns.service on
            # a host with IPv6 disabled (local-address-nonexist-fail).
            #
            # ONE PowerDNS owns port 53 and answers both the personal zones (backend
            # "main") and callbook.{DOMAIN} (backend "callbook", its own SQLite file,
            # written by callbook_dns_export). There is NO rate limiter in front
            # (Bruce, 2026-10-04: a Python proxy and dnsdist were both rejected; dnsdist
            # is the option only if we ever run more than one DNS server), so this file is
            # what keeps the public listener from being an amplifier, using only PowerDNS's
            # own documented settings (https://doc.powerdns.com/authoritative/settings.html):
            #   authoritative only: no recursion exists in this server, `resolver` stays unset
            #     (it is only for ALIAS lookups) and `expand-alias` stays at its default no;
            #     a name outside our zones gets REFUSED. primary/secondary=no, disable-axfr=yes
            #     and an empty allow-notify-from: no zone transfer or NOTIFY surface at all.
            #   lua-prequery-script (/opt/hams/etc/pdns-prequery.lua, the next entry): any query whose
            #     type we do not serve (ANY, AXFR/IXFR, anything exotic) gets a REFUSED reply no larger
            #     than the query. NOT in the settings documentation as a supported feature: it says
            #     "used internally for regression testing ... API not guaranteed to be stable", and
            #     `pdns_server --help` says "DO NOT USE". Chosen by Bruce 2026-10-04 anyway; the
            #     script is two lines of API (getQuestion, setRcode) and tested against the real
            #     binary, so a PowerDNS upgrade that changes it fails the tests, not production.
            #   any-to-tcp=yes: kept as a second line of defense for ANY.
            #   udp-truncation-threshold=1232 (PowerDNS's default, set explicitly): a callbook answer
            #     (~700 bytes) goes out in ONE UDP packet. Accepted by Bruce 2026-10-04: a forged
            #     55-byte query reflects ~700 bytes, about 14x, because single-packet lookups matter
            #     more and nothing limits the rate. Only the network (BCP 38 egress filtering by the
            #     host), TCP, or enforced DNS cookies address forged UDP sources; see the runbook.
            #   TCP caps (max-tcp-connections, per client, transactions per connection, idle
            #     timeout), version-string=anonymous, security-poll-suffix= (secpoll off),
            #     log-dns-queries=no (a callbook lookup names a person's callsign).
            # Tested against a real pdns_server in test_infrastructure.py (PdnsPublicConfigTests).
            "path": "/opt/hams/etc/pdns-gsqlite3.conf",
            "content": """\
launch=gsqlite3:main,gsqlite3:callbook
gsqlite3-main-database=/var/lib/powerdns/pdns.sqlite3
gsqlite3-main-dnssec=no
gsqlite3-callbook-database=/var/lib/powerdns/callbook/callbook.sqlite3
gsqlite3-callbook-dnssec=no
local-address=0.0.0.0
default-soa-content=ns1.{DOMAIN} hostmaster.{DOMAIN} 0 10800 3600 604800 3600
api=yes
api-key={PDNS_API_KEY}
webserver=yes
webserver-address=127.0.0.1
webserver-port=8081
webserver-allow-from=127.0.0.0/8,::1/128
dnsupdate=yes
allow-dnsupdate-from=127.0.0.0/8,::1/128
primary=no
secondary=no
disable-axfr=yes
allow-notify-from=
any-to-tcp=yes
udp-truncation-threshold=1232
lua-prequery-script=/opt/hams/etc/pdns-prequery.lua
max-tcp-connections=100
max-tcp-connections-per-client=5
max-tcp-transactions-per-conn=10
tcp-idle-timeout=5
version-string=anonymous
security-poll-suffix=
log-dns-queries=no
loglevel=4
""",
            "owner": "pdns:pdns",
            "mode": "640",
            "environments": ["prod"],
        },
        {
            # PowerDNS Authoritative's Lua "prequery" hook (settings reference: lua-prequery-script).
            # The only API used: p:getQuestion() -> qname, qtype (a number) and p:setRcode(), inside a pcall
            # so an API change fails open instead of SERVFAILing everything. The hook
            # cannot see the query class, opcode or flags; PowerDNS itself already answers a non-IN
            # class with a tiny NOTIMP/REFUSED, drops opcodes other than QUERY/NOTIFY/UPDATE, drops
            # malformed packets and answers NOTIFY/UPDATE with REFUSED (all measured, see the runbook).
            # The served set is what our zones can hold: SOA NS A AAAA CNAME MX TXT LOC SRV NAPTR SSHFP CAA
            # (ham_dns's record types and the callbook exporter's SOA/NS/TXT/LOC), plus DS, SVCB and HTTPS
            # which resolvers and browsers ask for any name and which PowerDNS answers with a small NODATA:
            # refusing those would make a resolver treat this server as broken. DNSKEY, RRSIG, NSEC and
            # friends are absent because the zones are unsigned (gsqlite3-*-dnssec=no).
            "path": "/opt/hams/etc/pdns-prequery.lua",
            "content": """\
-- Answer REFUSED, in a reply no larger than the query, to any query type we do not serve.
-- (Braces are doubled in this file because provisioning runs file contents through str.format.)
local served = {{
  [1] = true,    -- A
  [2] = true,    -- NS
  [5] = true,    -- CNAME
  [6] = true,    -- SOA
  [15] = true,   -- MX
  [16] = true,   -- TXT (the callbook answer)
  [28] = true,   -- AAAA
  [29] = true,   -- LOC (the callbook position record)
  [33] = true,   -- SRV
  [35] = true,   -- NAPTR
  [43] = true,   -- DS (asked at zone cuts; small NODATA, zones are unsigned)
  [44] = true,   -- SSHFP
  [64] = true,   -- SVCB
  [65] = true,   -- HTTPS
  [257] = true,  -- CAA
}}

local function refuse_unserved(p)
  local _, qtype = p:getQuestion()
  if served[qtype] then
    return false
  end
  p:setRcode(5)  -- REFUSED
  return true
end

-- Fail open: measured on 4.9.17, an error inside prequery makes PowerDNS answer SERVFAIL to EVERY
-- query (and a syntax error stops it starting), so a changed API must not take DNS down.
function prequery(p)
  local ok, handled = pcall(refuse_unserved, p)
  if ok then
    return handled
  end
  return false
end
""",
            "owner": "pdns:pdns",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # hams1 readiness audit 2026-10-04, row 19: Odoo's log rotated weekly, uncompressed, four kept, so
            # odoo-server.log.2 was 921 MB and .1 580 MB (0.6-0.9 GB a week at INFO). Daily, 14 kept, compressed
            # (delaycompress keeps the newest rotation readable by a tailing process), and a 300 MB size cap so a
            # noisy day rotates early. copytruncate because Odoo keeps its log file open. Braces are doubled
            # because provision_static_files formats content with str.format.
            "path": "/etc/logrotate.d/odoo",
            "content": """\
/var/log/odoo/*.log {{
    daily
    rotate 14
    maxsize 300M
    compress
    delaycompress
    copytruncate
    missingok
    notifempty
}}
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/etc/hosts",
            "content": """\
127.0.0.1 localhost
127.0.1.1 {LOCAL_HOSTNAME}
::1 localhost ip6-localhost ip6-loopback
127.0.0.1 postgres redis rabbitmq odoo powerdns daemon_dx_firehose
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["test"],
        },
        {
            "path": "/opt/hams/systemd/hams-pycache.service",
            "content": """\
[Unit]
Description=Hams.com PyCache JIT Compiler
Before=odoo.service

[Service]
Type=oneshot
User=root
Environment="PYTHONPYCACHEPREFIX=/opt/hams/pycache"
ExecStart=/bin/bash -c "/usr/bin/python3 -m compileall -q /opt/hams; chown -R hams_com:hams_com /opt/hams/pycache"
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/hams.daemon.keys.service",
            "content": """\
[Unit]
Description=Hams.com Daemon Key Bootstrapper
Requires=odoo.service
After=odoo.service

[Service]
# Hardened, but deliberately not at the ADR-0070 ProtectSystem=strict of the sync daemons (docs/proposals/DAEMON_OS_ISOLATION_PLAN.md,
# Phase 5; SANDBOX_BASELINE_EXEMPTIONS): the unit runs a full `odoo shell` as the account that owns every key directory, and a shell
# writes to places a read-only tree would have to enumerate (the filestore, the Odoo log and data directories, the key directories and
# every key family's directory). `full` makes /usr, /boot and /etc read-only and leaves /var and /opt/hams writable, so it can never
# be what stops a key from being written. No capability is kept: the key hand-off is a chgrp to a group the account is a member of,
# which needs none. NOT set: ProcSubset=pid, because psutil in Odoo reads /proc/meminfo.
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=odoo
Environment="ODOO_RC=/etc/odoo/odoo.conf"
Environment="HAMS_KEYS_DIR=/opt/hams/etc/keys"
EnvironmentFile=-/opt/hams/etc/odoo.env
EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
ExecStart=/bin/bash -c "DB={DB_NAME}; if [ -z \\"$$DB\\" ]; then DB=hams_test; fi; echo \\"r = env['daemon.key.registry'].action_force_provision_all(); env.cr.commit(); assert r['params']['type'] == 'success', r['params']['message']\\" | /usr/bin/python3 /usr/bin/odoo shell -c /etc/odoo/odoo.conf -d $$DB --no-http"
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/tmp/odoo.key",
            "url": "https://nightly.odoo.com/odoo.key",
            "owner": "root:root",
            "mode": "644",
            "environments": ["early_prod"],
            "post_provision_hooks": [hook_install_odoo_key],
        },
        {
            "path": "/tmp/pg.key",
            "url": "https://www.postgresql.org/media/keys/ACCC4CF8.asc",
            "owner": "root:root",
            "mode": "644",
            "environments": ["early_prod"],
            "post_provision_hooks": [hook_install_pg_key],
        },
        {
            "path": "/etc/apt/sources.list.d/pgdg.list",
            "content": "deb [signed-by=/usr/share/keyrings/postgresql-keyring.gpg] https://apt.postgresql.org/pub/repos/apt/ {DEB_CODENAME}-pgdg main\n",
            "owner": "root:root",
            "mode": "644",
            "environments": ["early_prod"],
        },
        {
            "path": "/tmp/kopia.tar.gz",
            "url": "https://github.com/kopia/kopia/releases/download/v0.23.1/kopia-0.23.1-linux-{KOPIA_ARCH}.tar.gz",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
            "post_provision_hooks": [hook_install_kopia_binary],
        },
        {
            "path": "/tmp/wkhtmltox.deb",
            "url": "https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_amd64.deb",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
            "post_provision_hooks": [hook_install_wkhtmltopdf],
        },

        {
            # The buffering reverse proxy between the Cloudflare tunnel and Odoo (hams_com
            # nginx/prod/hams-tunnel-origin.conf): 127.0.0.1:8085 only, `/` -> Odoo 8069, `/websocket` ->
            # 8072. Odoo's prefork HTTP workers cut a send that stalls 2 seconds, which truncated the
            # 35 MB relay installer for any browser behind the tunnel; Odoo's own source says to put a
            # buffering reverse proxy in front. The tunnel's catch-all service (Odoo tunnel form,
            # `cloudflare.tunnel.catch_all_service`) must be http://localhost:8085, a database
            # setting this file does not control. Installed by hand on hams1 2026-10-05; the hook
            # links it, drops Debian's default and any acme-only site (nothing may bind port 80) and
            # enables nginx. Self-hosters: Odoo needs a buffering reverse proxy in front, see
            # hams_shared docs/SELF_HOSTING_REVERSE_PROXY.md. Prod only; test hosts never need it.
            "src": "{HAMS_COM_DIR}/nginx/prod/hams-tunnel-origin.conf",
            "path": NGINX_TUNNEL_ORIGIN_CONF,
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
            "post_provision_hooks": [hook_enable_nginx_front_end],
        },
        {
            "src": "{HAMS_COM_DIR}/daemons",
            "path": "/opt/hams/daemons",
            # See _skip_deployed_copy(): on a deployed host --sync-daemons owns this tree.
            "deployed_by_sync_daemons": True,
            "owner": "hams_com:hams_com",
            "mode": "755",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_build_rust_daemons, hook_build_cloudflared_ffi, hook_daemons_perms],
        },
        {
            # The operator command for the pagerduty maintenance flag (start, end, status), from the pager_duty
            # module: a single stdlib-only file, so installing it anywhere is one copy. Root only for start and end.
            "src": "{HAMS_COMMUNITY_DIR}/pager_duty/daemon/pagerduty_maintenance.py",
            "path": "/usr/local/sbin/pagerduty-maintenance",
            "owner": "root:root",
            "mode": "755",
            "environments": ["prod", "test"],
        },
        {
            "src": "{HAMS_COMMUNITY_DIR}/hams_shared",
            "path": "/opt/hams/hams_shared",
            "owner": "hams_com:hams_com",
            "mode": "755",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_daemons_perms],
        },
        {
            # backup_management's daemon lives in hams_open (AGPL), not
            # hams_com's daemons/ tree, so it needs its own src -- the
            # {HAMS_COM_DIR}/daemons entry above only copies hams_com's
            # own daemons.
            "src": "{HAMS_COMMUNITY_DIR}/backup_management/daemon",
            "path": "/opt/hams/daemons/backup_worker",
            "deployed_by_sync_daemons": True,
            "owner": "hams_com:hams_com",
            "mode": "755",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_daemons_perms],
        },
        {
            "path": "/opt/hams/systemd/system-startup.service",
            "external_fetch": "its ExecStart starts amsat.tle.sync.service, an external fetch",
            "content": """\
[Unit]
Description=Run all timed daemons at startup
After=network.target

[Service]
Type=oneshot
ExecStart=/bin/systemctl start amsat.tle.sync.service
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/adif.processor.service",
            "content": """\
[Unit]
Description=Ham Radio ADIF Queue Processor (RabbitMQ Worker)
After=network.target rabbitmq-server.service
Requires=rabbitmq-server.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# /opt/hams/failed_input added 2026-09-23: main.py's save_failed_payload() writes a
# diagnostic metadata JSON file there for any job it can't process -- found live,
# itself failing with "Read-only file system" while handling an unrelated real
# failure, masking that failure's own error behind a second one.
ReadWritePaths=/opt/hams/spool/adif_queue /opt/hams/failed_input
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/adif_processor

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
# Found live on hams1, 2026-09-23: this unit was reading logbook_api_service_internal, the
# read-only public-API proxy account (ham_logbook.user_logbook_api_service) -- the same
# wrong-account mistake documented in ham_init/hooks.py's own comment on the retired
# lotw_eqsl_sync daemon. process_job() here writes ham.adif.queue.state and creates
# ham.qso records, both of which are hardcoded to require
# ham_logbook.user_logbook_sync_service specifically; every job failed with "You are not
# allowed to modify 'ADIF Processing Queue' records" and stayed pending forever.
Environment="ODOO_USER=logbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/logbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/adif_processor/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/adif_processor/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=adif.processor

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/backup.worker.service",
            "content": """\
[Unit]
Description=Asynchronous Backup Worker (RabbitMQ Consumer)
After=network.target rabbitmq-server.service
Requires=rabbitmq-server.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Broader than most daemons' ReadWritePaths by necessity: this is the
# same set backup_management/models/utils.py's validate_backup_path()
# already treats as legitimate backup/restore destinations at the
# application layer, plus pgBackRest's own state/log directories.
# A leading "-" means "skip if the directory does not exist": a fresh server has none of the optional
# destinations yet (no external backup disk is mounted), and without it systemd refuses to start
# the service at all (status 226/NAMESPACE, found on the first production release, 2026-09-21).
ReadWritePaths=/var/lib/odoo/backups -/var/lib/odoo/backup_repo -/var/backups/global -/opt/hams/backup /opt/hams/etc/keys -/mnt/backup /var/lib/pgbackrest /var/log/pgbackrest /opt/hams/backup_requests
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/backup_worker

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
EnvironmentFile=/opt/hams/etc/keys/backup_worker.env
Environment="PYTHONPATH=/opt/hams/daemons"

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/backup_worker/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/backup_worker/main.py

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=backup.worker

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # backup_management/daemon/pgbackrest_sidecar.py's own privileged half of
            # _run_pgbackrest_via_sidecar() (main.py). Deliberately NOT sandboxed like
            # backup.worker.service -- it needs to read PostgreSQL's own 0700
            # postgres:postgres data directory via `runuser -u postgres`, which
            # ProtectSystem=strict would forbid, the exact same reasoning
            # hams.db.local.backup.service's own comment already gives for pg_dump.
            # No EnvironmentFile=/Environment= on purpose: the only secrets it ever
            # handles (PGBACKREST_REPO1_S3_KEY/_SECRET) arrive per-request in the
            # spool file backup_worker writes, validated again by the sidecar script
            # itself before use -- this unit carries no standing credential of its
            # own. Started by hams-pgbackrest-backup.path below, not WantedBy=
            # multi-user.target -- a oneshot with nothing to keep running.
            "path": "/opt/hams/systemd/hams-pgbackrest-backup.service",
            "content": """\
[Unit]
Description=Privileged pgbackrest sidecar (runs real backups as postgres)
After=postgresql.service

[Service]
Type=oneshot
Environment="PYTHONPATH=/opt/hams/daemons"
ExecStart=/usr/bin/python3 /opt/hams/daemons/backup_worker/pgbackrest_sidecar.py
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.pgbackrest.sidecar
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # Watches backup_worker's own odoo-owned spool directory (see the
            # /opt/hams/backup_requests directory entry above) and starts the
            # privileged sidecar service the moment a request file appears --
            # this codebase's first .path unit; the symlink/enable provisioning
            # step below treats a .path exactly like a .timer (both are
            # [Install] WantedBy= activation units that need an explicit
            # `systemctl enable`, not just the symlink, to actually fire).
            "path": "/opt/hams/systemd/hams-pgbackrest-backup.path",
            "content": """\
[Unit]
Description=Trigger the privileged pgbackrest sidecar on a new backup request

[Path]
PathExistsGlob=/opt/hams/backup_requests/request-*.json
Unit=hams-pgbackrest-backup.service

[Install]
WantedBy=paths.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/dx.firehose.service",
            "content": """\
[Unit]
Description=Ham Radio Ultimate DX Cluster (Live Firehose Daemon)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_dx_firehose), Phase 4 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It serves a WebSocket on 127.0.0.1 and listens to PostgreSQL notifications and writes no file, so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=simple
User=hamsd_dx_firehose
Group=hamsd_dx_firehose
WorkingDirectory=/opt/hams/daemons/dx_firehose

# Code audit, 2026-10-04: dx_firehose/main.py reads the application database role (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS) for its LISTEN connection, its own DX_FIREHOSE_* settings (bind address 127.0.0.1 by default, port set in this unit) and the optional CF_ACCESS_* pair that no file sets. It holds no Odoo key. db_app.env carries the application role only: not the PostgreSQL superuser password that db.env also holds. No Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
EnvironmentFile=/opt/hams/etc/db_app.env
Environment="DX_FIREHOSE_WS_PORT=8765"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

LimitNOFILE=65535

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/dx_firehose/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/dx_firehose/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dx.firehose

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/ham.dx.daemon.service",
            "external_fetch": "holds a persistent telnet connection to a third-party DX cluster",
            "content": """\
[Unit]
Description=Ham Radio DX Cluster Telnet Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_dx_daemon), Phase 4 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It runs all the time, holds a DX cluster telnet connection and writes no file, so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=simple
User=hamsd_dx_daemon
Group=hamsd_dx_daemon
WorkingDirectory=/opt/hams/daemons/ham_dx_daemon

# Code audit, 2026-10-04: ham_dx_daemon/main.py reads DX_NODE_HOST, DX_NODE_PORT, DX_NODE_CALLSIGN and DX_DAEMON_MAX_PENDING_SPOTS (defaults in code, no environment file sets them) and it reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=dx_daemon_service"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/dx_daemon/dx_daemon_service.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/ham_dx_daemon/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/ham_dx_daemon/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ham.dx.daemon

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/noaa-swpc-sync.service",
            "external_fetch": "polls a government space-weather data service in a loop",
            "content": """\
[Unit]
Description=Ham Radio NOAA Space Weather Sync Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_space_weather), Phase 4 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It runs all the time and writes only its own spool and downloads directories (smart_download() with the daemon name noaa_swpc_sync). Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/noaa_swpc_sync /opt/hams/downloads/noaa_swpc_sync
Type=simple
User=hamsd_space_weather
Group=hamsd_space_weather
WorkingDirectory=/opt/hams/daemons/noaa_swpc_sync

# Odoo JSON2-RPC Credentials
# Code audit, 2026-10-04: noaa_swpc_sync/main.py reads SYSTEM_USER_AGENT and POLL_INTERVAL (set in this unit) and it reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=space_weather_service"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/space_weather/space_weather_service.key"
# noaa_swpc_sync/main.py polls at this interval (default 1800). NOAA publishes the Kp bins every three hours and the
# flux a few times a day, so 30 minutes keeps the homepage within a bin of the source without re-downloading.
Environment="POLL_INTERVAL=1800"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/noaa_swpc_sync/main.py --start-test

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_space_weather.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_space_weather
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_space_weather/hamsd_space_weather.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_space_weather/hamsd_space_weather.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_space_weather/hamsd_space_weather.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/noaa_swpc_sync/main.py $DAEMON_ARGS

# Resiliency
Restart=always
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/pdns.sync.service",
            "content": """\
[Unit]
Description=Ham Radio PowerDNS Sync Daemon (CQRS)
After=network.target rabbitmq-server.service pdns.service
Requires=rabbitmq-server.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_dns_sync), Phase 4 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It consumes RabbitMQ jobs and talks to the local PowerDNS API and writes no file, so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=simple
User=hamsd_dns_sync
Group=hamsd_dns_sync
WorkingDirectory=/opt/hams/daemons/pdns_sync

# Code audit, 2026-10-04: pdns_sync/main.py reads DOMAIN, the PDNS_* settings (the PowerDNS API key among them) and the RabbitMQ settings and it reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). pdns.env and rabbitmq.env are therefore loaded; no database or Redis credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
EnvironmentFile=/opt/hams/etc/pdns.env
EnvironmentFile=/opt/hams/etc/rabbitmq.env
Environment="ODOO_USER=dns_api_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/dns_sync/dns_api_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/pdns_sync/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/pdns_sync/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pdns.sync

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/amsat.tle.sync.service",
            "external_fetch": "downloads satellite orbital elements from a third-party site",
            "content": """\
[Unit]
Description=Ham Radio AMSAT TLE Sync Service
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_satellite_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It writes only its own spool and downloads directories (smart_download() with the daemon name amsat_tle_sync). Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/amsat_tle_sync /opt/hams/downloads/amsat_tle_sync
Type=oneshot
User=hamsd_satellite_sync
Group=hamsd_satellite_sync
WorkingDirectory=/opt/hams/daemons/amsat_tle_sync

# Code audit, 2026-10-04: amsat_tle_sync/main.py reads SYSTEM_USER_AGENT for its request headers and reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=satellite_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/satellite_sync/satellite_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/amsat_tle_sync/main.py --start-test

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_satellite_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_satellite_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_satellite_sync/hamsd_satellite_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_satellite_sync/hamsd_satellite_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_satellite_sync/hamsd_satellite_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/amsat_tle_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=amsat.tle.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/amsat.tle.sync.timer",
            "external_fetch": "activates amsat.tle.sync.service",
            "content": """\
[Unit]
Description=Run AMSAT TLE Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/hams.db.local.backup.service",
            "content": """\
[Unit]
Description=Nightly local pg_dump of the hams_prod database (interim safety net until the S3/B2 backup is configured)
After=postgresql.service

[Service]
# Deliberately not sandboxed like the sync daemons: it runs pg_dump as the postgres account and writes a root-only
# file under /opt/hams/backups, which ProtectSystem=strict would forbid. It holds no network access to speak of and
# reads only the local database socket.
Type=oneshot
# systemd expands $NAME itself, so every literal dollar sign is doubled ($$); %% likewise stands for a literal %.
ExecStart=/bin/bash -c 'set -euo pipefail; d=/opt/hams/backups/db-daily; install -d -m 700 $$d; f=$$d/hams_prod-$$(date -u +%%Y-%%m-%%d-%%H%%M).dump; runuser -u postgres -- pg_dump -Fc hams_prod > $$f.tmp; mv $$f.tmp $$f; chmod 600 $$f; ls -1t $$d/hams_prod-*.dump | tail -n +8 | xargs -r rm -f'
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.db.local.backup
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams.db.local.backup.timer",
            "content": """\
[Unit]
Description=Nightly local pg_dump of the hams_prod database

[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # Off-site backup to Backblaze B2 (hams_shared/tools/b2_backup.py; hams_com docs/runbooks/b2_backup.md).
            # Host class b2_backup (hams1 only) AND opt_in: provisioning links these units but enables nothing,
            # so a host without the B2 key never runs them. They upload to our own bucket and fetch nothing
            # from any third party. Not the ConditionPathExists-skips-silently trap: b2_backup.py check
            # (hams-b2-backup-check.timer) fails when no good backup was recorded recently.
            # The units run the root-owned deployed tree (/opt/hams/src/hams_open, written by
            # devbox_tools/deploy_to_production.py), not /opt/hams/hams_shared: on hams1 that one is a stale,
            # hams_com-owned copy without this tool, and a root job must not execute code another account can edit.
            "path": "/opt/hams/etc/b2_backup/config.json",
            "content": """\
{{
  "backend": {{
    "type": "s3",
    "bucket": "hams-com-prod-backups",
    "endpoint": "s3.us-east-005.backblazeb2.com",
    "region": "us-east-1",
    "prefix": "files/"
  }},
  "env_file": "/opt/hams/etc/b2_backup/b2_backup.env",
  "state_dir": "/var/lib/hams-b2-backup",
  "max_upload_bytes_per_sec": 5000000,
  "max_download_bytes_per_sec": 10000000,
  "retention": {{"daily": 14, "weekly": 8, "monthly": 12}},
  "paths": [
    {{"name": "filestore_hams_prod", "path": "/var/lib/odoo/.local/share/Odoo/filestore/hams_prod",
     "content_addressed": true}},
    {{"name": "etc", "path": "/opt/hams/etc", "exclude": ["b2_backup/b2_backup.env"]}},
    {{"name": "tenant_archives", "path": "/opt/hams/backups/tenants", "optional": true}},
    {{"name": "static_perens_com", "path": "/var/lib/hams-static/perens_com/static", "optional": true}}
  ],
  "tenant_spec_dir": "/opt/hams/etc/tenants.d",
  "tenant_data_root": "/var/lib/hams-tenants",
  "databases": ["hams_prod"],
  "pg_prefix": ["runuser", "-u", "postgres", "--"],
  "restore_test": {{"sample_files": 20, "verify_percent": 1, "database": true}}
}}
""",
            "owner": "root:root",
            "mode": "600",
            "host_class": "b2_backup",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams-b2-backup.service",
            "content": """\
[Unit]
Description=Encrypted off-site backup of hams1 files and database dumps to Backblaze B2 (kopia)
After=network-online.target postgresql.service hams-tenant-backup.service
Wants=network-online.target
ConditionPathExists=/opt/hams/etc/b2_backup/b2_backup.env
ConditionPathExists=/opt/hams/etc/b2_backup/config.json

[Service]
Type=oneshot
# Runs as root: it reads the odoo and tenant filestores and /opt/hams/etc, and runuser's to postgres for pg_dump.
# A nonzero exit (any source failed, password fingerprint changed, repository unreachable) fails this unit,
# which the pager_duty "Systemd Failed Services Tracker" check turns into an operator alert.
ProtectSystem=strict
ReadWritePaths=/var/lib/hams-b2-backup
ProtectHome=true
PrivateTmp=true
Nice=19
IOSchedulingClass=idle
IOSchedulingPriority=7
CPUQuota=100%
MemoryHigh=1G
MemoryMax=2G
TimeoutStartSec=4h
ExecStart=/usr/bin/python3 /opt/hams/src/hams_open/hams_shared/tools/b2_backup.py backup
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.b2.backup
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "b2_backup",
            "opt_in": "needs the B2 key and repository password placed on this host; see docs/runbooks/b2_backup.md",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams-b2-backup.timer",
            "content": """\
[Unit]
Description=Nightly off-site backup to Backblaze B2

[Timer]
# After hams.db.local.backup (02:30) and the tenant backup (03:10); well clear of the 00:00-00:20 UTC Odoo jobs.
OnCalendar=*-*-* 03:45:00 UTC
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "b2_backup",
            "opt_in": "needs the B2 key and repository password placed on this host; see docs/runbooks/b2_backup.md",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams-b2-restore-test.service",
            "content": """\
[Unit]
Description=Weekly restore test of the Backblaze B2 backups (sampled files and one database)
After=network-online.target postgresql.service
Wants=network-online.target
ConditionPathExists=/opt/hams/etc/b2_backup/b2_backup.env
ConditionPathExists=/opt/hams/etc/b2_backup/config.json

[Service]
Type=oneshot
# Restores a sample of files and one database into a scratch location under /var/lib/hams-b2-backup and a
# scratch database, compares, and removes both. Fails (and so alerts) on any mismatch.
ProtectSystem=strict
ReadWritePaths=/var/lib/hams-b2-backup
ProtectHome=true
PrivateTmp=true
Nice=19
IOSchedulingClass=idle
IOSchedulingPriority=7
CPUQuota=100%
MemoryHigh=1G
MemoryMax=2G
TimeoutStartSec=3h
ExecStart=/usr/bin/python3 /opt/hams/src/hams_open/hams_shared/tools/b2_backup.py restore-test
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.b2.restore.test
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "b2_backup",
            "opt_in": "needs the B2 key and repository password placed on this host; see docs/runbooks/b2_backup.md",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams-b2-restore-test.timer",
            "content": """\
[Unit]
Description=Weekly restore test of the Backblaze B2 backups

[Timer]
OnCalendar=Sun *-*-* 06:30:00 UTC
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "b2_backup",
            "opt_in": "needs the B2 key and repository password placed on this host; see docs/runbooks/b2_backup.md",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams-b2-backup-check.service",
            "content": """\
[Unit]
Description=Fail when the last good B2 backup or restore test is too old

[Service]
Type=oneshot
# Catches a timer that never fired or a unit skipped for a missing credential file, which a failed-unit
# alert cannot see. Reads only its own status files.
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ExecStart=/usr/bin/python3 /opt/hams/src/hams_open/hams_shared/tools/b2_backup.py check
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.b2.backup.check
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "b2_backup",
            "opt_in": "needs the B2 key and repository password placed on this host; see docs/runbooks/b2_backup.md",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams-b2-backup-check.timer",
            "content": """\
[Unit]
Description=Daily freshness check of the B2 backups

[Timer]
OnCalendar=*-*-* 09:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "b2_backup",
            "opt_in": "needs the B2 key and repository password placed on this host; see docs/runbooks/b2_backup.md",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/ses.inbound.mail.ingest.service",
            "external_fetch": "polls an external cloud mail landing zone and moves objects out of it",
            "content": """\
[Unit]
Description=Ham Radio SES Inbound Mail Ingest Service
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_mail_ingest), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It spools each message through a temporary file in the unit's private /tmp, so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_mail_ingest
Group=hamsd_mail_ingest
WorkingDirectory=/opt/hams/daemons/ses_inbound_mail_ingest

# Code audit, 2026-10-04: daemons/ses_inbound_mail_ingest/main.py reads SES_INBOUND_BUCKET and AWS_REGION (defaults in code), shells out to the aws CLI, which needs the AWS_* credentials of aws.env, and reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded; aws.env stays as the optional file it was.
EnvironmentFile=/opt/hams/etc/common.env
# Real IAM identity for the `odoo` user, resolved 2026-09-22 --
# AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY for the dedicated
# ses-inbound-mail-ingest-service IAM user (S3 read/delete/tag on
# incoming/*, write on processed/*+failed/*, scoped to
# hams-com-inbound-mail only -- see docs/proposals/EMAIL_SEND_RECEIVE.md).
# One-time provisioning: a person places this file, the daemon never touches it.
EnvironmentFile=-/opt/hams/etc/aws.env
Environment="ODOO_USER=mail_ingest_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/mail_ingest/mail_ingest_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/ses_inbound_mail_ingest/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/ses_inbound_mail_ingest/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=ses.inbound.mail.ingest
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/ses.inbound.mail.ingest.timer",
            "external_fetch": "activates ses.inbound.mail.ingest.service",
            "content": """\
[Unit]
Description=Poll SES Inbound Mail Every 2 Minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
Persistent=true

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # hams_com daemons/subcarrier_signer: a long-running signing helper, one
            # per key. odoo reaches it over the Unix socket in
            # /run/hams_subcarrier_signer (RuntimeDirectory, 0750; odoo is in that
            # group). It writes both its 0700 key directory and the public sibling, so
            # both are ReadWritePaths. SupplementaryGroups=hams_com lets it traverse
            # /opt/hams (0750 hams_com) to reach its code and directories.
            "path": "/opt/hams/systemd/hams-subcarrier-signer.service",
            "content": """\
[Unit]
Description=Privilege-isolated subcarrier-attestation signing helper
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/etc/subcarrier_signer /opt/hams/etc/subcarrier_signer_public
RuntimeDirectory=hams_subcarrier_signer
RuntimeDirectoryMode=0750
Type=simple
User=hams_subcarrier_signer
Group=hams_subcarrier_signer
SupplementaryGroups=hams_com
WorkingDirectory=/opt/hams/daemons/subcarrier_signer
UMask=0027

Environment="SUBCARRIER_SIGNER_BASE_DIR=/opt/hams/etc/subcarrier_signer"
Environment="SUBCARRIER_SIGNER_PUBLIC_DIR=/opt/hams/etc/subcarrier_signer_public"
Environment="SUBCARRIER_SIGNER_SOCKET_PATH=/run/hams_subcarrier_signer/signer.sock"

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/subcarrier_signer/main.py --start-test
ExecStart=/usr/bin/python3 /opt/hams/daemons/subcarrier_signer/main.py

Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.subcarrier.signer

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # hams_com daemons/relay_signer: a long-running signing helper, one
            # per key. odoo reaches it over the Unix socket in
            # /run/hams_relay_signer (RuntimeDirectory, 0750; odoo is in that
            # group). It writes both its 0700 key directory and the public sibling, so
            # both are ReadWritePaths. SupplementaryGroups=hams_com lets it traverse
            # /opt/hams (0750 hams_com) to reach its code and directories.
            "path": "/opt/hams/systemd/hams-relay-signer.service",
            "content": """\
[Unit]
Description=Privilege-isolated relay-bridge attestation and transmit-grant signing helper
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/etc/relay_signer /opt/hams/etc/relay_signer_public
RuntimeDirectory=hams_relay_signer
RuntimeDirectoryMode=0750
Type=simple
User=hams_relay_signer
Group=hams_relay_signer
SupplementaryGroups=hams_com
WorkingDirectory=/opt/hams/daemons/relay_signer
UMask=0027

Environment="RELAY_SIGNER_BASE_DIR=/opt/hams/etc/relay_signer"
Environment="RELAY_SIGNER_PUBLIC_DIR=/opt/hams/etc/relay_signer_public"
Environment="RELAY_SIGNER_SOCKET_PATH=/run/hams_relay_signer/signer.sock"

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/relay_signer/main.py --start-test
ExecStart=/usr/bin/python3 /opt/hams/daemons/relay_signer/main.py

Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.relay.signer

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # hams_com daemons/relay_ca (docs/proposals/CLOUD_HSM_CA_SIGNING.md): the Relay operating CA signer, on hams1 only
            # (host class ca_signer, prod). It builds each certificate under its fixed policy and sends only the digest
            # to Google Cloud KMS (HSM) with its OWN service-account key file, which can use exactly this one key to
            # sign. It listens on a Unix socket and answers only odoo (SO_PEERCRED). It
            # talks to Google (cloudkms.googleapis.com) and nothing else, hence external_fetch; opt_in because it is
            # inert until cloudkms_setup.py has written its key file and signer.env and the key has been imported, so
            # provisioning never enables or starts it. Skipped (not failed, not restart-looping) until then.
            "path": "/opt/hams/systemd/hams-relay-ca.service",
            "external_fetch": "calls Google Cloud KMS (cloudkms.googleapis.com) to sign",
            "opt_in": "needs its Google service-account key file and signer.env from cloudkms_setup.py, and the HSM key imported",
            "content": """\
[Unit]
Description=Relay operating CA signer (Google Cloud KMS HSM key; Unix socket for odoo only)
Wants=network-online.target
After=network-online.target
ConditionPathExists=/opt/hams/etc/relay_ca/signer.env
ConditionPathExists=/opt/hams/etc/relay_ca/gcp_service_account.json
ConditionPathExists=/opt/hams/etc/relay_ca_public/relay_issuing.pem

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
# Google over HTTPS, and the Unix socket.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/etc/relay_ca
RuntimeDirectory=hams_relay_ca
RuntimeDirectoryMode=0750
Type=simple
User=hams_relay_ca
Group=hams_relay_ca
SupplementaryGroups=hams_com
WorkingDirectory=/opt/hams/daemons/relay_ca
UMask=0077

Environment="RELAY_CA_PURPOSE=relay"
Environment="RELAY_CA_STATE_DIR=/opt/hams/etc/relay_ca"
Environment="RELAY_CA_PUBLIC_DIR=/opt/hams/etc/relay_ca_public"
Environment="RELAY_CA_SOCKET_PATH=/run/hams_relay_ca/signer.sock"
Environment="RELAY_CA_CREDENTIALS_FILE=/opt/hams/etc/relay_ca/gcp_service_account.json"
Environment="RELAY_CA_ALLOWED_USERS=odoo"
Environment="RELAY_CA_LEAF_DAYS=800"
# signer.env (written by cloudkms_setup.py, no secret in it): the KMS key version and the pinned public-key fingerprint.
EnvironmentFile=/opt/hams/etc/relay_ca/signer.env

# Smoketest Resource Verification (one read-only getPublicKey call to Google)
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/relay_ca/signer_daemon.py --start-test
ExecStart=/usr/bin/python3 /opt/hams/daemons/relay_ca/signer_daemon.py

Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.relay.ca

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            # hams_com daemons/relay_ca (docs/proposals/CLOUD_HSM_CA_SIGNING.md): the Identity operating CA signer, on hams1 only
            # (host class ca_signer, prod). It builds each certificate under its fixed policy and sends only the digest
            # to Google Cloud KMS (HSM) with its OWN service-account key file, which can use exactly this one key to
            # sign. It listens on a Unix socket and answers only odoo (SO_PEERCRED). It
            # talks to Google (cloudkms.googleapis.com) and nothing else, hence external_fetch; opt_in because it is
            # inert until cloudkms_setup.py has written its key file and signer.env and the key has been imported, so
            # provisioning never enables or starts it. Skipped (not failed, not restart-looping) until then.
            "path": "/opt/hams/systemd/hams-identity-ca.service",
            "external_fetch": "calls Google Cloud KMS (cloudkms.googleapis.com) to sign",
            "opt_in": "needs its Google service-account key file and signer.env from cloudkms_setup.py, and the HSM key imported",
            "content": """\
[Unit]
Description=Identity operating CA signer (Google Cloud KMS HSM key; Unix socket for odoo only)
Wants=network-online.target
After=network-online.target
ConditionPathExists=/opt/hams/etc/identity_ca/signer.env
ConditionPathExists=/opt/hams/etc/identity_ca/gcp_service_account.json
ConditionPathExists=/opt/hams/etc/identity_ca_public/identity_operating.pem

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
# Google over HTTPS, and the Unix socket.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/etc/identity_ca
RuntimeDirectory=hams_identity_ca
RuntimeDirectoryMode=0750
Type=simple
User=hams_identity_ca
Group=hams_identity_ca
SupplementaryGroups=hams_com
WorkingDirectory=/opt/hams/daemons/relay_ca
UMask=0077

Environment="RELAY_CA_PURPOSE=identity"
Environment="RELAY_CA_STATE_DIR=/opt/hams/etc/identity_ca"
Environment="RELAY_CA_PUBLIC_DIR=/opt/hams/etc/identity_ca_public"
Environment="RELAY_CA_SOCKET_PATH=/run/hams_identity_ca/signer.sock"
Environment="RELAY_CA_CREDENTIALS_FILE=/opt/hams/etc/identity_ca/gcp_service_account.json"
Environment="RELAY_CA_ALLOWED_USERS=odoo"
# signer.env (written by cloudkms_setup.py, no secret in it): the KMS key version and the pinned public-key fingerprint.
EnvironmentFile=/opt/hams/etc/identity_ca/signer.env

# Smoketest Resource Verification (one read-only getPublicKey call to Google)
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/relay_ca/signer_daemon.py --start-test
ExecStart=/usr/bin/python3 /opt/hams/daemons/relay_ca/signer_daemon.py

Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.identity.ca

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            # hams_com daemons/relay_ca (docs/proposals/CLOUD_HSM_CA_SIGNING.md): the Capability signing root signer, on hams1 only
            # (host class ca_signer, prod). It builds each certificate under its fixed policy and sends only the digest
            # to Google Cloud KMS (HSM) with its OWN service-account key file, which can use exactly this one key to
            # sign. It listens on a Unix socket and answers only root (SO_PEERCRED). It
            # talks to Google (cloudkms.googleapis.com) and nothing else, hence external_fetch; opt_in because it is
            # inert until cloudkms_setup.py has written its key file and signer.env and the key has been imported, so
            # provisioning never enables or starts it. Skipped (not failed, not restart-looping) until then.
            "path": "/opt/hams/systemd/hams-capability-ca.service",
            "external_fetch": "calls Google Cloud KMS (cloudkms.googleapis.com) to sign",
            "opt_in": "needs its Google service-account key file and signer.env from cloudkms_setup.py, and the HSM key imported",
            "content": """\
[Unit]
Description=Capability signing root signer (Google Cloud KMS HSM key; Unix socket for root only)
Wants=network-online.target
After=network-online.target
ConditionPathExists=/opt/hams/etc/capability_ca/signer.env
ConditionPathExists=/opt/hams/etc/capability_ca/gcp_service_account.json

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
# Google over HTTPS, and the Unix socket.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/etc/capability_ca
RuntimeDirectory=hams_capability_ca
RuntimeDirectoryMode=0750
Type=simple
User=hams_capability_ca
Group=hams_capability_ca
SupplementaryGroups=hams_com
WorkingDirectory=/opt/hams/daemons/relay_ca
UMask=0077

Environment="RELAY_CA_PURPOSE=capability"
Environment="RELAY_CA_STATE_DIR=/opt/hams/etc/capability_ca"
Environment="RELAY_CA_SOCKET_PATH=/run/hams_capability_ca/signer.sock"
Environment="RELAY_CA_CREDENTIALS_FILE=/opt/hams/etc/capability_ca/gcp_service_account.json"
Environment="RELAY_CA_ALLOWED_USERS=root"
Environment="RELAY_CA_CAPABILITY_ISSUERS_FILE=/opt/hams/etc/capability_ca/capability_issuers.json"
# signer.env (written by cloudkms_setup.py, no secret in it): the KMS key version and the pinned public-key fingerprint.
EnvironmentFile=/opt/hams/etc/capability_ca/signer.env

# Smoketest Resource Verification (one read-only getPublicKey call to Google)
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/relay_ca/signer_daemon.py --start-test
ExecStart=/usr/bin/python3 /opt/hams/daemons/relay_ca/signer_daemon.py

Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.capability.ca

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "ca_signer",
            "environments": ["prod"],
        },
        {
            # ADR 0106 (retired design): one instance of this template per tenant (hams-tenant@perens_com.service).
            # The account, database and limits come from the tenant spec (tenant_lib.py writes the
            # per-tenant drop-in with MemoryMax, CPUQuota, TasksMax and the allowed listening ports).
            # Fetches nothing from third parties on its own (a tenant's own outgoing mail is the
            # tenant operator's choice). Never named odoo@ : that collides with the Debian unit.
            "path": "/opt/hams/systemd/hams-tenant@.service",
            "content": """\
[Unit]
Description=Odoo tenant instance %i (own user, database, filestore and ports; ADR 0106, retired design)
After=network.target postgresql.service hams-tenant-firewall.service
Wants=postgresql.service hams-tenant-firewall.service
ConditionPathExists=/etc/hams-tenants/%i/odoo.conf

[Service]
Type=simple
User=t_%i
Group=t_%i
UMask=0027
WorkingDirectory=/var/lib/hams-tenants/%i
Environment=HOME=/var/lib/hams-tenants/%i
Environment=PYTHONPYCACHEPREFIX=/var/lib/hams-tenants/%i/pycache
ExecStart=/usr/bin/odoo --config /etc/hams-tenants/%i/odoo.conf
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30
# ADR-0070 OS-level restriction, plus: a tenant sees only its own data directory and cannot see
# hams_prod's processes, configuration, filestore or the hams daemons' files.
ProtectSystem=strict
ReadWritePaths=/var/lib/hams-tenants/%i
ProtectHome=true
InaccessiblePaths=/opt/hams /var/lib/odoo /etc/odoo /var/log/odoo /var/lib/postgresql /var/lib/redis /var/lib/rabbitmq /root
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
# No ProcSubset=pid: Odoo's psutil reads /proc/stat and /proc/meminfo at startup and exits without them
# (found on the dev box, 2026-10-03). ProtectProc=invisible still hides other users' processes.
ProtectProc=invisible
SystemCallArchitectures=native
SocketBindDeny=any
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.tenant.%i

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # ADR 0106 / MULTI_TENANT_ODOO.md A3: one instance of this template per site that needs
            # large static files (hams-static@perens_com.service: the 1.1 GB /static/ tree of perens.com).
            # static_site_server.py is a read-only loopback file server; the tunnel sends one path to it.
            # It fetches nothing from anywhere and has no outbound network at all (IPAddressDeny=any
            # leaves only loopback), no write path and no capability.
            "path": "/opt/hams/systemd/hams-static@.service",
            "content": """\
[Unit]
Description=Read-only static file server for %i (ADR 0106: origin of the /static/ path an Odoo tunnel row routes to it)
After=network.target
ConditionPathExists=/etc/hams-static/%i.json

[Service]
Type=simple
User=hams_static
Group=hams_static
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 /usr/local/lib/hams-static/static_site_server.py --config /etc/hams-static/%i.json
Restart=on-failure
RestartSec=5
ProtectSystem=strict
ProtectHome=true
InaccessiblePaths=/opt/hams /var/lib/odoo /etc/odoo /var/log/odoo /var/lib/postgresql /var/lib/redis /var/lib/rabbitmq /var/lib/hams-tenants /etc/hams-tenants /root
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
# The drop-in written by static_site_ctl.py adds the one port this instance may bind.
SocketBindDeny=any
IPAddressDeny=any
IPAddressAllow=localhost
MemoryMax=256M
TasksMax=128
CPUQuota=50%
LimitNOFILE=1024
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.static.%i

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # Loads the nftables table that stops tenant accounts opening new connections to local
            # services (Redis, RabbitMQ, hams daemons, PostgreSQL over TCP), the WireGuard network and
            # the cloud metadata address. tenant_lib.render_nft writes the file.
            "path": "/opt/hams/systemd/hams-tenant-firewall.service",
            "content": """\
[Unit]
Description=Firewall table for Odoo tenant accounts (retired design, superseded by ADR 0106)
Before=network-online.target
ConditionPathExists=/etc/hams-tenants/firewall.nft

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/hams-tenants/firewall.nft
ExecStop=-/usr/sbin/nft delete table inet hams_tenants
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.tenant.firewall

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # Every 10 minutes: each tenant's unit, its HTTP answer on loopback and the age of its
            # newest backup (tenant_ctl health --all). Exits non-zero when anything is wrong, which
            # leaves this unit failed, which the "Systemd Failed Services Tracker" pager check already
            # reports. Needs root (the backup directory is 0700 root). Reads local state only.
            "path": "/opt/hams/systemd/hams-tenant-health.service",
            "content": """\
[Unit]
Description=Check every Odoo tenant (unit, HTTP answer, backup age)
After=postgresql.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/hams/hams_shared/tools/tenant_ctl.py health --all
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.tenant.health
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/hams-tenant-health.timer",
            "content": """\
[Unit]
Description=Check every Odoo tenant every ten minutes

[Timer]
OnCalendar=*:0/10
Persistent=false
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # Nightly pg_dump plus filestore archive of every tenant, with checksums, root-only.
            # Deliberately not sandboxed for the same reason as hams.db.local.backup.service (runuser to
            # postgres, writes a root-only directory). Reads and writes local files only.
            "path": "/opt/hams/systemd/hams-tenant-backup.service",
            "content": """\
[Unit]
Description=Back up every Odoo tenant (database dump and filestore, with checksums)
After=postgresql.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/hams/hams_shared/tools/tenant_ctl.py backup --all --apply
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.tenant.backup
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/hams-tenant-backup.timer",
            "content": """\
[Unit]
Description=Nightly Odoo tenant backup

[Timer]
OnCalendar=*-*-* 03:10:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            # Weekly proof that the newest backup of each tenant actually restores (into a scratch
            # database that is dropped again). Local only.
            "path": "/opt/hams/systemd/hams-tenant-restore-test.service",
            "content": """\
[Unit]
Description=Restore-test the newest backup of every Odoo tenant into a scratch database
After=postgresql.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/hams/hams_shared/tools/tenant_ctl.py restore-test --all
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.tenant.restore.test
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/hams-tenant-restore-test.timer",
            "content": """\
[Unit]
Description=Weekly Odoo tenant restore test

[Timer]
OnCalendar=Sun *-*-* 04:20:00
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "host_class": "odoo_tenants",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/gdpr.csv.export.service",
            "content": """\
[Unit]
Description=GDPR CSV/Zip Export Daemon
After=network.target redis-server.service
Requires=redis-server.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_gdpr_export), Phase 4 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It builds each export in memory or in the unit's private /tmp and writes no other file, so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=simple
User=hamsd_gdpr_export
Group=hamsd_gdpr_export
WorkingDirectory=/opt/hams/daemons/gdpr_csv_export

# Code audit, 2026-10-04: gdpr_csv_export/main.py reads the GDPR_EXPORT_* caps (defaults in code) and the Redis settings REDIS_HOST, REDIS_PORT, REDIS_USERNAME and REDIS_PASSWORD and it reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). redis.env is therefore loaded; no database, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
EnvironmentFile=/opt/hams/etc/redis.env
Environment="ODOO_USER=gdpr_export_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/gdpr_export/gdpr_export_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification -- confirms Redis (the admission-control
# counter store) is reachable before systemd brings this into service.
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/gdpr_csv_export/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/gdpr_csv_export/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=gdpr.csv.export

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/qrz.scraper.service",
            "external_fetch": "scrapes a third-party callbook website",
            "content": """\
[Unit]
Description=Ham Radio QRZ Scraper Daemon
After=network.target rabbitmq-server.service
Requires=rabbitmq-server.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_qrz_scraper), Phase 4 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It consumes RabbitMQ jobs and writes no file, so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=simple
User=hamsd_qrz_scraper
Group=hamsd_qrz_scraper
WorkingDirectory=/opt/hams/daemons/qrz_scraper

# Code audit, 2026-10-04: qrz_scraper/main.py reads SYSTEM_USER_AGENT and the RabbitMQ settings RABBITMQ_HOST, RMQ_PORT, RMQ_USER and RMQ_PASS and it reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). rabbitmq.env is therefore loaded; no database, Redis or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
EnvironmentFile=/opt/hams/etc/rabbitmq.env
Environment="ODOO_USER=onboarding_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/qrz_scraper/onboarding_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/qrz_scraper/main.py --start-test

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_qrz_scraper.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_qrz_scraper
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_qrz_scraper/hamsd_qrz_scraper.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_qrz_scraper/hamsd_qrz_scraper.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_qrz_scraper/hamsd_qrz_scraper.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/qrz_scraper/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=qrz.scraper

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/au.acma.sync.service",
            "external_fetch": "downloads a national licensing regulator's amateur register",
            "content": """\
[Unit]
Description=Ham Radio Australia ACMA Callsign Sync (One-Shot)
After=network.target
# Republish the callbook DNS zone once fresh register data has landed
# (docs/proposals/CALLBOOK_DNS_SERVICE.md, "Serving": export after each
# country sync). AU publishes its licence class in that zone.
OnSuccess=callbook.dns.export.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_country_sync), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). Each daemon writes only its own spool and downloads directories (smart_download() with its daemon name, br_anatel's spool directory, ised_canada's HAMS_DOWNLOAD_DIR), and au.callsign.sync and nz.rsm.sync write no file. Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/au_acma_sync /opt/hams/downloads/au_acma_sync
Type=oneshot
User=hamsd_country_sync
Group=hamsd_country_sync
WorkingDirectory=/opt/hams/daemons/au_acma_sync

# Code audit, 2026-10-04: each daemon in the family reads only SYSTEM_USER_AGENT (and the Odoo client's ODOO_URL, DB_NAME and the key file through hams_config), plus its own source URL overrides (ANATEL_ZIP_URL, DE_MIRRORS, ISED_URL, RSM_*, UK_MIRRORS) and, for fcc_uls_sync, FCC_ULS_PROXY_URL and RANDOM_DELAY_MAX, none of which any environment file sets. No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/country_sync/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_country_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/au_acma_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=au.acma.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/au.acma.sync.timer",
            "external_fetch": "activates au.acma.sync.service",
            "content": """\
[Unit]
Description=Ham Radio Australia ACMA Callsign Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/au.callsign.sync.service",
            "external_fetch": "walks a national regulator's online register page by page",
            "content": """\
[Unit]
Description=Ham Radio Australia ACMA SPA Scraper (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_country_sync), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). Each daemon writes only its own spool and downloads directories (smart_download() with its daemon name, br_anatel's spool directory, ised_canada's HAMS_DOWNLOAD_DIR), and au.callsign.sync and nz.rsm.sync write no file. Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_country_sync
Group=hamsd_country_sync
WorkingDirectory=/opt/hams/daemons/au_callsign_sync

# Code audit, 2026-10-04: each daemon in the family reads only SYSTEM_USER_AGENT (and the Odoo client's ODOO_URL, DB_NAME and the key file through hams_config), plus its own source URL overrides (ANATEL_ZIP_URL, DE_MIRRORS, ISED_URL, RSM_*, UK_MIRRORS) and, for fcc_uls_sync, FCC_ULS_PROXY_URL and RANDOM_DELAY_MAX, none of which any environment file sets. No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/country_sync/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_country_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/au_callsign_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=au.callsign.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/au.callsign.sync.timer",
            "external_fetch": "activates au.callsign.sync.service",
            "content": """\
[Unit]
Description=Ham Radio Australia ACMA SPA Scraper Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/br.anatel.sync.service",
            "external_fetch": "downloads a national licensing regulator's amateur register",
            "content": """\
[Unit]
Description=Ham Radio Brazil ANATEL Callsign Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_country_sync), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). Each daemon writes only its own spool and downloads directories (smart_download() with its daemon name, br_anatel's spool directory, ised_canada's HAMS_DOWNLOAD_DIR), and au.callsign.sync and nz.rsm.sync write no file. Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/br_anatel_sync
Type=oneshot
User=hamsd_country_sync
Group=hamsd_country_sync
WorkingDirectory=/opt/hams/daemons/br_anatel_sync

# Code audit, 2026-10-04: each daemon in the family reads only SYSTEM_USER_AGENT (and the Odoo client's ODOO_URL, DB_NAME and the key file through hams_config), plus its own source URL overrides (ANATEL_ZIP_URL, DE_MIRRORS, ISED_URL, RSM_*, UK_MIRRORS) and, for fcc_uls_sync, FCC_ULS_PROXY_URL and RANDOM_DELAY_MAX, none of which any environment file sets. No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/country_sync/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_country_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/br_anatel_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=br.anatel.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/br.anatel.sync.timer",
            "external_fetch": "activates br.anatel.sync.service",
            "content": """\
[Unit]
Description=Ham Radio Brazil ANATEL Callsign Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/de.bnetza.sync.service",
            "external_fetch": "downloads the national callsign list from a third-party site",
            "content": """\
[Unit]
Description=Ham Radio Germany BNetzA Callsign Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_country_sync), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). Each daemon writes only its own spool and downloads directories (smart_download() with its daemon name, br_anatel's spool directory, ised_canada's HAMS_DOWNLOAD_DIR), and au.callsign.sync and nz.rsm.sync write no file. Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/de_bnetza_sync /opt/hams/downloads/de_bnetza_sync
Type=oneshot
User=hamsd_country_sync
Group=hamsd_country_sync
WorkingDirectory=/opt/hams/daemons/de_bnetza_sync

# Code audit, 2026-10-04: each daemon in the family reads only SYSTEM_USER_AGENT (and the Odoo client's ODOO_URL, DB_NAME and the key file through hams_config), plus its own source URL overrides (ANATEL_ZIP_URL, DE_MIRRORS, ISED_URL, RSM_*, UK_MIRRORS) and, for fcc_uls_sync, FCC_ULS_PROXY_URL and RANDOM_DELAY_MAX, none of which any environment file sets. No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/country_sync/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_country_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/de_bnetza_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=de.bnetza.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/de.bnetza.sync.timer",
            "external_fetch": "activates de.bnetza.sync.service",
            "content": """\
[Unit]
Description=Ham Radio Germany BNetzA Callsign Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/nz.rsm.sync.service",
            "external_fetch": "pages through a national regulator's register API",
            "content": """\
[Unit]
Description=Ham Radio New Zealand RSM Callsign Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_country_sync), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). Each daemon writes only its own spool and downloads directories (smart_download() with its daemon name, br_anatel's spool directory, ised_canada's HAMS_DOWNLOAD_DIR), and au.callsign.sync and nz.rsm.sync write no file. Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_country_sync
Group=hamsd_country_sync
WorkingDirectory=/opt/hams/daemons/nz_rsm_sync

# Code audit, 2026-10-04: each daemon in the family reads only SYSTEM_USER_AGENT (and the Odoo client's ODOO_URL, DB_NAME and the key file through hams_config), plus its own source URL overrides (ANATEL_ZIP_URL, DE_MIRRORS, ISED_URL, RSM_*, UK_MIRRORS) and, for fcc_uls_sync, FCC_ULS_PROXY_URL and RANDOM_DELAY_MAX, none of which any environment file sets. No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/country_sync/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_country_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/nz_rsm_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=nz.rsm.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/nz.rsm.sync.timer",
            "external_fetch": "activates nz.rsm.sync.service",
            "content": """\
[Unit]
Description=Ham Radio New Zealand RSM Callsign Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/uk.ofcom.sync.service",
            "external_fetch": "downloads a national licensing regulator's amateur register",
            "content": """\
[Unit]
Description=Ham Radio UK Ofcom Callsign Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_country_sync), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). Each daemon writes only its own spool and downloads directories (smart_download() with its daemon name, br_anatel's spool directory, ised_canada's HAMS_DOWNLOAD_DIR), and au.callsign.sync and nz.rsm.sync write no file. Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/uk_ofcom_sync /opt/hams/downloads/uk_ofcom_sync
Type=oneshot
User=hamsd_country_sync
Group=hamsd_country_sync
WorkingDirectory=/opt/hams/daemons/uk_ofcom_sync

# Code audit, 2026-10-04: each daemon in the family reads only SYSTEM_USER_AGENT (and the Odoo client's ODOO_URL, DB_NAME and the key file through hams_config), plus its own source URL overrides (ANATEL_ZIP_URL, DE_MIRRORS, ISED_URL, RSM_*, UK_MIRRORS) and, for fcc_uls_sync, FCC_ULS_PROXY_URL and RANDOM_DELAY_MAX, none of which any environment file sets. No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/country_sync/callbook_sync_service_internal.key"
# The Chromium visit (fetch_with_browser) launches the browser from the shared install; the variable is set here
# because it lives in core.env, which this unit does not load. Playwright starts Chromium without its own
# sandbox by default, which is why RestrictNamespaces above does not stop it.
Environment="PLAYWRIGHT_BROWSERS_PATH=/opt/hams/cache/ms-playwright"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_country_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/uk_ofcom_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=uk.ofcom.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # Weekly, not daily (2026-10-02): Ofcom's amateur callsign CSV changes rarely, and a
            # daily poll of someone else's server for a file that is almost always unchanged is
            # load for nothing. Each run is also cheap when nothing changed: smart_download sends a
            # HEAD and skips the body when the ETag/Last-Modified match the values stored in Odoo.
            "path": "/opt/hams/systemd/uk.ofcom.sync.timer",
            "external_fetch": "activates uk.ofcom.sync.service",
            "content": """\
[Unit]
Description=Ham Radio UK Ofcom Callsign Sync Weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/wa7bnm.contest.sync.service",
            "external_fetch": "fetches a third-party contest calendar feed",
            "content": """\
[Unit]
Description=Ham Radio WA7BNM Contest Calendar Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_event_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). The five scripts write no file (they fetch a page or feed and push to Odoo), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_event_sync
Group=hamsd_event_sync
WorkingDirectory=/opt/hams/daemons/event_sync

# Code audit, 2026-10-04: the five scripts in daemons/event_sync read SYSTEM_USER_AGENT for their request headers and reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_event_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/event_sync/wa7bnm_contest_sync.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wa7bnm.contest.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/wa7bnm.contest.sync.timer",
            "external_fetch": "activates wa7bnm.contest.sync.service",
            "content": """\
[Unit]
Description=Ham Radio WA7BNM Contest Calendar Sync Weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/arrl.hamfests.sync.service",
            "external_fetch": "scrapes a third-party hamfest calendar",
            "content": """\
[Unit]
Description=Ham Radio ARRL Hamfests Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_event_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). The five scripts write no file (they fetch a page or feed and push to Odoo), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_event_sync
Group=hamsd_event_sync
WorkingDirectory=/opt/hams/daemons/event_sync

# Code audit, 2026-10-04: the five scripts in daemons/event_sync read SYSTEM_USER_AGENT for their request headers and reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_event_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/event_sync/arrl_hamfests_sync.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=arrl.hamfests.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/arrl.hamfests.sync.timer",
            "external_fetch": "activates arrl.hamfests.sync.service",
            "content": """\
[Unit]
Description=Ham Radio ARRL Hamfests Sync Weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/event.cover.image.sync.service",
            "external_fetch": "queries Wikidata and Wikimedia Commons for openly licensed event cover pictures",
            "content": """\
[Unit]
Description=Ham Radio Event Cover Picture Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# The event_sync family account (hamsd_event_sync). The script writes no file: it asks Wikidata and Commons for a licensed picture and pushes it to Odoo, so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_event_sync
Group=hamsd_event_sync
WorkingDirectory=/opt/hams/daemons/event_sync

# Code audit, 2026-10-05: event_cover_image_sync.py reads SYSTEM_USER_AGENT for its request headers and reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_event_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/event_sync/event_cover_image_sync.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=event.cover.image.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/event.cover.image.sync.timer",
            "external_fetch": "activates event.cover.image.sync.service",
            "content": """\
[Unit]
Description=Ham Radio Event Cover Picture Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/rac.events.sync.service",
            "external_fetch": "scrapes a third-party event calendar",
            "content": """\
[Unit]
Description=Ham Radio RAC Events Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_event_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). The five scripts write no file (they fetch a page or feed and push to Odoo), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_event_sync
Group=hamsd_event_sync
WorkingDirectory=/opt/hams/daemons/event_sync

# Code audit, 2026-10-04: the five scripts in daemons/event_sync read SYSTEM_USER_AGENT for their request headers and reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_event_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/event_sync/rac_events_sync.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=rac.events.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/rac.events.sync.timer",
            "external_fetch": "activates rac.events.sync.service",
            "content": """\
[Unit]
Description=Ham Radio RAC Events Sync Weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/sm3cer.contest.sync.service",
            "external_fetch": "scrapes a third-party contest calendar",
            "content": """\
[Unit]
Description=Ham Radio SM3CER Contest Calendar Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_event_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). The five scripts write no file (they fetch a page or feed and push to Odoo), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_event_sync
Group=hamsd_event_sync
WorkingDirectory=/opt/hams/daemons/event_sync

# Code audit, 2026-10-04: the five scripts in daemons/event_sync read SYSTEM_USER_AGENT for their request headers and reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_event_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/event_sync/sm3cer_contest_sync.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=sm3cer.contest.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/sm3cer.contest.sync.timer",
            "external_fetch": "activates sm3cer.contest.sync.service",
            "content": """\
[Unit]
Description=Ham Radio SM3CER Contest Calendar Sync Weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # electronicsfleamarket.com's monthly swap meet ("2nd Saturday
            # of each month") -- same shape as arrl.hamfests.sync above.
            # The site's own schedule changes a handful of times a year,
            # so the sibling daemons' weekly cadence is ample.
            "path": "/opt/hams/systemd/electronicsfleamarket.sync.service",
            "external_fetch": "scrapes a third-party event website",
            "content": """\
[Unit]
Description=Ham Radio Electronics Flea Market Swap Meet Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_event_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). The five scripts write no file (they fetch a page or feed and push to Odoo), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_event_sync
Group=hamsd_event_sync
WorkingDirectory=/opt/hams/daemons/event_sync

# Code audit, 2026-10-04: the five scripts in daemons/event_sync read SYSTEM_USER_AGENT for their request headers and reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_event_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/event_sync/electronicsfleamarket_sync.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=electronicsfleamarket.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/electronicsfleamarket.sync.timer",
            "external_fetch": "activates electronicsfleamarket.sync.service",
            "content": """\
[Unit]
Description=Ham Radio Electronics Flea Market Swap Meet Sync Weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # Historic radio societies and museums as event sources (the California Historical
            # Radio Society's iCalendar feed, the Museum Ships Weekend rule) and the registration
            # of each one as a "historic_radio_society" club. Same shape as the sibling event
            # syncs; hams_com daemons/event_sync/radio_history_events_sync.py.
            "path": "/opt/hams/systemd/radio.history.events.sync.service",
            "external_fetch": "fetches a historic radio society calendar feed from a third-party website",
            "content": """\
[Unit]
Description=Ham Radio Historic Radio Societies Event Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_event_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). The five scripts write no file (they fetch a page or feed and push to Odoo), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_event_sync
Group=hamsd_event_sync
WorkingDirectory=/opt/hams/daemons/event_sync

# Code audit, 2026-10-04: the five scripts in daemons/event_sync read SYSTEM_USER_AGENT for their request headers and reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_event_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/event_sync/radio_history_events_sync.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=radio.history.events.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/radio.history.events.sync.timer",
            "external_fetch": "activates radio.history.events.sync.service",
            "content": """\
[Unit]
Description=Ham Radio Historic Radio Societies Event Sync Weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # Re-reads the page of each recurring event (Electronics Flea Market, Flea at MIT, ...)
            # about a week before it happens, to catch a moved parking lot, gate or venue; hams_com
            # daemons/event_sync/event_series_recheck.py. Odoo decides what is due (occurrences 5 to
            # 9 days away, not checked in the last 6 days), so a daily run acts about once per event.
            "path": "/opt/hams/systemd/monthly.events.recheck.service",
            "external_fetch": "re-reads the pages of recurring events on third-party websites",
            "content": """\
[Unit]
Description=Ham Radio Recurring Event Pre-Event Re-check (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_event_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). The five scripts write no file (they fetch a page or feed and push to Odoo), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_event_sync
Group=hamsd_event_sync
WorkingDirectory=/opt/hams/daemons/event_sync

# Code audit, 2026-10-04: the five scripts in daemons/event_sync read SYSTEM_USER_AGENT for their request headers and reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_event_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/event_sync/event_series_recheck.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=monthly.events.recheck
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/monthly.events.recheck.timer",
            "external_fetch": "activates monthly.events.recheck.service",
            "content": """\
[Unit]
Description=Ham Radio Recurring Event Pre-Event Re-check Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/fcc.uls.sync.service",
            "external_fetch": "downloads the national licence database (about 1.3M rows)",
            "content": """\
[Unit]
Description=Ham Radio FCC ULS Daily Sync Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_country_sync), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). Each daemon writes only its own spool and downloads directories (smart_download() with its daemon name, br_anatel's spool directory, ised_canada's HAMS_DOWNLOAD_DIR), and au.callsign.sync and nz.rsm.sync write no file. Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/fcc_uls_sync /opt/hams/downloads/fcc_uls_sync
# Real bug found live on hams1, 2026-09-22: this was Type=simple with
# Restart=always/RestartSec=10 -- but run_sync() (main.py) processes every
# configured download once per invocation and exits; it was never meant to
# be a persistent service. That combination meant systemd restarted it
# every 10 seconds, forever, all day (confirmed live: restart counter over
# 5000) -- hammering data.fcc.gov with a fresh batch of requests every 10
# seconds continuously, which is very likely what got this box's IP
# rate-limited/blocked by Akamai in the first place, independent of
# whatever else was also wrong with the request shape. A "Daily Sync
# Daemon" belongs on Type=oneshot triggered by its own .timer (see
# fcc.uls.sync.timer), the same shape every other daily sync daemon in
# this codebase already uses, not Restart=always.
Type=oneshot
User=hamsd_country_sync
Group=hamsd_country_sync
WorkingDirectory=/opt/hams/daemons/fcc_uls_sync

# Code audit, 2026-10-04: each daemon in the family reads only SYSTEM_USER_AGENT (and the Odoo client's ODOO_URL, DB_NAME and the key file through hams_config), plus its own source URL overrides (ANATEL_ZIP_URL, DE_MIRRORS, ISED_URL, RSM_*, UK_MIRRORS) and, for fcc_uls_sync, FCC_ULS_PROXY_URL and RANDOM_DELAY_MAX, none of which any environment file sets. No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/country_sync/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_country_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/fcc_uls_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=fcc.uls.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/fcc.uls.sync.timer",
            "external_fetch": "activates fcc.uls.sync.service",
            "content": """\
[Unit]
Description=Run the FCC ULS Daily Sync Once a Day

[Timer]
OnCalendar=*-*-* 05:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            # night_shift_todo/medium/pi500-1-fcc-uls-sync-not-tracked-in-infrastructure-py-
            # 6d9a2f83.md: this "prod"/"test" entry describes the OLD deployment shape (system
            # Python directly on hams1) -- it is stale and no longer what's actually running
            # anywhere. The real, currently-working deployment moved to pi500-1's own
            # residential egress path (see fcc-uls-sync-needs-non-datacenter-egress-path-
            # a8e5f3c1.md for why: hams1's datacenter IP got rate-limited/blocked by Akamai);
            # see the "pi500-1" entries a few lines below for the real, live unit files. Left
            # in place rather than deleted -- removing a provisioning manifest entry that
            # something else might still reference was a real, unverified risk, and this
            # to-do's own scope is "capture the real content in git," not "clean up the old
            # one."
            "environments": ["prod", "test"],
        },
        {
            # night_shift_todo/medium/pi500-1-fcc-uls-sync-not-tracked-in-infrastructure-py-
            # 6d9a2f83.md: the REAL, currently-working unit file, pulled directly from
            # /etc/systemd/system/fcc.uls.sync.service on pi500-1 (2026-09-30) -- not the stale
            # "prod"/"test" entry a few lines above. Tagged with its own "pi500-1" environment
            # (matching the existing "early_prod" precedent) so provision_environment() never
            # touches hams1's own provisioning unless something explicitly targets
            # environment="pi500-1" in the future.
            #
            # Runs under a real venv (Python 3.11.2, packages: requests, curl_cffi, certifi,
            # idna, urllib3, cffi, pycparser, charset-normalizer -- curl_cffi specifically
            # because a plain `requests` TLS fingerprint was part of what got the old
            # datacenter-IP deployment blocked; see the egress-path proposal doc above) at
            # /opt/hams/daemons/fcc_uls_sync/.venv, and a real ODOO_KEY_FILE at
            # /opt/hams/daemons/fcc_uls_sync/.keys/fcc_uls_sync.env -- neither the venv's own
            # installed packages nor the key file's contents are reproduced here; a rebuild
            # still needs to recreate the venv (`python3 -m venv .venv && .venv/bin/pip install
            # requests curl_cffi`) and re-provision a real key file separately, the same as
            # every other daemon's own secret-provisioning convention in this codebase.
            #
            # ODOO_KEY_SELF_ROTATE_DAEMON: hams1's Odoo cannot write this machine's key file,
            # so the daemon rotates its own key over JSON-2 (daemon_key_manager's
            # rotate_own_key(), hams_com daemons/hams_config.py's rotate_own_key_if_due()). The
            # value is the daemon.key.registry name, whose Remote Self-Rotation box must be set.
            # Without it, hams1's 59-day cron revokes this key and the daemon is locked out.
            "path": "/etc/systemd/system/fcc.uls.sync.service",
            "external_fetch": "downloads the national licence database (about 1.3M rows)",
            "content": """\
[Unit]
Description=FCC ULS Daily Sync (non-datacenter egress path, see night_shift_todo/high/fcc-uls-sync-needs-non-datacenter-egress-path-a8e5f3c1.md)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=odoo
UMask=0002
WorkingDirectory=/opt/hams/daemons/fcc_uls_sync
Environment="ODOO_URL=https://hams.com"
Environment="ODOO_DB=hams_prod"
Environment="ODOO_KEY_FILE=/opt/hams/daemons/fcc_uls_sync/.keys/fcc_uls_sync.env"
Environment="ODOO_KEY_SELF_ROTATE_DAEMON=FCC ULS Sync (pi500-1)"
Environment="SYSTEM_USER_AGENT=HamsComSyncDaemon/1.0 (+https://crawler.hams.com)"
Environment="PYTHONPATH=/opt/hams/daemons"
ExecStart=/opt/hams/daemons/fcc_uls_sync/.venv/bin/python3 /opt/hams/daemons/fcc_uls_sync/main.py

StandardOutput=journal
StandardError=journal
SyslogIdentifier=fcc.uls.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["pi500-1"],
        },
        {
            "path": "/etc/systemd/system/fcc.uls.sync.timer",
            "external_fetch": "activates fcc.uls.sync.service",
            "content": """\
[Unit]
Description=Run the FCC ULS Daily Sync Once a Day (from pi500-1's residential egress path)

[Timer]
OnCalendar=*-*-* 05:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["pi500-1"],
        },
        {
            # HamCall licensed-index sync (ADR-0092), a ONE-SHOT service with NO TIMER on
            # purpose. Bruce, 2026-09-20: no automatic updates for now. The daemon runs once
            # when the licensed hamcall.idx is placed at /opt/hams/hamcall/hamcall.idx and
            # again by hand when a new file is dropped in:
            #     systemctl start hamcall.idx.sync.service
            # It exits 1 with the path and the operator step if the file is absent, so a
            # timer on a host without the file would fail every run. To add scheduled updates
            # later, add a hamcall.idx.sync.timer entry modeled on au.callsign.sync.timer
            # (OnCalendar=daily, Persistent=true, RandomizedDelaySec=15m); the whole-file
            # SHA-256 short-circuit makes an unchanged day cheap. Read-only on purpose:
            # the daemon only reads the file and talks to Odoo.
            "path": "/opt/hams/systemd/hamcall.idx.sync.service",
            "content": """\
[Unit]
Description=HamCall Licensed Index Presence Sync (One-Shot, manual start)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_hamcall_sync), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It only reads the licensed hamcall.idx in /opt/hams/hamcall, which the account owns (the operator installs the file as that account), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_hamcall_sync
Group=hamsd_hamcall_sync
WorkingDirectory=/opt/hams/daemons/hamcall_idx_sync

# Code audit, 2026-10-04: daemons/hamcall_idx_sync/main.py reads HAMCALL_IDX_PATH (set in this unit) and RANDOM_DELAY_MAX (no environment file sets it) and reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=hamcall_verify_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/hamcall_sync/hamcall_verify_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="HAMCALL_IDX_PATH=/opt/hams/hamcall/hamcall.idx"
Environment="DAEMON_ARGS="

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/hamcall_idx_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=hamcall.idx.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # Independent cadence from fcc.uls.sync deliberately -- that
            # daemon polls continuously (Restart=always/RestartSec=10, cheap
            # per-attempt thanks to its own ETag/Last-Modified short-circuit)
            # for latency-sensitive FCC data freshness. Geo-enrichment has no
            # such latency need (docs/proposals/FCC_INGESTION_GEO_ENRICHMENT.md:
            # congressional-district boundaries only change on redistricting,
            # not per-record), each run does a real incremental search_read
            # over ham.callbook plus a live per-address call to the Census
            # Bureau's free public API (be polite, not a bulk API meant for
            # hammering per main.py's own docstring) -- a fast restart-loop
            # cadence would be wasteful DB/API load for no real freshness
            # benefit. A daily oneshot+timer (the au.callsign.sync/br.anatel
            # pattern) comfortably keeps up with same-day FCC ingestion.
            "path": "/opt/hams/systemd/callbook.geo.enrich.service",
            "external_fetch": "calls a public geocoding service once per callbook record",
            "content": """\
[Unit]
Description=Ham Radio Callbook Congressional District / County Geo-Enrichment (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_callbook_geo), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It writes no file (its temporary upload file is in the unit's private /tmp), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_callbook_geo
Group=hamsd_callbook_geo
WorkingDirectory=/opt/hams/daemons/ham_callbook_geo_enrich

# Code audit, 2026-10-04: daemons/ham_callbook_geo_enrich/main.py reads GEO_ENRICH_FULL_PASS and GEO_ENRICH_MAX_RECORDS (neither is set by any environment file) and reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_geo_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_geo/callbook_geo_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_callbook_geo.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_callbook_geo
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_callbook_geo/hamsd_callbook_geo.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_callbook_geo/hamsd_callbook_geo.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_callbook_geo/hamsd_callbook_geo.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/ham_callbook_geo_enrich/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=callbook.geo.enrich
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/callbook.geo.enrich.timer",
            "external_fetch": "activates callbook.geo.enrich.service",
            "content": """\
[Unit]
Description=Ham Radio Callbook Geo-Enrichment Daily (Incremental)

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/callbook.dns.export.service",
            "content": """\
[Unit]
Description=Ham Radio Callbook DNS Zone Exporter (One-Shot)
After=network.target pdns.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_dns_export), Phase 5 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). The one directory it writes is PowerDNS's callbook database directory (/var/lib/powerdns/callbook, pdns:pdns 2775): the account reaches it through SupplementaryGroups=pdns on this unit alone, not through membership of the pdns group.
ProtectProc=invisible
ProcSubset=pid
# UMask 0002, deliberately NOT the 0027 of the other family units: the database files this run creates or rewrites (the staging file, the -wal and -shm files) must stay group-writable for `pdns`, because pdns.service (User=pdns, no supplementary groups) needs write access to a WAL-mode SQLite database to read it. Found live 2026-09-22: "attempt to write a readonly database" after the first publish. The setgid bit on the directory supplies the group.
UMask=0002
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/var/lib/powerdns/callbook
Type=oneshot
User=hamsd_dns_export
Group=hamsd_dns_export
SupplementaryGroups=pdns
WorkingDirectory=/opt/hams/daemons/callbook_dns_export

# Code audit, 2026-10-06: callbook_dns_export/main.py reads DOMAIN, CALLBOOK_DNS_DB, CALLBOOK_DNS_NAMESERVERS (optional), CALLBOOK_PDNS_API_URL and PDNS_API_KEY (the PowerDNS API key, for the cache flush), and reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file, SYSTEM_USER_AGENT). common.env and pdns.env are therefore loaded; no database, Redis or RabbitMQ credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
EnvironmentFile=/opt/hams/etc/pdns.env
Environment="ODOO_USER=callbook_dns_export_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/dns_export/callbook_dns_export_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="CALLBOOK_DNS_DB=/var/lib/powerdns/callbook/callbook.sqlite3"
Environment="CALLBOOK_PDNS_API_URL=http://127.0.0.1:8081/api/v1/servers/localhost"

ExecStart=/usr/bin/python3 /opt/hams/daemons/callbook_dns_export/main.py

StandardOutput=journal
StandardError=journal
SyslogIdentifier=callbook.dns.export
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/callbook.dns.export.timer",
            "content": """\
[Unit]
Description=Ham Radio Callbook DNS Zone Export, Nightly and After the FCC Sync

[Timer]
OnCalendar=daily
# fcc.uls.sync runs on pi500-1, not on this host, so OnSuccess= cannot
# chain to it. Its timer fires at 05:00 America/New_York plus up to 1h
# of RandomizedDelaySec; this run, two hours after the latest start,
# publishes the day's FCC changes instead of waiting for midnight UTC.
# The CA and AU syncs that run here chain to the export with OnSuccess=.
OnCalendar=*-*-* 08:00:00 America/New_York
Persistent=true
RandomizedDelaySec=30m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/ised.canada.sync.service",
            "external_fetch": "downloads a national licensing regulator's amateur register",
            "content": """\
[Unit]
Description=Ham Radio ISED Canada Callbook Sync (One-Shot)
After=network.target
# Republish the callbook DNS zone once fresh register data has landed
# (docs/proposals/CALLBOOK_DNS_SERVICE.md, "Serving": export after each
# country sync).
OnSuccess=callbook.dns.export.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_country_sync), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). Each daemon writes only its own spool and downloads directories (smart_download() with its daemon name, br_anatel's spool directory, ised_canada's HAMS_DOWNLOAD_DIR), and au.callsign.sync and nz.rsm.sync write no file. Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/downloads/ised_canada_sync
Type=oneshot
User=hamsd_country_sync
Group=hamsd_country_sync
WorkingDirectory=/opt/hams/daemons/ised_canada_sync

# Code audit, 2026-10-04: each daemon in the family reads only SYSTEM_USER_AGENT (and the Odoo client's ODOO_URL, DB_NAME and the key file through hams_config), plus its own source URL overrides (ANATEL_ZIP_URL, DE_MIRRORS, ISED_URL, RSM_*, UK_MIRRORS) and, for fcc_uls_sync, FCC_ULS_PROXY_URL and RANDOM_DELAY_MAX, none of which any environment file sets. No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/country_sync/callbook_sync_service_internal.key"
Environment="HAMS_DOWNLOAD_DIR=/opt/hams/downloads/ised_canada_sync"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_country_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/ised_canada_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=ised.canada.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/ised.canada.sync.timer",
            "external_fetch": "activates ised.canada.sync.service",
            "content": """\
[Unit]
Description=Ham Radio ISED Canada Callbook Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/ncvec.sync.service",
            "external_fetch": "downloads question pools from a third-party site",
            "content": """\
[Unit]
Description=Ham Radio NCVEC Question Pool Sync Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account, Phase 1 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). Only the account's
# own directories are writable: the account is in hams_com, which owns directories this daemon must
# never write (the ADIF queue), and this read-only mount is what keeps it out of them.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/ncvec /opt/hams/spool/ncvec_sync /opt/hams/downloads/ncvec_sync
Type=oneshot
User=hamsd_ncvec_sync
Group=hamsd_ncvec_sync
WorkingDirectory=/opt/hams/daemons/ncvec_sync

# Code audit, 2026-10-04: main.py and hams_config.py read SYSTEM_USER_AGENT, and ODOO_URL / DB_NAME on
# the Odoo push path that run_sync() does not call today. No database, Redis, RabbitMQ or PowerDNS
# credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=ncvec_sync_service_internal"
Environment="HAMS_NCVEC_DATA_DIR=/opt/hams/spool/ncvec"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/ncvec_sync/ncvec_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_ncvec_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_ncvec_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_ncvec_sync/hamsd_ncvec_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_ncvec_sync/hamsd_ncvec_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_ncvec_sync/hamsd_ncvec_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/ncvec_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=ncvec.sync

""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # The Web Bot Auth key directory publisher: creates its signing key once, then signs and writes the directory bundle
            # hams.com serves. Local only: reads and writes its own directory, no network (RestrictAddressFamilies=AF_UNIX).
            "path": "/opt/hams/systemd/web.bot.auth.directory.service",
            "content": """\
[Unit]
Description=Sign and Publish the Web Bot Auth Key Directory

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_UNIX
CapabilityBoundingSet=
ProtectProc=invisible
ProcSubset=pid
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/web_bot_auth
Type=oneshot
User=hamsd_web_bot_auth
Group=hamsd_web_bot_auth
WorkingDirectory=/opt/hams/daemons

# No secret is loaded and no network is reachable: the private key is a file this account creates (mode 0600) in its own directory.
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth/publisher.key --jwk-file /opt/hams/spool/web_bot_auth/jwks/publisher.jwk
# One --jwk-file per signing family: the public JWK that family's own ExecStartPre= writes (a family that has not run yet is skipped with a warning).
ExecStart=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py refresh --key-file /opt/hams/spool/web_bot_auth/publisher.key --jwk-dir /opt/hams/spool/web_bot_auth/jwks \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync/hamsd_activator_sync.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_callbook_geo/hamsd_callbook_geo.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_club_crawl/hamsd_club_crawl.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_club_search/hamsd_club_search.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_country_sync/hamsd_country_sync.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_event_sync/hamsd_event_sync.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_ncvec_sync/hamsd_ncvec_sync.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_qrz_scraper/hamsd_qrz_scraper.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_satellite_sync/hamsd_satellite_sync.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_space_weather/hamsd_space_weather.jwk \\
    --jwk-file /opt/hams/spool/web_bot_auth_keys/odoo/odoo.jwk \\
    --out /opt/hams/spool/web_bot_auth/directory.json

StandardOutput=journal
StandardError=journal
SyslogIdentifier=web.bot.auth.directory

""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/web.bot.auth.directory.timer",
            "content": """\
[Unit]
Description=Refresh the Web Bot Auth Key Directory Daily

[Timer]
# The directory's signature lasts seven days; a daily refresh leaves six days of slack for a failed run.
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/ncvec.sync.timer",
            "external_fetch": "activates ncvec.sync.service",
            "content": """\
[Unit]
Description=Run the NCVEC Question Pool Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/pota.sync.service",
            "external_fetch": "downloads park data from a third-party program API",
            "content": """\
[Unit]
Description=Ham Radio POTA Park Reference Data Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_activator_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). pota.sync writes no file; sota.sync writes only its own spool and downloads directories (smart_download() with the daemon name sota_sync). Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_activator_sync
Group=hamsd_activator_sync
WorkingDirectory=/opt/hams/daemons/pota_sync

# Code audit, 2026-10-04: pota_sync/main.py reads POTA_API_BASE (a public default), SYSTEM_USER_AGENT and the pause setting; sota_sync/main.py reads SYSTEM_USER_AGENT and a URL default; both reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=activator_data_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/activator_sync/activator_data_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_activator_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync/hamsd_activator_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync/hamsd_activator_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync/hamsd_activator_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/pota_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=pota.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/pota.sync.timer",
            "external_fetch": "activates pota.sync.service",
            "content": """\
[Unit]
Description=Ham Radio POTA Park Reference Data Sync Weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/sota.sync.service",
            "external_fetch": "downloads summit data from a third-party program site",
            "content": """\
[Unit]
Description=Ham Radio SOTA Summit Reference Data Sync (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_activator_sync), Phase 2 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). pota.sync writes no file; sota.sync writes only its own spool and downloads directories (smart_download() with the daemon name sota_sync). Everything else is read-only in the unit's sandbox.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/opt/hams/spool/sota_sync /opt/hams/downloads/sota_sync
Type=oneshot
User=hamsd_activator_sync
Group=hamsd_activator_sync
WorkingDirectory=/opt/hams/daemons/sota_sync

# Code audit, 2026-10-04: pota_sync/main.py reads POTA_API_BASE (a public default), SYSTEM_USER_AGENT and the pause setting; sota_sync/main.py reads SYSTEM_USER_AGENT and a URL default; both reach Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=activator_data_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/activator_sync/activator_data_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_activator_sync.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync/hamsd_activator_sync.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync/hamsd_activator_sync.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_activator_sync/hamsd_activator_sync.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/sota_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=sota.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/sota.sync.timer",
            "external_fetch": "activates sota.sync.service",
            "content": """\
[Unit]
Description=Ham Radio SOTA Summit Reference Data Sync Weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/code.review.sweep.service",
            "external_fetch": "sends source batches to a paid third-party LLM API",
            # Paid per call (Gemini API): never enabled or started by provisioning unless named
            # with provision.py --enable-opt-in. See opt_in_unit_names().
            "opt_in": "calls the paid Gemini API",
            "content": """\
[Unit]
Description=Hams.com Code Review Sweep -- Gemini leg (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction. Unlike pota.sync/sota.sync above,
# this unit's own execution costs real, recurring Gemini API spend on
# every run once a key is configured -- see docs/proposals/
# CODE_REVIEW_PROCESS.md's own Status section before enabling the timer
# below. ReadWritePaths only covers the report output directory: the rest
# of the repo tree stays read-only, which is all this daemon needs to
# read source files and run `git diff`/`git ls-files`.
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=-/opt/hams/hams_com/docs/code_review_reports
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/code_review_sweep

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=code_review_sweep_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/code_review_sweep_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
# CODE_REVIEW_REPO_ROOT: the live box's real checkout path hasn't been
# confirmed yet -- set this to wherever hams_com/ and hams_open/ actually
# live as siblings before this unit is ever enabled. Left unset here
# deliberately rather than guessed at.
Environment="DAEMON_ARGS=--mode=incremental"

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/code_review_sweep/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=code.review.sweep
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/code.review.sweep.timer",
            "external_fetch": "activates code.review.sweep.service",
            # Paid per call (Gemini API): never enabled or started by provisioning unless named
            # with provision.py --enable-opt-in. See opt_in_unit_names().
            "opt_in": "calls the paid Gemini API",
            "content": """\
[Unit]
Description=Hams.com Code Review Sweep Quarterly (incremental mode)

[Timer]
OnCalendar=quarterly
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/club.web.search.discovery.service",
            "external_fetch": "asks a paid third-party search-grounded LLM API about ham clubs, then fetches each new club's homepage once",
            # Paid per grounded request (Gemini API with Google Search grounding): never enabled or
            # started by provisioning unless named with provision.py --enable-opt-in.
            "opt_in": "calls the paid Gemini API with Google Search grounding",
            "content": """\
[Unit]
Description=Hams.com Club Web-Search Discovery -- grounded search for ham radio clubs (One-Shot)
After=network.target

[Service]
# docs/proposals/WEB_SEARCH_CLUB_DISCOVERY.md. Costs real money on every run (one grounded Gemini
# request covers several regions; the daemon enforces a per-run, a per-day request and a per-day
# search-query cap against a ledger kept in Odoo). It only ever stages CANDIDATES for an
# administrator to review; it never creates a club. Needs no write path: nothing is kept on disk.
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_club_search), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It writes no file (nothing is kept on disk), so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_club_search
Group=hamsd_club_search
WorkingDirectory=/opt/hams/daemons/club_web_search_discovery
TimeoutStartSec=1h

# Code audit, 2026-10-04: daemons/club_web_search_discovery/main.py reads CLUB_WEB_SEARCH_MODEL (no environment file sets it); the Gemini key it spends comes from the Odoo parameter gemini.api_key through the daemon's own Odoo key, not from the environment, so core.env's GEMINI_API_KEY is not used. It reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=club_web_search_discovery_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/club_search/club_web_search_discovery_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS=--max-requests-per-run=5 --max-requests-per-day=20 --max-queries-per-day=300"

# Execution via system Python
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_club_search.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_club_search
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_club_search/hamsd_club_search.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_club_search/hamsd_club_search.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_club_search/hamsd_club_search.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/club_web_search_discovery/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=club.web.search.discovery
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/club.web.search.discovery.timer",
            "external_fetch": "activates club.web.search.discovery.service",
            "opt_in": "calls the paid Gemini API with Google Search grounding",
            "content": """\
[Unit]
Description=Hams.com Club Web-Search Discovery (daily; a run with no region due spends nothing)

[Timer]
OnCalendar=daily
Persistent=false
RandomizedDelaySec=2h

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/aprs.is.sync.service",
            "external_fetch": "holds a persistent feed connection to a third-party network",
            "content": """\
[Unit]
Description=Ham Radio APRS-IS Sync Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_aprs_sync), Phase 4 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It runs all the time, holds an APRS-IS socket and writes no file, so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=simple
User=hamsd_aprs_sync
Group=hamsd_aprs_sync
WorkingDirectory=/opt/hams/daemons/aprs_is_sync

# Code audit, 2026-10-04: aprs_is_sync/main.py reads no environment variable of its own and it reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=aprs_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/aprs_sync/aprs_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/aprs_is_sync/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aprs.is.sync

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/au.pii.sync.service",
            "external_fetch": "designed to fetch operator data from an external source (stubbed today)",
            "content": """\
[Unit]
Description=Ham Radio Australia PII Sync Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
# Its own account (hamsd_au_pii), Phase 3 of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md (hams_com). It writes no file, so the unit has no writable path.
ProtectProc=invisible
ProcSubset=pid
# Group-readable output (0640): odoo is in the account's group and may read, never write.
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
Type=oneshot
User=hamsd_au_pii
Group=hamsd_au_pii
WorkingDirectory=/opt/hams/daemons/au_pii_sync

# Code audit, 2026-10-04: daemons/au_pii_sync/main.py reads no environment variable of its own and reaches Odoo through hams_config (ODOO_URL, DB_NAME, the key file). No database, Redis, RabbitMQ or PowerDNS credential and none of the secrets in core.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/au_pii/au_pii_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/au_pii_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=au.pii.sync

""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/au.pii.sync.timer",
            "external_fetch": "activates au.pii.sync.service",
            "content": """\
[Unit]
Description=Run the Australia PII Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/adif.ingress.service",
            "content": """\
[Unit]
Description=Ham Radio ADIF Upload Ingress Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/spool /opt/hams/spool/adif_uploads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/adif_ingress

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=logbook_api_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/logbook_api_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Defense in depth for the intermittent boot-time ModuleNotFoundError crash loop
# root-caused 2026-10-02 (night_shift_todo's
# adif-ingress-intermittent-aiohttp-modulenotfound-crash-loop report): the real fix is
# the MANIFEST apt_packages entry now guaranteeing python3-aiohttp is actually
# installed before this unit is ever started (see infrastructure.py), but this daemon's
# own ExecStart does a real, unconditional `import aiohttp` at module load, before
# main() runs anything -- unlike this unit's sibling daemons (adif.processor.service,
# gdpr.csv.export.service), it had no ExecStartPre resource check at all. Retries a
# plain import of the one dependency that actually crashed in production for up to
# ten seconds (matching this unit's own RestartSec cadence) before letting ExecStart
# run, so this unit waits out any future transient unavailability of its own real
# runtime dependency internally instead of climbing NRestarts via the full
# Restart=always crash-loop path; a genuinely still-missing package still fails loudly
# (re-raises the same ModuleNotFoundError) rather than retrying forever.
ExecStartPre=/bin/bash -c 'n=0; while ! /usr/bin/python3 -c "import aiohttp" >/dev/null 2>&1; do n=$$((n + 1)); [ "$$n" -ge 10 ] && exec /usr/bin/python3 -c "import aiohttp"; sleep 1; done'

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/adif_ingress/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=adif.ingress

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/hams.data.relay.service",
            "content": """\
[Unit]
Description=Hams.com Live Map Data Relay (Aircraft/Marine/APRS)
After=network.target redis-server.service
Requires=redis-server.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/hams_data_relay

EnvironmentFile=-/opt/hams/etc/redis.env

ExecStart=/opt/hams/daemons/hams_data_relay/target/release/hams_data_relay

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.data.relay

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/hams.relay.bridge.service",
            "content": """\
[Unit]
Description=Hams.com Central Relay Bridge (Browser <-> Local Hardware Relay)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/hams_relay_bridge

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/odoo.env
EnvironmentFile=-/opt/hams/etc/bridge.env

ExecStart=/opt/hams/daemons/hams_relay_bridge/target/release/hams_relay_bridge

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.relay.bridge

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # hams_com daemons/hams_auth_gateway: the auth.hams.com certificate login gateway.
            # Text identical to hams_com daemons/hams_auth_gateway/packaging/
            # hams-auth-gateway.service (hams_com's daemons/test_daemon_provisioning_coverage.py
            # fails if the two drift). Like every .service here it is only linked into
            # /etc/systemd/system, never enabled: it cannot start until the release steps that
            # this public repository cannot perform are done (the auth.hams.com DNS record, its
            # TLS certificate, /etc/hams/auth/gateway.toml, the ARRL anchors, the token secret,
            # and `ufw allow 443/tcp` -- hams_com docs/proposals/PROVISION_PRODUCTION_NOTES.md,
            # "the gateway replaces nginx on auth.hams.com"). Then: systemctl enable --now.
            "path": "/opt/hams/systemd/hams-auth-gateway.service",
            "content": """\
# systemd unit for hams_auth_gateway (auth.hams.com). Provisioned by hams_shared/tools/infrastructure.py's
# MANIFEST as /opt/hams/systemd/hams-auth-gateway.service; daemons/hams_auth_gateway/packaging/
# hams-auth-gateway.service in hams_com must stay identical (daemons/test_daemon_provisioning_coverage.py).
[Unit]
Description=hams.com certificate login gateway (auth.hams.com)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/hams/daemons/hams_auth_gateway/target/release/hams_auth_gateway /etc/hams/auth/gateway.toml
Restart=always
RestartSec=5

# Unprivileged. Port 443 is bound with CAP_NET_BIND_SERVICE only.
User=hams-auth
Group=hams-auth
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Hardening: no new privileges, read-only system, private /tmp and devices, nothing writable at all.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
PrivateUsers=no
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
RestrictAddressFamilies=AF_INET AF_INET6
ReadOnlyPaths=/etc/hams/auth
UMask=0077

# Limits (the daemon has its own per-source and total connection limits too).
LimitNOFILE=4096
TasksMax=256
MemoryMax=256M

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # hams_com daemons/shack_console: the shack console's static server for https://hams.com/console
            # (the Cloudflare tunnel route `^/console(/|$)` -> http://localhost:8767, prepared in ham_base's
            # data and pushed by Bruce from the Odoo tunnel form). Text identical to hams_com
            # daemons/shack_console/packaging/shack-console.service (hams_com's
            # daemons/test_daemon_provisioning_coverage.py fails if the two drift). Like every .service here it
            # is only linked into /etc/systemd/system, never enabled. It binds 127.0.0.1 in the code we ship (no
            # setting can change that), takes no secrets, and fetches nothing, so it is not external_fetch.
            "path": "/opt/hams/systemd/shack-console.service",
            "content": """\
# systemd unit for shack_console_server (https://hams.com/console, through the Cloudflare tunnel). Provisioned by
# hams_shared/tools/infrastructure.py's MANIFEST as /opt/hams/systemd/shack-console.service;
# daemons/shack_console/packaging/shack-console.service in hams_com must stay identical
# (daemons/test_daemon_provisioning_coverage.py). Linked, never enabled by provisioning.
[Unit]
Description=hams.com shack console static server (loopback 127.0.0.1:8767)
After=network.target

[Service]
Type=simple
ExecStart=/opt/hams/daemons/shack_console/target/release/shack_console_server
Restart=always
RestartSec=5

# Listens on 127.0.0.1 only: the address is fixed in the program (shack_console::bind_loopback), not read from
# any setting. The two lines below are a second, independent layer that keeps the service off every other
# address even if the program were ever changed.
IPAddressDeny=any
IPAddressAllow=localhost
RestrictAddressFamilies=AF_INET

# No account of its own to manage, nothing to read or write: the console is embedded in the binary.
DynamicUser=yes
# hams1's /opt/hams is mode 750 with an execute-only ACL for the hams_traverse group; without this group the
# dynamic user cannot exec the binary under /opt/hams (found on the live deploy, 2026-10-05).
SupplementaryGroups=hams_traverse
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
PrivateUsers=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
UMask=0077

LimitNOFILE=4096
TasksMax=256
MemoryMax=256M

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams.simulated.band.service",
            "content": """\
[Unit]
Description=Hams.com Simulated Band WebRTC SFU
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
# AF_NETLINK is required: the WebRTC library lists the host's network interfaces over a
# netlink socket to build its ICE host candidates. Without it the SFU logs "Failed to
# enumerate local interfaces ... Address family not supported (os error 97)" and offers a
# browser no host candidate, so audio connects only when a public STUN server answers and
# never on a network without internet. (Production hams1 carried a hand-placed drop-in,
# hams.simulated.band.service.d/30-netlink.conf, for this; this line makes it unnecessary.)
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
CapabilityBoundingSet=
ReadWritePaths=
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/hams_simulated_band

# Added 2026-10-02 -- like hams.simulated.bots.service's own 2026-09-30 fold-in
# below, this previously existed ONLY in a hand-maintained, untracked drop-in
# on hams1 (/etc/systemd/system/hams.simulated.band.service.d/bot-keys.conf,
# dated 2026-09-24). It is a file *path*, not a secret: the per-host bot key
# lines (minted with daemons/hams_simulated_band/tools/new_bot_key.sh) stay
# hand-placed at this path, mode 0600/0640, never in this unit text. Without
# the variable the SFU starts but refuses every QAI (bot) client; with it set
# and the file missing, the SFU fails fast at startup by design (bot_auth.rs).
# Since 2026-10-04 a production-style provisioning run also enables this unit at boot
# (boot_service_unit_names(): production must get the band back after a reboot; a test
# environment and a --hold-odoo run still only link it). A host without the key file is
# still safe: the SFU starts and refuses every bot, as described above.
Environment="HAMS_BAND_BOT_KEYS_FILE=/etc/hams-band/band_bot_keys"

ExecStart=/opt/hams/daemons/hams_simulated_band/target/release/hams_simulated_band

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.simulated.band

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/hams.simulated.bots.service",
            "content": """\
[Unit]
Description=Hams.com Simulated Band Bot Fleet (STT/TTS via WebRTC)
After=network.target hams.simulated.band.service
Requires=hams.simulated.band.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/cache/whisper
# The robots' one process runs about 24 threads per robot just idling (ten robots: about 242,
# measured with `ps -o nlwp`) and gains about 16 per person who transmits: the band server
# adds a media line per person, every robot's aiortc connection starts a receiver and decoder
# thread for it, and aiortc never stops a receiver whose line the server retired. A small
# limit ends in "RuntimeError: can't start new thread" inside aiortc, which leaves a robot's
# new receiver silently deaf. 4096 is the Pacificon demo kit's measured-safe value; the limit
# is a runaway guard, not a sizing tool. Follow-up: night_shift_todo/medium/
# bots-threads-grow-with-every-person-and-the-shipped-tasksmax-is-below-the-baseline-1f6c9e83.md
# (recycle a connection after N retired lines, or stop the retired receivers).
TasksMax=4096
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/hams_simulated_bots

Environment="HF_HOME=/opt/hams/cache/whisper"
# Speech models live at a stable path outside the daemon tree (MANIFEST["model_files"], placed by
# `provision.py --fetch-models` on a production host only). Set, the daemon loads exactly these files and
# never reaches for the network; a missing file is a loud error, not a silent download.
Environment="HAMS_PIPER_VOICE_PATH=/opt/hams/models/piper/en_US-lessac-low.onnx"
Environment="HAMS_WHISPER_MODEL_DIR=/opt/hams/models/faster-whisper-tiny.en"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="
# Added 2026-09-30 -- these previously existed ONLY in a hand-maintained,
# untracked systemd drop-in (/etc/systemd/system/hams.simulated.bots.service.d/
# band-config.conf) on hams1 itself, invisible to this MANIFEST. None of these
# four values are secrets (two are file *paths*, matching this codebase's own
# ODOO_KEY_FILE convention elsewhere in this MANIFEST; one is a loopback URL;
# one is a plain hostname label) -- the actual secrets live at the paths named
# here, never in this unit text. See night_shift_history.md (hams_com) for the
# full story of why this drop-in existed unversioned in the first place.
Environment="HAMS_SIMULATED_BAND_WS_URL=ws://localhost:3000/ws"
Environment="HAMS_SIMULATED_BAND_KEY_FILE=/etc/hams-bots/band.key"
Environment="HAMS_SIMULATED_BOT_HOST_NAME=hams1-local-bots"
Environment="HAMS_SIMULATED_BOT_COORDINATOR_TOKEN_FILE=/etc/hams-bots/coordinator.key"
Environment="ODOO_URL=https://hams.com"

ExecStart=/usr/bin/python3 /opt/hams/daemons/hams_simulated_bots/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.simulated.bots

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # Added 2026-09-30: found via test_daemon_provisioning_coverage.py that this real,
            # already-built child-safety monitoring daemon (docs/proposals/
            # CHILD_SAFETY_COMMUNICATIONS_CONSENT.md section G.2, "The Official Observer") had zero
            # systemd unit and zero running process on production -- a documented safety feature
            # that was never actually deployed. Mirrors hams.simulated.bots.service's own
            # environment exactly (same connection settings, same STT backend, same
            # safety-classification module -- see observer.py's own module docstring for why they
            # share configuration), differing only in ExecStart/Description/SyslogIdentifier and
            # depending on hams.simulated.band.service directly (the underlying SFU) rather than on
            # hams.simulated.bots.service, since the Observer is architecturally independent of the
            # conversational bot fleet, not a client of it.
            "path": "/opt/hams/systemd/hams.simulated.observer.service",
            "content": """\
[Unit]
Description=Hams.com Simulated Band Official Observer (silent safety monitor)
After=network.target hams.simulated.band.service
Requires=hams.simulated.band.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/cache/whisper
# One band connection and one transcriber (no robots), so far fewer threads than the bot fleet,
# but its receiver count also grows with each person who transmits (see
# hams.simulated.bots.service above). 1024 matches the Pacificon demo kit's Observer.
TasksMax=1024
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/hams_simulated_bots

Environment="HF_HOME=/opt/hams/cache/whisper"
# Speech models live at a stable path outside the daemon tree (MANIFEST["model_files"], placed by
# `provision.py --fetch-models` on a production host only). Set, the daemon loads exactly these files and
# never reaches for the network; a missing file is a loud error, not a silent download.
Environment="HAMS_PIPER_VOICE_PATH=/opt/hams/models/piper/en_US-lessac-low.onnx"
Environment="HAMS_WHISPER_MODEL_DIR=/opt/hams/models/faster-whisper-tiny.en"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="
# Same rationale as hams.simulated.bots.service's own 2026-09-30 comment above --
# only the subset observer.py itself actually reads (confirmed by reading the
# file: it imports `main as bots_main` for create_safety_ticket(), which needs
# ODOO_URL, but never reads HAMS_SIMULATED_BOT_HOST_NAME or
# HAMS_SIMULATED_BOT_COORDINATOR_TOKEN_FILE -- those are BotClient-specific).
Environment="HAMS_SIMULATED_BAND_WS_URL=ws://localhost:3000/ws"
Environment="HAMS_SIMULATED_BAND_KEY_FILE=/etc/hams-bots/band.key"
Environment="ODOO_URL=https://hams.com"

ExecStart=/usr/bin/python3 /opt/hams/daemons/hams_simulated_bots/observer.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.simulated.observer

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            # Bringing an already-live, hand-provisioned file under tracked management --
            # night_shift_todo/low/claude-cli-wrapper-scripts-untracked-on-hams1-b71e4a92.md.
            # Content copied verbatim from the real, already-working file on hams1 (read directly,
            # confirmed clean, no secrets embedded) -- not rewritten. `hams_ai_agent` has no login
            # shell (nologin, by design), so `sudo -i` can't be used to get one, and plain
            # `sudo -u` does not change the working directory either -- Claude Code's own
            # workspace/CLAUDE.md discovery and MCP config resolution are CWD/HOME-relative, so
            # without this fix it would inherit whatever directory and HOME the calling shell
            # happened to have. Only exists on hams1 -- the dedicated `hams_ai_agent` account is
            # itself prod-only, not provisioned in this MANIFEST yet (a separate, larger gap, see
            # night_shift_todo/medium/service-account-creation-and-sudoers-sandboxes-are-
            # untracked-infra-c3a9f714.md), so this file is `environments: ["prod"]` only.
            #
            # [@ANCHOR: infrastructure:claude_wrapper_ci_oauth_token]
            # Re-synced 2026-10-02 (night_shift_todo/high/wrapper-scripts-manifest-drifted-from-
            # live-hams1-fix-2d8e4a71.md): the claude-service-account-oauth-refresh-unreliable
            # pass edited this file (and its event-agent sibling) directly on hams1 to prefer a
            # provisioned `~/.claude/ci_oauth_token` (from `claude setup-token`) as
            # CLAUDE_CODE_OAUTH_TOKEN, falling back to the old behavior when absent. Content below
            # is again copied verbatim from the live file and diffed byte-for-byte against it, so
            # a re-provisioning run no longer reverts that fix. It contains no `{`/`}`, so
            # format_env() passes it through unchanged.
            "path": "/usr/local/sbin/run-hams-ai-agent-claude.sh",
            "content": """\
#!/bin/bash
# Runs the real Claude Code CLI as hams_ai_agent with its CWD and HOME set to its own home
# directory. Same reasoning as run-hams-event-agent-claude.sh: plain sudo -u does not change the
# working directory, and this account has no login shell (nologin, by design), so sudo -i cannot
# be used either. Claude Code's own workspace/CLAUDE.md discovery and MCP config resolution are
# CWD/HOME-relative, so without this fix it would inherit whatever directory and HOME the calling
# shell happened to have.
cd /home/hams_ai_agent || exit 1
export HOME=/home/hams_ai_agent

# If a long-lived CI token (from `claude setup-token`, run once interactively by Bruce in his own
# browser -- never generated by any agent session, see
# night_shift_questions/open/claude-cli-service-accounts-need-setup-token-9c1e4f7b.md) has been
# provisioned for this account, prefer it over the normal interactive-login session below: it is a
# static, ~1-year-lived credential immune to the exact failure class
# night_shift_todo/high/claude-service-account-oauth-refresh-unreliable-c4f8e912.md found live --
# Claude Code CLI's own headless/non-interactive OAuth refresh is an acknowledged, currently
# unresolved upstream limitation (confirmed 2026-10-02 against two real, closed
# anthropics/claude-code issues matching this exact error text and exact headless-service-account
# shape: #79685 "closed as not planned", #60503/#31095 "the refresh token is never used" in
# non-interactive mode), not something this codebase can fix directly. Falls back to the normal
# `.claude/.credentials.json`-based session (today's only working option until that one-time setup
# happens) when no token file has been provisioned yet -- this account behaves EXACTLY as before
# in that case.
CI_OAUTH_TOKEN_FILE=/home/hams_ai_agent/.claude/ci_oauth_token
if [ -s "$CI_OAUTH_TOKEN_FILE" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN
    CLAUDE_CODE_OAUTH_TOKEN="$(cat "$CI_OAUTH_TOKEN_FILE")"
fi

exec /home/hams_ai_agent/.local/bin/claude "$@"
""",
            "owner": "root:root",
            "mode": "755",
            "environments": ["prod"],
        },
        {
            # Same gap, same fix, sibling account -- see the run-hams-ai-agent-claude.sh entry
            # above for the full reasoning (including its 2026-10-02 ci_oauth_token re-sync
            # anchor). Content likewise copied verbatim from hams1.
            "path": "/usr/local/sbin/run-hams-event-agent-claude.sh",
            "content": """\
#!/bin/bash
# Runs the real Claude Code CLI as hams_event_agent with its CWD and HOME set to its own home
# directory. Same reasoning as run-hams-event-agent.sh (the agy wrapper): plain sudo -u does not
# change the working directory, and this account has no login shell (nologin, by design), so
# sudo -i cannot be used either -- a nologin shell makes -i refuse with "This account is
# currently not available." Claude Code's own workspace/CLAUDE.md discovery and MCP config
# resolution are CWD/HOME-relative, so without this fix it would inherit whatever directory and
# HOME the calling shell happened to have.
cd /home/hams_event_agent || exit 1
export HOME=/home/hams_event_agent

# If a long-lived CI token (from `claude setup-token`, run once interactively by Bruce in his own
# browser -- never generated by any agent session, see
# night_shift_questions/open/claude-cli-service-accounts-need-setup-token-9c1e4f7b.md) has been
# provisioned for this account, prefer it over the normal interactive-login session below: it is a
# static, ~1-year-lived credential immune to the exact failure class
# night_shift_todo/high/claude-service-account-oauth-refresh-unreliable-c4f8e912.md found live --
# Claude Code CLI's own headless/non-interactive OAuth refresh is an acknowledged, currently
# unresolved upstream limitation (confirmed 2026-10-02 against two real, closed
# anthropics/claude-code issues matching this exact error text and exact headless-service-account
# shape: #79685 "closed as not planned", #60503/#31095 "the refresh token is never used" in
# non-interactive mode), not something this codebase can fix directly. Falls back to the normal
# `.claude/.credentials.json`-based session (today's only working option until that one-time setup
# happens) when no token file has been provisioned yet -- this account behaves EXACTLY as before
# in that case. This is the exact account that died twice already (see that to-do's own
# 2026-10-01/2026-10-02 entries), so it is the more urgent of the two to move onto this token.
CI_OAUTH_TOKEN_FILE=/home/hams_event_agent/.claude/ci_oauth_token
if [ -s "$CI_OAUTH_TOKEN_FILE" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN
    CLAUDE_CODE_OAUTH_TOKEN="$(cat "$CI_OAUTH_TOKEN_FILE")"
fi

exec /home/hams_event_agent/.local/bin/claude "$@"
""",
            "owner": "root:root",
            "mode": "755",
            "environments": ["prod"],
        },
        {
            # night_shift_questions/answered/credential-touch-timer-unattended-schedule-f3c8a291.md
            # (Bruce, 2026-10-01: "Build the touch timer") / night_shift_todo/high/
            # claude-service-account-oauth-refresh-unreliable-c4f8e912.md. See
            # daemons/credential_touch/main.py's own module docstring for the full reasoning.
            #
            # Deliberately NOT the ADR-0070 standard hardening set: NoNewPrivileges is omitted
            # (not set true) and CapabilityBoundingSet is narrowed to exactly
            # CAP_SETUID/CAP_SETGID rather than emptied, because this unit's whole job is to run
            # `sudo -u hams_ai_agent`/`sudo -u hams_event_agent` -- NoNewPrivileges=true disables
            # the effect of setuid binaries (including sudo) for the whole process tree regardless
            # of capabilities, and an emptied CapabilityBoundingSet would stop a setuid-root
            # binary from ever regaining root's capabilities even with NoNewPrivileges off. Both
            # `sudo -u` calls are restricted to one exact, already-live, narrowly-scoped command
            # each via real sudoers.d grants (confirmed directly on hams1, not assumed):
            #   odoo ALL=(hams_ai_agent) NOPASSWD: /usr/local/sbin/run-hams-ai-agent-claude.sh
            #   odoo ALL=(hams_event_agent) NOPASSWD: /usr/local/sbin/run-hams-event-agent-claude.sh
            # ReadWritePaths carves out exactly the two accounts' home directories (where the
            # Claude Code CLI's own refreshed .credentials.json actually gets written), rather
            # than weakening ProtectHome=read-only more broadly than that one real need.
            #
            # No Odoo/DB/Redis/RabbitMQ EnvironmentFile lines: this daemon never touches the
            # database, Redis, or RabbitMQ at all, so it carries none of the credentials that
            # would grant it.
            "path": "/opt/hams/systemd/credential.touch.timer.service",
            "external_fetch": "refreshes a third-party OAuth session, rotating its token",
            "content": """\
[Unit]
Description=Periodic Touch of Claude Code CLI Service-Account OAuth Sessions (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction, with ONE deliberate, documented exception -- see this
# entry's own comment above for the full reasoning.
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=CAP_SETUID CAP_SETGID
ReadWritePaths=/home/hams_ai_agent /home/hams_event_agent
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/credential_touch

ExecStart=/usr/bin/python3 /opt/hams/daemons/credential_touch/main.py

StandardOutput=journal
StandardError=journal
SyslogIdentifier=credential.touch.timer
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/credential.touch.timer.timer",
            "external_fetch": "activates credential.touch.timer.service",
            "content": """\
[Unit]
Description=Periodic Touch of Claude Code CLI Service-Account OAuth Sessions Twice Daily

[Timer]
# Matches the already-established hams-aws-session-touch-interval cadence precedent for exactly
# this class of problem (an OAuth-style session that needs periodic real use to stay refreshed),
# and sits well inside the ~34-hour real failure window
# claude-service-account-oauth-refresh-unreliable-c4f8e912.md found live.
OnCalendar=*-*-* 00,12:00:00
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # AI ticket triage on a timer. Bruce, 2026-10-04 (NIGHT_PLAN decision 199, answering
            # night_shift_questions/answered/ticket-triage-agent-unsupervised-schedule-735ffee2.md):
            # "run on a timer", DRAFTS ONLY. daemons/ticket_triage_agent/README.md has the full
            # contract (what runs, caps, kill switch, what to do before enabling).
            #
            # The model never runs as this unit's user: ticket_triage_agent/main.py reaches the
            # Claude Code CLI through `sudo -u hams_ai_agent run-hams-ai-agent-claude.sh`, the
            # dedicated nologin, no-sudo account (system_accounts above), and the CLI's only tool is
            # the hams_ticket_triage_mcp server (post_internal_note is its one write; propose_fix is
            # closed). This unit is the supervisor, so it follows credential.touch.timer's shape:
            # NoNewPrivileges omitted and CapabilityBoundingSet narrowed to SETUID/SETGID, because
            # both the `sudo -u hams_ai_agent` hop and the MCP server's own `sudo -u odoo` hop need a
            # setuid binary to work. ReadWritePaths: hams_ai_agent's home (the CLI writes session
            # state there) and the triage repo clones (read_repo runs `git fetch`). The state
            # directory (lock, daily counters, ledger) is systemd's StateDirectory=.
            #
            # Kill switch: ConditionPathExists=! skips the run cleanly while
            # /opt/hams/etc/ticket_triage.disabled exists, and main.py checks it first as well.
            # Limits come from EnvironmentFile=-/opt/hams/etc/ticket_triage.env
            # (HAMS_TRIAGE_MAX_CLI_PER_DAY, _MAX_TICKETS_PER_DAY, _MAX_TICKETS_PER_RUN).
            #
            # opt_in: provisioning links this unit but never enables or starts it unless named with
            # provision.py --enable-opt-in (it spends Claude quota and reads untrusted mail).
            # Prod-only: the account, wrapper and sudoers grants exist only on hams1.
            "path": "/opt/hams/systemd/ticket.triage.service",
            "external_fetch": "calls Anthropic through the Claude Code CLI on every run",
            "opt_in": "spends Claude Code subscription quota; reads untrusted ticket text",
            "content": """\
[Unit]
Description=Hams.com AI Ticket Triage, Drafts Only (One-Shot)
After=network.target
ConditionPathExists=!/opt/hams/etc/ticket_triage.disabled

[Service]
# ADR-0070 OS-Level Daemon Restriction, with the credential.touch.timer exception (no
# NoNewPrivileges, SETUID/SETGID kept) -- see this entry's own comment above.
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=CAP_SETUID CAP_SETGID
ReadWritePaths=/home/hams_ai_agent -/opt/hams/ai_triage_repo
StateDirectory=hams-ticket-triage
StateDirectoryMode=0700
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/ticket_triage_agent
EnvironmentFile=-/opt/hams/etc/ticket_triage.env
# Longer than the pre-check (60s) plus the CLI's own hard timeout (600s) and kill grace.
TimeoutStartSec=900

ExecStartPre=/usr/bin/python3 /opt/hams/daemons/ticket_triage_agent/main.py --start-test
ExecStart=/usr/bin/python3 /opt/hams/daemons/ticket_triage_agent/main.py

StandardOutput=journal
StandardError=journal
SyslogIdentifier=ticket.triage
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # FALLBACK cadence, every four hours (Bruce, NIGHT_PLAN decision 222: triage is now event-driven,
            # ticket.triage.path below, and this timer only catches a ticket whose wake-up was lost or
            # dropped, for example while the daemon was disabled, the spool was full, or the daily cap had
            # been reached). A run with no waiting ticket spends no Claude quota (cheap pre-check), so even
            # a frequent fallback costs nothing; the hard ceilings are the daemon's daily caps (12 model
            # calls and 20 tickets a day by default). Persistent=false: a missed run is simply skipped,
            # never replayed as a burst after downtime. RandomizedDelaySec spreads the start.
            "path": "/opt/hams/systemd/ticket.triage.timer",
            "external_fetch": "activates ticket.triage.service",
            "opt_in": "activates ticket.triage.service, which spends Claude Code quota",
            "content": """\
[Unit]
Description=Hams.com AI Ticket Triage Fallback Every Four Hours (drafts only)

[Timer]
OnCalendar=*-*-* 00/4:07:00
Persistent=false
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # EVENT trigger for the AI ticket triage (Bruce, NIGHT_PLAN decision 222: "Can we get a
            # notification of an incoming ticket from Odoo and run it immediately?"). hams_helpdesk writes
            # `ticket-<id>.json` (the id only) to /opt/hams/spool/ticket_triage after a ticket commits; this
            # path unit starts ticket.triage.event.service the moment one exists. Why a spool and a path unit
            # and not RabbitMQ: the daemon stays a one-shot (no always-on consumer, no broker credential in
            # the triage service), the filesystem is up whenever Odoo is, backup_management already uses this
            # shape, and the file stays on disk until something consumes it, so a notification cannot be lost
            # by the daemon being down. systemd starts the service at most once at a time; files written while
            # it runs survive and start exactly one follow-up run when it exits. The service deletes the files
            # it has seen before it does anything else, so a reached cap or the kill switch cannot leave the
            # glob matching and re-fire this unit in a loop.
            #
            # opt_in and external_fetch exactly like the timer: enabling it lets ticket text start model runs.
            # Prod only: the account, wrapper, spool directory and sudoers grants exist only on hams1.
            "path": "/opt/hams/systemd/ticket.triage.path",
            "external_fetch": "activates ticket.triage.event.service",
            "opt_in": "activates ticket.triage.event.service, which spends Claude Code quota",
            "content": """\
[Unit]
Description=Hams.com AI Ticket Triage Wake-Up (a new ticket was created)

[Path]
PathExistsGlob=/opt/hams/spool/ticket_triage/ticket-*.json
Unit=ticket.triage.event.service
# Bound how often a wake-up can fire the service; the service's own debounce is the real batching.
TriggerLimitIntervalSec=60
TriggerLimitBurst=10

[Install]
WantedBy=paths.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # The unit ticket.triage.path starts. Same sandbox and supervisor shape as ticket.triage.service
            # (see its comment), with three differences, each deliberate:
            #   * ExecStart is `main.py --event`: debounce (wait for a quiet period so a burst of tickets
            #     becomes one run), consume the wake-up files, then the same guarded pass (kill switch, daily
            #     and per-run caps, flock, pre-check, internal-note-only write surface);
            #   * NO ConditionPathExists= kill switch and NO ExecStartPre: both would let the service be
            #     skipped or fail BEFORE it deleted the wake-up files, and a path unit whose files are never
            #     consumed re-fires and reaches its trigger limit. The kill switch is checked first, in
            #     main.py, which then deletes the files and exits 0;
            #   * a longer TimeoutStartSec: debounce (max 120 s) + wait for the lock (max 700 s) + pre-check
            #     (60 s) + the CLI's hard timeout (600 s) + kill grace.
            # ReadWritePaths adds the spool directory, so the service can delete the wake-up files.
            "path": "/opt/hams/systemd/ticket.triage.event.service",
            "external_fetch": "calls Anthropic through the Claude Code CLI on every run",
            "opt_in": "spends Claude Code subscription quota; reads untrusted ticket text",
            "content": """\
[Unit]
Description=Hams.com AI Ticket Triage on a New Ticket, Drafts Only (One-Shot)
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction, with the credential.touch.timer exception (no
# NoNewPrivileges, SETUID/SETGID kept) -- see ticket.triage.service's own comment.
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=CAP_SETUID CAP_SETGID
ReadWritePaths=/home/hams_ai_agent -/opt/hams/ai_triage_repo /opt/hams/spool/ticket_triage
StateDirectory=hams-ticket-triage
StateDirectoryMode=0700
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/ticket_triage_agent
EnvironmentFile=-/opt/hams/etc/ticket_triage.env
Environment="HAMS_TRIAGE_SPOOL_DIR=/opt/hams/spool/ticket_triage"
# Debounce 120 + lock wait 700 + pre-check 60 + CLI timeout 600 + kill grace, with margin.
TimeoutStartSec=1800

ExecStart=/usr/bin/python3 /opt/hams/daemons/ticket_triage_agent/main.py --event

StandardOutput=journal
StandardError=journal
SyslogIdentifier=ticket.triage.event
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # Automatic club-website crawl for repeaters and events. Bruce, NIGHT_PLAN 203, 2026-10-04:
            # "We were supposed to crawl all club sites for repeaters and events automatically. Not just
            # three, and not manually." hams_com docs/proposals/AUTOMATIC_CLUB_CRAWL.md has the design and
            # daemons/club_crawl/README.md the contract (politeness, caps, what is automatic, what waits
            # for a person, how to switch it off and how to enable it).
            #
            # Its own account (hamsd_club_crawl). The model never runs as that account: the daemon
            # reaches the Claude Code CLI through `sudo -u hams_ai_agent run-hams-ai-agent-claude.sh`
            # (club-crawl-claude-sandbox grant below), as credential.touch.timer and ticket.triage do, with
            # --tools "" and --strict-mcp-config (no tool, no MCP server: a bounded text completion over
            # untrusted pages). So this unit has the documented `agent_sudo` shape (FAMILY_ACCOUNT_UNITS):
            # NoNewPrivileges omitted, CapabilityBoundingSet narrowed to SETUID/SETGID, the agent's home
            # the only ReadWritePaths=. Everything else of the family isolation set is kept, and only
            # common.env is loaded: no database, Redis, RabbitMQ or Cloudflare credential reaches it.
            #
            # Kill switch: ConditionPathExists=! skips the run cleanly while /opt/hams/etc/club_crawl.disabled
            # exists. ExecStartPre checks the Claude CLI wrapper runs under this sandbox, with no model call,
            # so a sandbox that breaks sudo fails the unit at once instead of recording a failed crawl.
            #
            # opt_in: provisioning links this unit but never enables or starts it unless named with
            # provision.py --enable-opt-in (it fetches from thousands of third-party servers and spends
            # Claude Code subscription quota). Prod-only: the agent account and wrapper exist only on hams1.
            # DAEMON_ARGS carry the caps: 30 sites and 60 model calls a run, 300 sites and 400 model calls a
            # day. There is no review queue: every extracted item is accepted or rejected by a fixed
            # data-quality rule, and the coverage report counts each rejection by its reason.
            "path": "/opt/hams/systemd/club.crawl.service",
            "external_fetch": "fetches club websites from thousands of third-party servers and calls Anthropic through the Claude Code CLI",
            "opt_in": "fetches third-party club websites on a schedule and spends Claude Code subscription quota",
            "content": """\
[Unit]
Description=Hams.com Automatic Club Website Crawl, Repeaters and Events (One-Shot)
After=network.target
ConditionPathExists=!/opt/hams/etc/club_crawl.disabled

[Service]
# ADR-0070 OS-Level Daemon Restriction and the daemon-family isolation set, with the documented
# agent_sudo exception (no NoNewPrivileges, SETUID/SETGID kept) -- see this entry's own comment above.
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=CAP_SETUID CAP_SETGID
ProtectProc=invisible
ProcSubset=pid
UMask=0027
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
ReadWritePaths=/home/hams_ai_agent
Type=oneshot
User=hamsd_club_crawl
Group=hamsd_club_crawl
WorkingDirectory=/opt/hams/daemons/club_crawl
# 30 sites at a few requests each, 6 s apart per host, plus up to 60 model calls of up to 120 s.
TimeoutStartSec=2h

# main.py and hams_config.py read SYSTEM_USER_AGENT, ODOO_URL and DB_NAME; nothing else of core.env,
# db.env or odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=club_crawl_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/club_crawl/club_crawl_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS=--max-sites=30 --max-sites-per-day=300 --max-model-calls-per-run=60 --max-model-calls-per-day=400"

ExecStartPre=/usr/bin/python3 /opt/hams/daemons/club_crawl/main.py --start-test
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, hamsd_club_crawl.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/hamsd_club_crawl
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/hamsd_club_crawl/hamsd_club_crawl.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/hamsd_club_crawl/hamsd_club_crawl.key --jwk-file /opt/hams/spool/web_bot_auth_keys/hamsd_club_crawl/hamsd_club_crawl.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/club_crawl/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=club.crawl
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # Cadence: every two hours. A run visits at most 30 due sites, so the registry's 4,693
            # crawlable rows take about 16 days for the first full pass (the daily cap of 300 sites binds, not the 360 a day the
            # timer allows),
            # after which only sites whose 30-day interval has passed come due, and an unchanged site costs
            # conditional requests and no model call. A run with nothing due spends nothing.
            # Persistent=false: a missed run is skipped, never replayed as a burst after downtime.
            "path": "/opt/hams/systemd/club.crawl.timer",
            "external_fetch": "activates club.crawl.service",
            "opt_in": "activates club.crawl.service, which fetches third-party sites and spends Claude Code quota",
            "content": """\
[Unit]
Description=Hams.com Automatic Club Website Crawl (every two hours; a run with nothing due spends nothing)

[Timer]
OnCalendar=*-*-* 00/2:15:00
Persistent=false
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # AI correction of scraped ARRL hamfest listings on a timer (hams_com daemons/event_ai_enrichment;
            # Bruce, NIGHT_PLAN 203: the club crawl became automatic and this job is wanted automatic too,
            # night_shift_todo schedule-event-ai-enrichment-on-a-timer-after-the-club-crawl-3b8d7e15).
            # Different job from club.crawl: this one corrects an event listing by finding the event's own page
            # (the Python crawl plus one batched Gemini-grounded search for events ARRL's link cannot lead from),
            # then asks the Claude Code CLI to write the correction through the event enrichment MCP server.
            #
            # Account: `odoo`, on the SHARED_ODOO_ACCOUNT_UNITS list, as ticket.triage does and for the same two
            # reasons. (1) The unit reaches the CLI through `sudo -u hams_event_agent run-hams-event-agent-claude.sh`
            # (the existing odoo grant), so it has the credential.touch.timer exception: NoNewPrivileges omitted,
            # CapabilityBoundingSet narrowed to SETUID/SETGID, hams_event_agent's home the only ReadWritePaths=.
            # (2) Its Odoo key is event_ai_correction_service_internal.key, the SAME file the MCP server reads
            # (run-event-enrichment-mcp.sh runs it as odoo through a sudo hop): a hamsd_ family account would need
            # that key moved into a family key directory (NIGHT_PLAN 226) and a second key registered for the same
            # service account, which is its own reviewed change. Moving this unit to its own account belongs to that
            # later phase of docs/proposals/DAEMON_OS_ISOLATION_PLAN.md; the model itself never runs as odoo.
            #
            # Guard rails live in main.py `--scheduled`: kill switch file /opt/hams/etc/event_ai_enrichment.disabled
            # (also ConditionPathExists=! below), one run at a time, at most 30 model dispatches a UTC day, and an
            # event the model left uncorrected is not asked about again for 7 days (the state directory,
            # StateDirectory=hams-event-ai-enrichment, holds the ledger). Without that ledger the same first five
            # unresolved events would be redispatched on every run. Politeness: page_cleaning.fetch_page() honors
            # robots.txt and is SSRF-safe, and a run crawls at most five events (see main.py's docstring).
            #
            # The Gemini key for the search fallback is read from odoo's own ~/.secrets/google_search/api_key.txt
            # (/var/lib/odoo/.secrets/google_search/api_key.txt); without it the fallback logs one line and is skipped.
            #
            # opt_in: provisioning links this unit but never enables or starts it unless named with
            # provision.py --enable-opt-in (it fetches third-party sites and spends Claude Code subscription quota).
            # Prod-only: hams_event_agent and its wrapper exist only on hams1. Bruce enables it.
            "path": "/opt/hams/systemd/event.ai.enrichment.service",
            "external_fetch": "fetches hamfest and club websites from third-party servers, calls Google (Gemini grounded search) and Anthropic through the Claude Code CLI",
            "opt_in": "fetches third-party websites on a schedule and spends Claude Code subscription quota",
            "content": """\
[Unit]
Description=Hams.com AI Event Enrichment, Corrects Scraped Hamfest Listings (One-Shot)
After=network.target
ConditionPathExists=!/opt/hams/etc/event_ai_enrichment.disabled

[Service]
# ADR-0070 OS-Level Daemon Restriction, with the credential.touch.timer exception (no NoNewPrivileges,
# SETUID/SETGID kept) -- see this entry's own comment above.
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=CAP_SETUID CAP_SETGID
ReadWritePaths=/home/hams_event_agent
StateDirectory=hams-event-ai-enrichment
StateDirectoryMode=0700
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/event_ai_enrichment
# Five events a run, each a short crawl, an optional shared search call and one model call of up to 240 s.
TimeoutStartSec=1h

# main.py and hams_config.py read SYSTEM_USER_AGENT, ODOO_URL and DB_NAME; nothing else of core.env, db.env or
# odoo.env is used, so none is loaded.
EnvironmentFile=/opt/hams/etc/common.env
Environment="ODOO_USER=event_ai_correction_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_ai_correction_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="EVENT_AI_ENRICHMENT_STATE_DIR=/var/lib/hams-event-ai-enrichment"
Environment="EVENT_AI_ENRICHMENT_BATCH_SIZE=5"
Environment="EVENT_AI_ENRICHMENT_MAX_EVENTS_PER_DAY=30"

ExecStartPre=/usr/bin/python3 /opt/hams/daemons/event_ai_enrichment/main.py --start-test
# Web Bot Auth: this account signs its requests to third-party servers with its own key (daemons/web_bot_auth.py); the
# public half, odoo.jwk, is read by path by web.bot.auth.directory.service and listed in hams.com's key directory.
ReadWritePaths=/opt/hams/spool/web_bot_auth_keys/odoo
Environment="HAMS_WEB_BOT_AUTH_KEY_FILE=/opt/hams/spool/web_bot_auth_keys/odoo/odoo.key"
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/web_bot_auth.py ensure-key --key-file /opt/hams/spool/web_bot_auth_keys/odoo/odoo.key --jwk-file /opt/hams/spool/web_bot_auth_keys/odoo/odoo.jwk
ExecStart=/usr/bin/python3 /opt/hams/daemons/event_ai_enrichment/main.py --scheduled

StandardOutput=journal
StandardError=journal
SyslogIdentifier=event.ai.enrichment
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # Cadence: every three hours, five events a run, at most 30 dispatches a day (main.py's cap), so a
            # backlog of a few hundred scraped listings clears in about a week and an idle day costs nothing: a
            # run with no eligible event ends at the first query. Persistent=false: a missed run is skipped, never
            # replayed as a burst after downtime.
            "path": "/opt/hams/systemd/event.ai.enrichment.timer",
            "external_fetch": "activates event.ai.enrichment.service",
            "opt_in": "activates event.ai.enrichment.service, which fetches third-party sites and spends Claude Code quota",
            "content": """\
[Unit]
Description=Hams.com AI Event Enrichment (every three hours; a run with nothing eligible spends nothing)

[Timer]
OnCalendar=*-*-* 00/3:40:00
Persistent=false
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # night_shift_todo/medium/service-account-creation-and-sudoers-sandboxes-are-
            # untracked-infra-c3a9f714.md: every real file under hams1's /etc/sudoers.d/ was
            # hand-provisioned, with no tracked source anywhere. Confirmed directly on hams1,
            # 2026-10-02 -- `ls -la /etc/sudoers.d/` lists exactly 8 custom grants (plus the
            # stock Debian `README` the `sudo` package itself installs, not tracked here since it
            # carries no project-specific content). Seven of the eight are tracked below; the
            # eighth, `odoo-agyimport-sandbox`, is deliberately NOT tracked -- see its own
            # omission comment further down, where its sibling `odoo-agy-sandbox` entry sits.
            # Content copied verbatim from each live file (read directly via `sudo cat`,
            # confirmed clean, no secrets). Real mode on hams1 is 0440 (`-r--r-----`, root:root)
            # for every one of them -- visudo's own convention for sudoers.d drop-ins, enforced
            # here the same way.
            #
            # This one grants `ai` (the account used to administer hams1 itself) full,
            # unrestricted NOPASSWD sudo -- not command-restricted like the six other tracked
            # grants below. The `ai` system account itself remains a separate, pre-existing gap
            # (it predates this MANIFEST's own tooling -- it is the account `infrastructure.py`'s
            # provisioning commands are themselves run as/through -- and is not provisioned here).
            "path": "/etc/sudoers.d/ai-nopasswd",
            "content": "ai ALL=(ALL) NOPASSWD:ALL\n",
            "owner": "root:root",
            "mode": "440",
            "environments": ["prod"],
        },
        {
            # Lets `hams_ai_agent` (see this MANIFEST's "system_accounts" entry) run the
            # ticket-triage MCP server as `odoo`, restricted to this one exact wrapper script --
            # see the matching `run-ticket-triage-mcp.sh` static_files entry below for what it
            # actually runs.
            "path": "/etc/sudoers.d/hams_ai_agent-mcp-servers",
            "content": (
                "hams_ai_agent ALL=(odoo) NOPASSWD: "
                "/usr/local/sbin/run-ticket-triage-mcp.sh\n"
            ),
            "owner": "root:root",
            "mode": "440",
            "environments": ["prod"],
        },
        {
            # Sibling grant for `hams_event_agent`'s own event-enrichment MCP server -- see the
            # matching `run-event-enrichment-mcp.sh` static_files entry below. Deliberately a
            # separate sudoers.d file and a separate wrapper script from the ticket-triage grant
            # above (see the hams_event_agent "system_accounts" entry's own comment for why these
            # two accounts/tools must never share a grant).
            "path": "/etc/sudoers.d/hams_event_agent-mcp-servers",
            "content": (
                "hams_event_agent ALL=(odoo) NOPASSWD: "
                "/usr/local/sbin/run-event-enrichment-mcp.sh\n"
            ),
            "owner": "root:root",
            "mode": "440",
            "environments": ["prod"],
        },
        # Deliberately NOT tracking /etc/sudoers.d/odoo-agyimport-sandbox (confirmed live on
        # hams1, 2026-10-02: "odoo ALL=(agyimport) NOPASSWD: /usr/local/lib/agy-import/agy
        # --sandbox --output-format json") or the `agyimport` system account it grants to, even
        # though both still exist today -- night_shift_todo/low/agyimport-account-is-dead-
        # infrastructure-on-hams1-a7e4c891.md (filed 2026-10-01, before this pass) already
        # identified this exact account/sudoers-file/binary trio as dead infrastructure from a
        # superseded isolation plan (ham_repeater_import.py moved to Claude Code CLI/
        # hams_ai_agent instead; agyimport's own one-time interactive sign-in was never
        # completed), slated for removal on hams1, not for permanent tracking. Adding a
        # static_files entry here would make a fresh-host rebuild recreate dead infrastructure
        # forever and would need its own follow-up removal the moment a7e4c891 is actioned --
        # whoever closes that item needs to touch only hams1, not this MANIFEST too.
        {
            # Lets `odoo` run the repeater-import `agy` tool as `hams_ai_agent` itself (a second,
            # older route alongside the dedicated `agyimport` sandbox above) -- restricted to the
            # not-yet-tracked `run-hams-ai-agent.sh` wrapper, see its own static_files entry
            # below.
            "path": "/etc/sudoers.d/odoo-agy-sandbox",
            "content": (
                "odoo ALL=(hams_ai_agent) NOPASSWD: "
                "/usr/local/sbin/run-hams-ai-agent.sh\n"
            ),
            "owner": "root:root",
            "mode": "440",
            "environments": ["prod"],
        },
        {
            # Lets `odoo` run the real Claude Code CLI as `hams_ai_agent` -- restricted to the
            # already-tracked `run-hams-ai-agent-claude.sh` wrapper (see its own static_files
            # entry elsewhere in this MANIFEST).
            "path": "/etc/sudoers.d/odoo-ai-agent-claude-sandbox",
            "content": (
                "odoo ALL=(hams_ai_agent) NOPASSWD: "
                "/usr/local/sbin/run-hams-ai-agent-claude.sh\n"
            ),
            "owner": "root:root",
            "mode": "440",
            "environments": ["prod"],
        },
        {
            # Lets the club crawl's own account run the real Claude Code CLI as `hams_ai_agent`, the one
            # reach it has into another account -- the same single wrapper the odoo grant above names, and
            # nothing else. The CLI is started with --tools "" and --strict-mcp-config (see
            # daemons/club_registry_recrawl/repeater_recrawl.py's CLAUDE_COMMAND): no tool and no MCP
            # server, a bounded text completion over untrusted club pages. Prod-only, like the account.
            "path": "/etc/sudoers.d/club-crawl-claude-sandbox",
            "content": (
                "hamsd_club_crawl ALL=(hams_ai_agent) NOPASSWD: "
                "/usr/local/sbin/run-hams-ai-agent-claude.sh\n"
            ),
            "owner": "root:root",
            "mode": "440",
            "environments": ["prod"],
        },
        {
            # Sibling grant letting `odoo` run the real Claude Code CLI as `hams_event_agent` --
            # restricted to the already-tracked `run-hams-event-agent-claude.sh` wrapper (see its
            # own static_files entry elsewhere in this MANIFEST).
            "path": "/etc/sudoers.d/odoo-event-agent-claude-sandbox",
            "content": (
                "odoo ALL=(hams_event_agent) NOPASSWD: "
                "/usr/local/sbin/run-hams-event-agent-claude.sh\n"
            ),
            "owner": "root:root",
            "mode": "440",
            "environments": ["prod"],
        },
        {
            # Lets `odoo` run the event-enrichment tooling as `hams_event_agent` itself --
            # restricted to the `run-hams-event-agent.sh` wrapper, see its own static_files entry
            # below (a fourth remaining untracked wrapper script this todo's own "Done when" list
            # did not name, found by checking the sudoers grant's actual target directly on
            # hams1 rather than trusting that list's count -- the same three-vs-seven-vs-eight
            # undercount pattern as this MANIFEST's sudoers.d entries above).
            "path": "/etc/sudoers.d/odoo-event-agent-sandbox",
            "content": (
                "odoo ALL=(hams_event_agent) NOPASSWD: "
                "/usr/local/sbin/run-hams-event-agent.sh\n"
            ),
            "owner": "root:root",
            "mode": "440",
            "environments": ["prod"],
        },
        {
            # Bringing the four remaining untracked wrapper scripts the sudoers.d grants above
            # reference under tracked management -- the two Claude CLI wrappers
            # (`run-hams-ai-agent-claude.sh`, `run-hams-event-agent-claude.sh`) were already
            # tracked by a separate pass (see their own static_files entries elsewhere in this
            # MANIFEST); these four were not (this todo's own "Done when" section named three of
            # them; the fourth, `run-hams-event-agent.sh`, was found by checking the
            # `odoo-event-agent-sandbox` sudoers grant's actual target directly on hams1 -- see
            # that entry's own comment above). Content copied verbatim from the real,
            # already-working files on hams1 (read directly via `sudo cat`, confirmed clean -- no
            # secrets embedded, only a key *file path*, not a key value, in the two MCP wrapper
            # scripts below).
            "path": "/usr/local/sbin/run-hams-ai-agent.sh",
            "content": """\
#!/bin/bash
# Same fix, same reasoning, for hams_ai_agent (ticket-triage) -- see run-hams-event-agent.sh.
cd /home/hams_ai_agent || exit 1
exec /home/hams_ai_agent/.local/bin/agy "$@"
""",
            "owner": "root:root",
            "mode": "755",
            "environments": ["prod"],
        },
        {
            # Sibling wrapper for the ticket-triage MCP server, run as `odoo` via the
            # `hams_ai_agent-mcp-servers` sudoers.d grant above. A wrapper script, not the raw
            # command line in sudoers, because sudoers' own parser chokes on the URL colon in
            # ODOO_URL (same reasoning as run-event-enrichment-mcp.sh below).
            "path": "/usr/local/sbin/run-ticket-triage-mcp.sh",
            "content": """\
#!/bin/bash
# Exact env + exec for hams_ticket_triage_mcp, run as odoo via a narrow sudoers grant for
# hams_ai_agent. A wrapper script, not the raw command line in sudoers, because sudoers own
# parser chokes on the URL colon in ODOO_URL.
export PYTHONPATH=/opt/hams/daemons
export ODOO_URL=http://127.0.0.1:8069
export ODOO_DB=hams_prod
export ODOO_KEY_FILE=/opt/hams/etc/keys/ai_triage_service_internal.key
# The sudoers grant allows ANY arguments, so this wrapper is what restricts them: no argument is
# the MCP server (what the model's own session launches); --count-pending is the scheduled
# agent's cheap "is any ticket waiting" check (no model call). Anything else, notably
# --enable-propose-fix style switches, is refused. propose_fix is also closed in the server
# unless HAMS_TRIAGE_ENABLE_PROPOSE_FIX=1, which this wrapper never sets.
case "$1" in
    "") exec /usr/bin/python3 /opt/hams/daemons/hams_ticket_triage_mcp/main.py ;;
    --count-pending) exec /usr/bin/python3 /opt/hams/daemons/hams_ticket_triage_mcp/main.py --count-pending ;;
    *) echo "run-ticket-triage-mcp.sh: unsupported argument" >&2; exit 2 ;;
esac
""",
            "owner": "root:root",
            "mode": "755",
            "environments": ["prod"],
        },
        {
            # Sibling wrapper for the event-enrichment MCP server, run as `odoo` via the
            # `hams_event_agent-mcp-servers` sudoers.d grant above. Deliberately its own separate
            # MCP registration from ticket-triage (see the hams_event_agent "system_accounts"
            # entry's own comment) -- this one fetches untrusted third-party club-website content
            # via read_url and must never share write tools with unrelated data.
            "path": "/usr/local/sbin/run-event-enrichment-mcp.sh",
            "content": """\
#!/bin/bash
# Exact env + exec for hams_event_enrichment_mcp, run as odoo via a narrow sudoers grant for
# hams_event_agent (its own dedicated account, separate from hams_ai_agent/ticket-triage --
# see agents/skills/local-resources/SKILL.md and night_shift_todo/high/
# agy-shared-mcp-blanket-permission-crosses-repeater-import-and-ticket-triage-b6e91f2a.md for why
# event-enrichment, which fetches untrusted third-party club-website content via read_url, must
# never share an MCP registration with a feature that has real write tools on unrelated data).
export PYTHONPATH=/opt/hams/daemons
export ODOO_URL=http://127.0.0.1:8069
export ODOO_DB=hams_prod
export ODOO_KEY_FILE=/opt/hams/etc/keys/event_ai_correction_service_internal.key
exec /usr/bin/python3 /opt/hams/daemons/hams_event_enrichment_mcp/main.py
""",
            "owner": "root:root",
            "mode": "755",
            "environments": ["prod"],
        },
        {
            # Fourth remaining wrapper script -- see the "Bringing the four remaining untracked
            # wrapper scripts" comment above. Run as `hams_event_agent` via the
            # `odoo-event-agent-sandbox` sudoers.d grant above; sibling of
            # `run-hams-ai-agent.sh` (same fix, same reasoning, for the event-enrichment side's
            # own agy invocation).
            "path": "/usr/local/sbin/run-hams-event-agent.sh",
            "content": """\
#!/bin/bash
# Runs agy as hams_event_agent with its CWD set to its own home directory. Necessary because
# plain sudo -u does not change the working directory, and this account has no login shell
# (nologin, by design), so sudo -i cannot be used either -- a nologin shell makes -i refuse with
# "This account is currently not available." Found live 2026-09-29: without this, agy inherited
# whatever CWD the calling shell happened to have (often /home/ai, the sysadmin account it was
# invoked from during manual testing), computed that as its own workspace directory, and burned
# huge time/token cost on hundreds of failed permission-denied attempts to read
# /home/ai/AGENTS.md, /home/ai/GEMINI.md, and similar project-discovery files it could never
# actually reach -- the real root cause of every non-converging multi-hop test run tonight, not a
# prompt-wording or page-size issue as first suspected.
cd /home/hams_event_agent || exit 1
exec /home/hams_event_agent/.local/bin/agy "$@"
""",
            "owner": "root:root",
            "mode": "755",
            "environments": ["prod"],
        },
        {
            # hams1 readiness audit (docs/runbooks/PRODUCTION_READINESS_2026-10.md in hams_com), row 3: nothing
            # watched the site and nothing outside the host could page anyone. One pass per minute (timer below)
            # of tools/site_monitor.py: Odoo's health endpoint, PostgreSQL, the tunnel's edge connections, the
            # public site, every long-running daemon (signers included) and disk space. It pages through the
            # operator's webhook and/or SMTP (PAGER_WEBHOOK_URL, PAGER_FALLBACK_EMAIL, SMTP_HOST ... in the
            # optional /opt/hams/etc/site_monitor.env) with no Odoo and no mail server of ours in the path, and
            # pings HAMS_MONITOR_HEARTBEAT_URL only when everything passes, so an off-host dead-man service
            # pages when the host, its network or this timer dies. Local checks and the operator's own
            # webhook only, so not external_fetch. Runs as root with every capability but read-anywhere dropped:
            # `systemctl is-active` and the state file need none, and reading the root-only env file needs only that.
            "path": "/opt/hams/systemd/hams-site-monitor.service",
            "content": """\
[Unit]
Description=One pass of the hams.com site monitor (checks, page on failure, dead-man heartbeat)
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=-/opt/hams/etc/site_monitor.env
ExecStart=/usr/bin/python3 /opt/hams/src/hams_open/hams_shared/tools/site_monitor.py
# A hung check must not stack up passes: the longest legitimate pass is a few HTTP timeouts.
TimeoutStartSec=120
StateDirectory=hams-monitor
StateDirectoryMode=0700
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.site.monitor
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams-site-monitor.timer",
            "content": """\
[Unit]
Description=Run the hams.com site monitor every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # night_shift_todo/low/detect-stray-long-running-interactive-odoo-processes-
            # b3f8a672.md: a forgotten `odoo shell` process once ran on hams1 for 4+ days
            # undetected (night_shift_todo/medium/stray-odoo-shell-process-ran-4-days-on-
            # production-e81f4a92.md, closed), holding stale in-memory code and running its own
            # independent ir.cron scheduler the whole time. See daemons/
            # stray_odoo_shell_detector/main.py's own module docstring for the full reasoning.
            #
            # The FULL ADR-0070 standard hardening set applies unexceptionally here (unlike
            # credential_touch's own deliberate exception just above) -- this daemon only ever
            # runs `ps` to read /proc, never sudo's into another account, so it needs no setuid
            # capability and no ReadWritePaths carve-out.
            "path": "/opt/hams/systemd/stray.odoo.shell.detector.service",
            "content": """\
[Unit]
Description=Detect a Stray, Long-Running Interactive Odoo Shell Process (One-Shot)
After=network.target

[Service]
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/stray_odoo_shell_detector

ExecStart=/usr/bin/python3 /opt/hams/daemons/stray_odoo_shell_detector/main.py

StandardOutput=journal
StandardError=journal
SyslogIdentifier=stray.odoo.shell.detector
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/stray.odoo.shell.detector.timer",
            "content": """\
[Unit]
Description=Hourly Check For A Stray Interactive Odoo Shell Process

[Timer]
# Hourly matches this check's own STALE_THRESHOLD_SECONDS (one hour) -- no point checking much
# more often than the threshold itself, and hourly still catches a stray process reasonably
# promptly against the real incident this closes (one ran undetected for 4+ days).
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # hams_com daemons/auth_cert_renew: renews the auth.hams.com certificate on hams1 itself through
            # hams1's own PowerDNS (DNS-01), replacing the dev-box renewal (Bruce, 2026-10-05; to-do
            # night_shift_todo/medium/auth-hams-com-certificate-renewal-on-hams1-3e8d61b4.md). `_acme-challenge.
            # auth.hams.com` is a CNAME (entered through Odoo's Cloudflare panel) to a TXT name in the dedicated
            # zone acme.hams.com, delegated to ns1/ns2.hams.com, which this host already serves on port 53: no new
            # port, and no Cloudflare or other third-party credential on this host. certbot keeps its own state
            # under /var/lib/hams-acme (StateDirectory), not /etc/letsencrypt, so it cannot touch the unused
            # hams.com lineage and Debian's certbot.timer cannot drive this one. The deploy hook streams the
            # renewed pair to /usr/local/sbin/hams-install-auth-cert (hams_com nginx/prod/cert-deploy/), which
            # checks it and installs /etc/hams/auth/auth.crt and auth.key for hams_auth_gateway to reload.
            #
            # It talks to Let's Encrypt, hence external_fetch (never enabled or started on a test host). opt_in:
            # provisioning links it but never enables it; the coordinator enables it only after the delegation,
            # the CNAME and the first issuance exist (the ConditionPathExists lines also keep it inert until
            # then). Runs as root because it reads the root-only pdns.env and the installer chowns the key to
            # root:hams-auth; the sandbox leaves only its own state, its logs, /etc/hams/auth and a private
            # runtime directory writable, and only three capabilities (chown, read a root-only file, set mode).
            "path": "/opt/hams/systemd/hams-auth-cert-renew.service",
            "external_fetch": "talks to Let's Encrypt (the ACME directory) to renew the certificate",
            "opt_in": "needs the acme.hams.com delegation, the _acme-challenge CNAME and a first issuance; see hams_com night_shift_todo/medium/auth-hams-com-certificate-renewal-on-hams1-3e8d61b4.md",
            "content": """\
[Unit]
Description=Renew the auth.hams.com certificate through our own PowerDNS (DNS-01) and install it for hams_auth_gateway
After=network-online.target pdns.service
Wants=network-online.target
ConditionPathExists=/var/lib/hams-acme/config/renewal/auth-hams-com.conf
ConditionPathExists=/usr/local/sbin/hams-install-auth-cert

[Service]
Type=oneshot
User=root
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER
StateDirectory=hams-acme
StateDirectoryMode=0700
LogsDirectory=hams-acme
LogsDirectoryMode=0700
RuntimeDirectory=hams-auth-cert-renew
RuntimeDirectoryMode=0700
ReadWritePaths=/etc/hams/auth
Environment=HAMS_INSTALL_TMPDIR=/run/hams-auth-cert-renew
WorkingDirectory=/opt/hams/daemons/auth_cert_renew
TimeoutStartSec=15min

ExecStart=/usr/bin/python3 /opt/hams/daemons/auth_cert_renew/main.py renew

StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams-auth-cert-renew
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams-auth-cert-renew.timer",
            "external_fetch": "activates hams-auth-cert-renew.service, which talks to Let's Encrypt",
            "opt_in": "activates hams-auth-cert-renew.service; see that unit",
            "content": """\
[Unit]
Description=Twice-daily renewal check for the auth.hams.com certificate

[Timer]
# certbot renews only when 30 days or fewer remain, so most runs do nothing. Twice a day, so one missed
# run (the host was down) is retried the same day, well inside the 30-day window.
OnCalendar=*-*-* 04,16:23:00
Persistent=true
RandomizedDelaySec=30m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # hams_com daemons/hams_turn: coturn as turn.hams.com, relaying UDP between a signed-in member's browser (or a
            # relay) and a relay, on 443/udp and 443/tcp (turns:) of hams1's one public address. Text identical to hams_com
            # daemons/hams_turn/packaging/hams-turn.service (hams_com's daemons/hams_turn/test_turnserver_conf.py fails if the
            # two drift). It is OPT-IN and prod-only: provisioning links it and never enables or starts it, and the
            # smoketest never starts it; the install steps in hams_com docs/runbooks/TURN_GO_LIVE.md do, after the certificate,
            # the DNS record and the secret exist (the ConditionPathExists lines keep it inert until then). It fetches
            # nothing from a third party (it only relays what a credentialed client sends), so it is not external_fetch.
            # The package's own coturn.service is masked by the install step before the package is installed, and is not
            # used: it would run on coturn's default settings.
            "path": "/opt/hams/systemd/hams-turn.service",
            "opt_in": "needs the certificate, the shared secret and the DNS record, and opens a public port; started only by hams_com docs/runbooks/TURN_GO_LIVE.md",
            "content": """\
# systemd unit for coturn as turn.hams.com. Provisioned by hams_shared/tools/infrastructure.py's MANIFEST as
# /opt/hams/systemd/hams-turn.service; daemons/hams_turn/packaging/hams-turn.service in hams_com must stay identical
# (daemons/hams_turn/test_turnserver_conf.py). It is linked, never enabled, by provisioning: it is started only by the
# install steps in docs/runbooks/TURN_GO_LIVE.md (its `enable` step), after the certificate and the DNS record exist.
[Unit]
Description=hams.com members-only TURN server (turn.hams.com, 443/udp and 443/tcp)
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/hams/turn/turnserver.conf
ConditionPathExists=/etc/hams/turn/turn.crt

[Service]
Type=simple
ExecStart=/usr/bin/turnserver -c /etc/hams/turn/turnserver.conf --pidfile=
# A renewed certificate is picked up with SIGUSR2, without dropping live allocations (tested in the rehearsal).
ExecReload=/bin/kill -USR2 $MAINPID
Restart=always
RestartSec=5

# Unprivileged. Port 443 is bound with CAP_NET_BIND_SERVICE only.
User=hams-turn
Group=hams-turn
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Hardening: no new privileges, read-only system, private /tmp and devices, nothing writable at all.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadOnlyPaths=/etc/hams/turn
UMask=0077

# No limits of our own: coturn raises its own open-file limit to the system's, and its per-user and total
# quotas are left at coturn's defaults (Bruce, 2026-10-07: no caps).

StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams-turn

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            # hams_com daemons/auth_cert_renew, `--profile turn`: renews the turn.hams.com certificate on hams1 through its own
            # PowerDNS, exactly as hams-auth-cert-renew.service does for auth.hams.com (read that unit's comment): the
            # CNAME `_acme-challenge.turn.hams.com` (entered through Odoo's Cloudflare panel) points into acme.hams.com,
            # which this host serves on port 53. Its own lineage, key and installer (/usr/local/sbin/hams-install-turn-cert,
            # hams_com nginx/prod/cert-deploy/), which installs /etc/hams/turn/turn.crt and turn.key and reloads
            # hams-turn.service (the unit's ExecReload sends SIGUSR2, which makes coturn serve the new certificate with no restart). It talks to Let's Encrypt, hence
            # external_fetch; opt_in: the coordinator enables it after the first issuance.
            "path": "/opt/hams/systemd/hams-turn-cert-renew.service",
            "external_fetch": "talks to Let's Encrypt (the ACME directory) to renew the certificate",
            "opt_in": "needs the _acme-challenge.turn CNAME and a first issuance; see hams_com docs/runbooks/TURN_GO_LIVE.md",
            "content": """\
[Unit]
Description=Renew the turn.hams.com certificate through our own PowerDNS (DNS-01) and install it for coturn
After=network-online.target pdns.service
Wants=network-online.target
ConditionPathExists=/var/lib/hams-acme/config/renewal/turn-hams-com.conf
ConditionPathExists=/usr/local/sbin/hams-install-turn-cert

[Service]
Type=oneshot
User=root
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER
StateDirectory=hams-acme
StateDirectoryMode=0700
LogsDirectory=hams-acme
LogsDirectoryMode=0700
RuntimeDirectory=hams-turn-cert-renew
RuntimeDirectoryMode=0700
ReadWritePaths=/etc/hams/turn
Environment=HAMS_INSTALL_TMPDIR=/run/hams-turn-cert-renew
WorkingDirectory=/opt/hams/daemons/auth_cert_renew
TimeoutStartSec=15min

ExecStart=/usr/bin/python3 /opt/hams/daemons/auth_cert_renew/main.py renew --profile turn

StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams-turn-cert-renew
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/hams-turn-cert-renew.timer",
            "external_fetch": "activates hams-turn-cert-renew.service, which talks to Let's Encrypt",
            "opt_in": "activates hams-turn-cert-renew.service; see that unit",
            "content": """\
[Unit]
Description=Twice-daily renewal check for the turn.hams.com certificate

[Timer]
# certbot renews only when 30 days or fewer remain, so most runs do nothing. Twice a day, at other minutes than the
# auth.hams.com timer, so the two never hold certbot's lock together.
OnCalendar=*-*-* 04,16:41:00
Persistent=true
RandomizedDelaySec=30m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
    ],
    # Speech models for the simulated-band bots (daemons/hams_simulated_bots). Fetched by
    # provision_model_files() ONLY on a production-class provisioning run (never --test, never
    # HAMS_ISOLATED_NS=1, never a test environment), each file verified against `sha256` and `size`
    # before it is moved into place; nothing is downloaded when the installed copy already verifies.
    # Checksums and sizes were taken from the dev box's copies of these exact files (2026-10-04), not
    # from a download. The Hugging Face whisper URLs pin a commit (`resolve/<sha>/`); the Piper voice
    # repository is read at `main`, which is still safe because the content is checksum-pinned (a
    # changed upstream file fails verification and is not installed).
    "model_files": [
        {
            "path": "/opt/hams/models/piper/en_US-lessac-low.onnx",
            "url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low/en_US-lessac-low.onnx",
            "sha256": "f7d01dde371555732c4c314111ac79672b1a5ce2fc19266ab42178fd8df7f375",
            "size": 63201294,
            "owner": "hams_com:hams_com",
            "mode": "640",
        },
        {
            "path": "/opt/hams/models/piper/en_US-lessac-low.onnx.json",
            "url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low/en_US-lessac-low.onnx.json",
            "sha256": "45754dfdebb3b8661c3fc564713772deec6e064feeb5b4e9594857dc7305193a",
            "size": 4882,
            "owner": "hams_com:hams_com",
            "mode": "640",
        },
        {
            "path": "/opt/hams/models/faster-whisper-tiny.en/config.json",
            "url": "https://huggingface.co/Systran/faster-whisper-tiny.en/resolve/0d3d19a32d3338f10357c0889762bd8d64bbdeba/config.json",
            "sha256": "14b1b421a90349bc551b881461426b561a874049cb9e4c4864f2ca384f6a7cc5",
            "size": 2317,
            "owner": "hams_com:hams_com",
            "mode": "640",
        },
        {
            "path": "/opt/hams/models/faster-whisper-tiny.en/model.bin",
            "url": "https://huggingface.co/Systran/faster-whisper-tiny.en/resolve/0d3d19a32d3338f10357c0889762bd8d64bbdeba/model.bin",
            "sha256": "1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a",
            "size": 75537502,
            "owner": "hams_com:hams_com",
            "mode": "640",
        },
        {
            "path": "/opt/hams/models/faster-whisper-tiny.en/tokenizer.json",
            "url": "https://huggingface.co/Systran/faster-whisper-tiny.en/resolve/0d3d19a32d3338f10357c0889762bd8d64bbdeba/tokenizer.json",
            "sha256": "929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df",
            "size": 2128466,
            "owner": "hams_com:hams_com",
            "mode": "640",
        },
        {
            "path": "/opt/hams/models/faster-whisper-tiny.en/vocabulary.txt",
            "url": "https://huggingface.co/Systran/faster-whisper-tiny.en/resolve/0d3d19a32d3338f10357c0889762bd8d64bbdeba/vocabulary.txt",
            "sha256": "ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf",
            "size": 422309,
            "owner": "hams_com:hams_com",
            "mode": "640",
        },
    ],
    "apt_packages": [
        {"name": "odoo", "debian_name": "odoo", "environments": ["early_prod"]},
        {
            "name": "postgresql",
            "debian_name": "postgresql",
            "environments": ["early_prod"],
        },
        {
            "name": "postgresql-common",
            "debian_name": "postgresql-common",
            "environments": ["early_prod"],
        },
        {
            "name": "postgresql-client",
            "debian_name": "postgresql-client",
            "environments": ["early_prod"],
        },
        # Since 2026-10-05 nginx runs on production as a loopback-only buffering proxy
        # (127.0.0.1:8085) between the Cloudflare Tunnel and Odoo: see the
        # /etc/nginx/hams/hams-tunnel-origin.conf static_files entry. It is still not a
        # public listener (no port 80 or 443). The dev box also uses it for its local
        # 127.0.0.1:8080 bus proxy (hams_com CLAUDE.md).
        {"name": "nginx", "debian_name": "nginx", "environments": ["early_prod"]},
        # setfacl: the traversal grant on /opt/hams, /opt/hams/etc, ... that lets a daemon family's account
        # open its own paths without being in hams_com (MANIFEST directories "acl").
        {"name": "acl", "debian_name": "acl", "environments": ["early_prod"]},
        # hook_build_rust_daemons() runs `cargo build`; without this a fresh box (no rustup)
        # reports PROVISIONING DEGRADED and the three Rust daemons are never built.
        # Debian 13's plain `cargo` is 1.85, too old for the daemons' dependencies (icu_* crates need
        # 1.86); Debian's security-maintained cargo-web/rustc-web (1.96) replace it and provide
        # /usr/bin/cargo, so the hook's plain `cargo` command works.
        {"name": "cargo", "debian_name": "cargo-web", "environments": ["early_prod"]},
        # hook_build_cloudflared_ffi() compiles daemons/cloudflared-ffi (stdlib-only Go, cgo) into
        # libcloudflared.so, which the cloudflare module's tunnel-daemon tests load. Debian 13 and
        # Ubuntu 24.04 both ship golang-1.24-go (Ubuntu's plain golang-go is 1.22, too old for the
        # module's go.mod), so no tarball or toolchain download is needed; gcc comes from
        # build-essential below.
        {"name": "golang-1.24-go", "debian_name": "golang-1.24-go", "environments": ["early_prod"]},
        {
            "name": "redis-server",
            "debian_name": "redis-server",
            "environments": ["early_prod"],
        },
        {
            "name": "rabbitmq-server",
            "debian_name": "rabbitmq-server",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-redis",
            "debian_name": "python3-redis",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pika",
            "debian_name": "python3-pika",
            "environments": ["early_prod"],
        },
        {
            # Real, previously-masked missing dependency found 2026-09-13
            # hardware-qualifying pi500-1 (a genuinely fresh Raspberry Pi 500):
            # ham_onboarding/__manifest__.py declares a real external Python
            # dependency on `fitz` (PyMuPDF's import name), but this MANIFEST
            # never installed the apt package that actually provides it --
            # `odoo -i ham_onboarding` failed outright on a fresh box with
            # "Unable to install module... external dependency is not met:
            # fitz". Never caught on the dev box because it already had
            # python3-pymupdf/python3-fitz installed incidentally from
            # unrelated prior work -- the same masking pattern this same
            # qualification pass already found for python3-pypdf and
            # python3-lxml-html-clean. python3-fitz (present on both Debian
            # 12 bookworm, confirmed via apt-cache policy on pi500-1, and
            # Debian 13 Trixie) provides the exact `fitz` import name the
            # manifest checks for.
            "name": "python3-fitz",
            "debian_name": "python3-fitz",
            "environments": ["early_prod"],
        },
        {"name": "sqlite3", "debian_name": "sqlite3", "environments": ["early_prod"]},
        {
            "name": "pdns-server",
            "debian_name": "pdns-server",
            "environments": ["early_prod"],
        },
        {
            "name": "pdns-backend-sqlite3",
            "debian_name": "pdns-backend-sqlite3",
            "environments": ["early_prod"],
        },
        {
            "name": "pgbackrest",
            "debian_name": "pgbackrest",
            "environments": ["early_prod"],
        },
        {"name": "certbot", "debian_name": "certbot", "environments": ["early_prod"]},
        # Unused in production (nginx disabled; no port 80 open on hams1).
        {
            "name": "python3-certbot-nginx",
            "debian_name": "python3-certbot-nginx",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-passlib",
            "debian_name": "python3-passlib",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-cryptography",
            "debian_name": "python3-cryptography",
            "environments": ["early_prod"],
        },
        {
            "name": "build-essential",
            "debian_name": "build-essential",
            "environments": ["early_prod"],
        },
        {
            "name": "libpq-dev",
            "debian_name": "libpq-dev",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-dev",
            "debian_name": "python3-dev",
            "environments": ["early_prod"],
        },
        {
            "name": "bind9-dnsutils",
            "debian_name": "bind9-dnsutils",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-stdeb",
            "debian_name": "python3-stdeb",
            "environments": ["early_prod"],
        },
        {"name": "fakeroot", "debian_name": "fakeroot", "environments": ["early_prod"]},
        {
            "name": "python3-all",
            "debian_name": "python3-all",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pypdf2",
            "debian_name": "python3-pypdf",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-setuptools",
            "debian_name": "python3-setuptools",
            "environments": ["early_prod"],
        },
        {
            "name": "dh-python",
            "debian_name": "dh-python",
            "environments": ["early_prod"],
        },
        {"name": "jing", "debian_name": "jing", "environments": ["early_prod"]},
        {"name": "dbus-x11", "debian_name": "dbus-x11", "environments": ["early_prod"]},
        {
            "name": "python3-asyncpg",
            "debian_name": "python3-asyncpg",
            "environments": ["early_prod"],
        },
        # distributed_redis_cache's manifest declares python-dotenv as an external dependency,
        # and Odoo refuses to install that module without it. The dev box had it incidentally, so
        # the first real release provision (2026-09-21) was the first to hit the gap.
        {
            "name": "python3-dotenv",
            "debian_name": "python3-dotenv",
            "environments": ["early_prod"],
        },
        {"name": "black", "debian_name": "black", "environments": ["early_prod"]},
        {
            "name": "python3-psutil",
            "debian_name": "python3-psutil",
            "environments": ["early_prod"],
        },
        # wa7bnm_contest_sync.py's own module-level `import feedparser` is a
        # real runtime dependency, not a test-only one -- confirmed live on
        # hams1, 2026-09-22: wa7bnm.contest.sync.service crashed with
        # ModuleNotFoundError on every run, because this package had only
        # ever been installed under `if is_test:` further down in this same
        # file (grouped there with flask/flask-cors/aiohttp for OTHER
        # daemons' own test suites -- a real miscategorization for this one,
        # since this daemon needs it to run at all, in prod, not just to
        # test itself). Installed here instead so prod actually gets it; the
        # is_test-gated install becomes a harmless duplicate for test boxes
        # rather than the only place either environment could get it.
        {
            "name": "python3-feedparser",
            "debian_name": "python3-feedparser",
            "environments": ["early_prod"],
        },
        # The same miscategorization this file's own comment above already flags for
        # feedparser, left unfixed here until now: daemons/adif_ingress/main.py and
        # daemons/gdpr_csv_export/main.py both do a real, unconditional module-level
        # `import aiohttp` / `from aiohttp import web` -- a genuine prod runtime
        # dependency for two real daemons, not a test-only one -- yet this package was
        # ONLY ever installed under the `if is_test:` branch further down in this same
        # file. Root-caused 2026-10-02 against night_shift_todo's
        # adif-ingress-intermittent-aiohttp-modulenotfound-crash-loop report: with no
        # `early_prod` entry, a (re)provisioning run never guarantees this package is
        # actually present on a prod box before `adif.ingress.service`
        # (WantedBy=multi-user.target, Restart=always) first tries to start -- whether
        # it happens to be there at that moment depends entirely on uncontrolled
        # outside state (a stale manual install, dpkg's current state, an
        # unattended-upgrades operation mid-flight), explaining the observed
        # intermittent `ModuleNotFoundError: No module named 'aiohttp'` crash-loop on
        # boot that then "resolves itself" once something else outside this project's
        # own provisioning happens to leave the package in place. Installed here
        # instead so prod actually gets it guaranteed, same fix shape as feedparser
        # above; the is_test-gated install becomes a harmless duplicate for test boxes.
        {
            "name": "python3-aiohttp",
            "debian_name": "python3-aiohttp",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-ephem",
            "debian_name": "python3-ephem",
            "environments": ["early_prod"],
        },
        # daemons/ses_inbound_mail_ingest/main.py shells out to the `aws` CLI
        # (its own docstring explains why: no other daemon here depends on
        # boto3). Real gap found live on hams1, 2026-09-22:
        # ses.inbound.mail.ingest.service had been failing with zero journal
        # output for its own unit name, traced to a plain, unhandled
        # `FileNotFoundError: [Errno 2] No such file or directory: 'aws'`
        # crashing the whole process before any logging happened -- the `aws`
        # binary was never installed on this box at all, a more basic gap
        # than the credentials-not-provisioned one already tracked in this
        # daemon's own systemd unit comment.
        {
            "name": "awscli",
            "debian_name": "awscli",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-ldap3",
            "debian_name": "python3-ldap3",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-lxml",
            "debian_name": "python3-lxml",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-ntplib",
            "debian_name": "python3-ntplib",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pyinotify",
            "debian_name": "python3-pyinotify",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pymysql",
            "debian_name": "python3-pymysql",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-docx",
            "debian_name": "python3-docx",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-yaml",
            "debian_name": "python3-yaml",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-requests",
            "debian_name": "python3-requests",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-websocket",
            "debian_name": "python3-websocket",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-websockets",
            "debian_name": "python3-websockets",
            "environments": ["early_prod"],
        },
        {"name": "flake8", "debian_name": "flake8", "environments": ["early_prod"]},
        {
            "name": "python3-pip",
            "debian_name": "python3-pip",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pandas",
            "debian_name": "python3-pandas",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-numpy",
            "debian_name": "python3-numpy",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-markdown",
            "debian_name": "python3-markdown",
            "environments": ["early_prod"],
        },
        # daemons/gdpr_csv_export/main.py imports zipstream and calls
        # zipstream.ZipStream(...) -- that name is only ever provided by the
        # "zipstream-ng" distribution. Debian's OTHER, unrelated "zipstream"
        # package (python3-zipstream, an old "python-zipstream" by a
        # different author) installs into the exact same top-level import
        # path with a completely different, older API (zipstream.ZipFile,
        # no ZipStream at all) -- real bug found live on hams1, 2026-09-23:
        # whichever prior provisioning step installed python3-zipstream by
        # hand (never through this manifest -- neither package was ever
        # listed here) shadowed the real dependency, so every GDPR zip
        # export request crashed with AttributeError, 100% of the time,
        # with zero test coverage catching it (see
        # daemons/gdpr_csv_export/test_main.py's new
        # TestConsumeAndExportCallShape for the sibling bug found in the
        # same request path). python3-zipstream must never be installed
        # alongside this one.
        {
            "name": "python3-zipstream-ng",
            "debian_name": "python3-zipstream-ng",
            "environments": ["early_prod"],
        },
    ],
    "env_defaults": {
        "DB_PORT": "5432",
        "RMQ_PORT": "5672",
        "REDIS_PORT": "6379",
        "DX_FIREHOSE_WS_PORT": "8765",
        # No RMQ_USER/RMQ_PASS here. provision_environment() applies these defaults with setdefault()
        # BEFORE load_and_prompt_env(), so a "guest"/"guest" entry here (present until 2026-10-03)
        # pre-empted load_and_prompt_env's own production defaults: RMQ_USER never became
        # "hams_rabbitmq" and RMQ_PASS was never generated, and _create_rabbitmq_user_if_missing then
        # refused "guest" (recorded only as a "rabbitmq_bindings" hook failure). The guest default now
        # lives only where a broker that knows no other account is in use: load_and_prompt_env's test
        # branch and scaffold_test_environment().
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/hams/cache/ms-playwright",
    },
    # Host firewall (ufw) rules provisioned on production. Only ports a hams.com service must
    # answer from the internet belong here; everything else stays denied (ufw's default).
    # `args` is passed to `ufw` as is. Applied by provision_firewall_rules(), only when ufw is
    # installed and active, only when the rule is not already present, never on a test host.
    "firewall_rules": [
        {
            # stun.hams.com: the relay bridge's STUN responder (hams_relay_bridge stun_responder.rs,
            # BRIDGE_STUN_BIND in bridge.env). UDP only; the responder answers nothing but a
            # well-formed Binding request, rate limited, with a reply of at most 44 bytes.
            "args": ["allow", "3478/udp"],
            "comment": "stun.hams.com (hams_relay_bridge STUN responder)",
            "environments": ["prod"],
        },
    ],
    "systemd_odoo_override": {
        "Unit": {"Requires": "hams-pycache.service", "After": "hams-pycache.service"},
        "Service": {
            "EnvironmentFile": [
                "-/opt/hams/etc/odoo.env",
                "-/opt/hams/etc/core.env",
                "-/opt/hams/etc/db.env",
                "-/opt/hams/etc/redis.env",
                "-/opt/hams/etc/rabbitmq.env",
                "-/opt/hams/etc/smtp.env",
                "-/opt/hams/etc/pdns.env",
            ],
            "Environment": [
                "PYTHONPYCACHEPREFIX=/opt/hams/pycache",
                "ODOO_RC=/etc/odoo/odoo.conf",
            ],
            "ExecStartPre": "+/usr/bin/python3 /opt/hams/hams_shared/tools/env_validator.py",
            "ProtectSystem": "strict",
            "ReadWritePaths": [
                "/opt/hams/etc/keys",
                "/var/lib/odoo",
                "/var/log/odoo",
                # Found live on hams1, 2026-09-23: ADIF log uploads (ham_logbook's
                # web_adif_upload(), a real member-facing feature, not a daemon)
                # spool directly to /opt/hams/spool/adif_uploads from inside the
                # main odoo.service process itself, but that path was never added
                # here -- every one of this project's OTHER systemd units already
                # grants itself "/opt/hams/spool /opt/hams/downloads", but odoo.service
                # is the one process that actually needs to write an ADIF upload
                # there and was the one unit missing it. Confirmed live: every
                # upload attempt failed with "Read-only file system" until this was
                # added.
                "/opt/hams/spool",
            ],
            "PrivateTmp": "true",
            "PrivateDevices": "true",
            "NoNewPrivileges": "true",
            "KillSignal": "SIGINT",
            "TimeoutStopSec": "15",
            # Real incident, 2026-10-01: odoo.service had no Restart= at all
            # (systemd's own default, "no"), so a transient PostgreSQL
            # restart -- even a brief, intentional one -- fatally crashed
            # odoo.service outright rather than self-healing. Root cause:
            # odoo/service/server.py's process_spawn() -> check_registries()
            # opens a fresh cursor as part of its own pre-fork health check
            # and does not retry on a connection failure; if that check
            # happens to run during the exact window PostgreSQL is mid-
            # restart, the resulting psycopg2.OperationalError propagates
            # all the way up and the whole server process exits (status
            # 255), which systemd then just leaves "failed" rather than
            # restarting. Confirmed live: this happened twice in one
            # session restarting postgresql@18-main.service to pick up a
            # new EnvironmentFile, each one a real site outage until
            # manually `systemctl start odoo`'d back. `on-failure` makes
            # this self-heal; 5s gives PostgreSQL a moment to finish coming
            # back up before the retry.
            "Restart": "on-failure",
            "RestartSec": "5",
        },
    },
    # Bug found live on hams1, 2026-09-22: pdns.service ships as a plain OS
    # package unit with no override, so it always ran with the package's own
    # default /etc/powerdns/pdns.conf (an unconfigured stock template --
    # `launch=` blank, no real backend, no API) instead of this project's
    # real gsqlite3-backed config below. pdns_sync's busy-loop (Connection
    # refused against the API, forever) traced back to this: the API was
    # never enabled because pdns.service was never told to load the config
    # that enables it. ExecStart as a two-item list clears the packaged
    # unit's own ExecStart= (systemd's documented idiom for overriding
    # rather than appending) before setting the real one.
    "systemd_pdns_override": {
        "Service": {
            "ExecStart": [
                "",
                "/usr/sbin/pdns_server --guardian=no --daemon=no --disable-syslog "
                "--log-timestamp=no --write-pid=no --config-dir=/opt/hams/etc "
                "--config-name=gsqlite3",
            ],
        },
    },
}


def scaffold_test_environment(args_db, provision_dirs=True):
    for k, v in MANIFEST["env_defaults"].items():
        os.environ.setdefault(k, v)
    # The test pipeline starts its own private rabbitmq-server, whose only account is the factory
    # "guest" one (test.py's run_cmd sets the same default for the Odoo process).
    os.environ.setdefault("RMQ_USER", "guest")
    os.environ.setdefault("RMQ_PASS", "guest")

    os.environ.setdefault("DB_NAME", args_db)
    os.environ.setdefault("ODOO_DB", args_db)
    os.environ.setdefault("DB_USER", "odoo")
    os.environ.setdefault("DB_PASS", "odoo")
    os.environ.setdefault("DB_HOST", "postgres")
    os.environ.setdefault("ODOO_URL", "http://odoo:8069")
    os.environ.setdefault(
        "PDNS_API_URL", "http://powerdns:8081/api/v1/servers/localhost/zones"
    )
    os.environ.setdefault("PDNS_API_KEY", "secret")

    if provision_dirs:
        try:
            apply_production_directories(environment="test")
        except PermissionError as e:
            print(f"[*] PermissionError provisioning test directories: {e}")
            print("[*] Note: 'sudo' fallback removed per strict DevSecOps mandates.")
            raise


def get_mount_paths(environment, mount_type):
    return [
        d["path"]
        for d in MANIFEST["directories"]
        if environment in d["environments"] and d.get("runtime_mount") == mount_type
        and _in_host_class(d)
    ]


def provision_system_accounts(run_cmd_func, environment="prod", dest_dir="", only_host_class=None):
    for acc in MANIFEST.get("system_accounts", []):
        if environment not in acc.get("environments", ["prod", "test"]):
            continue
        if not _spec_selected(acc, only_host_class):
            continue

        user = acc["user"]
        group = acc["group"]
        home = acc.get("home", "/opt/hams")
        shell = acc.get("shell", "/bin/bash")
        add_to_users = acc.get("add_to_users", [])
        member_of = acc.get("member_of", [])

        try:
            grp.getgrnam(group)
        except KeyError:  # burn-ignore-os-account-probe
            run_cmd_func(["groupadd", "--system", group])

        try:
            pwd.getpwnam(user)
        except KeyError:  # burn-ignore-os-account-probe
            run_cmd_func(
                ["useradd", "--system", "-g", group, "-d", home, "-s", shell, user]
            )

        # `member_of`: existing groups this account itself joins (a group that does not exist makes
        # usermod fail, which stops provisioning: an account that cannot reach /opt/hams must not be
        # created half-configured and left to fail at run time, as pdns did on 2026-09-22).
        for existing_group in member_of:
            run_cmd_func(["usermod", "-a", "-G", existing_group, user])

        # `not_member_of`: groups the account must NOT be in. A migrated daemon family leaves hams_com
        # (its membership gave read access to the ADIF queue and every other hams_com-readable
        # directory; the traversal grant on the directories it really needs replaces it). Removal runs
        # only when the account is a member, so a clean host sees no command, and it is the one place
        # provisioning takes a membership away.
        for left_group in acc.get("not_member_of", []):
            try:
                if user in grp.getgrnam(left_group).gr_mem:
                    run_cmd_func(["gpasswd", "-d", user, left_group])
            except KeyError:  # burn-ignore-os-account-probe
                _logger.debug("Group %s not found, nothing to leave.", left_group)

        for extra_user in add_to_users:
            try:
                pwd.getpwnam(extra_user)
                run_cmd_func(["usermod", "-a", "-G", group, extra_user])
            except KeyError:  # burn-ignore-os-account-probe
                _logger.debug("User %s not found, skipping group addition.", extra_user)


def execute_hooks(environment, run_cmd_func, env_vars=None, dest_dir=""):
    if dest_dir and dest_dir.endswith("/"):
        dest_dir = dest_dir[:-1]

    for d in MANIFEST["directories"]:
        if environment in d["environments"] and "post_provision_hooks" in d and _in_host_class(d):
            for hook in d["post_provision_hooks"]:
                physical_path = (
                    os.path.join(dest_dir, d["path"].lstrip("/"))
                    if dest_dir
                    else d["path"]
                )
                if _plan("hook", f"{hook.__name__} on {physical_path}"):
                    continue
                hook(env_vars or {}, dest_dir, physical_path, run_cmd_func)


# [@ANCHOR: infrastructure:apply_production_directories]
def apply_production_directories(run_cmd_func=None, environment="prod", dest_dir="", only_host_class=None):
    """Creates every MANIFEST directory for environment and sets its owner and mode. An entry
    marked "preserve_existing" is only created when missing: an existing one keeps its owner and
    mode (e.g. /var/log/redis, which redis-server's package owns)."""
    for d in MANIFEST["directories"]:
        if environment in d["environments"] and _spec_selected(d, only_host_class):
            path = (
                os.path.join(dest_dir, d["path"].lstrip("/")) if dest_dir else d["path"]
            )
            mode = int(d["provision_mode"], 8)
            exists = os.path.isdir(path)
            if exists and d.get("preserve_existing"):
                continue
            if _planning():
                if not exists:
                    _plan("mkdir", f"{path} ({d.get('owner')}, {d['provision_mode']})")
                else:
                    _plan_ownership(path, d.get("owner"), mode)
                    if d.get("recursive_owner"):
                        _plan_recursive_ownership(path, d.get("owner"))
                continue
            os.makedirs(path, mode=mode, exist_ok=True)
            apply_permissions(
                path, d.get("owner"), mode, recursive=bool(d.get("recursive_owner"))
            )
            _apply_directory_acl(d, path, environment, run_cmd_func)


# [@ANCHOR: infrastructure:directory_acl]
def _apply_directory_acl(spec, path, environment, run_cmd_func):
    """Applies the entry's "acl" list (setfacl -m specs, such as "g:hams_traverse:--x") to `path`.
    Production only: the daemon accounts that need the grant exist on every environment, but the
    sandboxes of a test host never run them. `setfacl -m` is idempotent. The traversal group is
    the replacement for hams_com membership (DAEMON_OS_ISOLATION_PLAN.md, Phase 2): execute
    permission on a directory lets an account open a path it already knows and never list it."""
    entries = spec.get("acl")
    if not entries or environment != "prod":
        return
    for entry in entries:
        if _plan("setfacl", f"{path}: {entry}"):
            continue
        if not shutil.which("setfacl"):
            raise RuntimeError(
                "setfacl is not installed (apt package acl); the traversal grant on "
                f"{path} cannot be applied. Install it: apt-get install acl"
            )
        command = ["setfacl", "-m", entry, path]
        if run_cmd_func is not None:
            run_cmd_func(command)
        else:
            subprocess.run(command, check=True)


def _plan_recursive_ownership(path, owner_str):
    """Records how many entries below `path` a recursive chown to owner_str would change."""
    try:
        user, group = owner_str.split(":")
        want = (pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid)
    except (KeyError, ValueError, AttributeError):  # burn-ignore-os-account-probe
        _plan("chown -R", f"{path}: contents -> {owner_str} (account not created yet)")
        return
    differing = 0
    for current, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            try:
                info = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            if (info.st_uid, info.st_gid) != want:
                differing += 1
    if differing:
        _plan("chown -R", f"{path}: {differing} entries -> {owner_str}")


def _plan_ownership(path, owner_str, mode_int):
    """Records a chown/chmod of an existing path only when it would actually change something."""
    try:
        st = os.stat(path)
    except OSError:
        return
    changes = []
    if owner_str:
        try:
            user, group = owner_str.split(":")
            if (st.st_uid, st.st_gid) != (pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid):
                changes.append(f"owner -> {owner_str}")
        except (KeyError, ValueError):  # burn-ignore-os-account-probe
            changes.append(f"owner -> {owner_str} (account not created yet)")
    if mode_int is not None and (st.st_mode & 0o7777) != mode_int:
        changes.append(f"mode {st.st_mode & 0o7777:o} -> {mode_int:o}")
    if changes:
        _plan("chmod", f"{path}: {', '.join(changes)}")


# [@ANCHOR: infrastructure:write_env_files]
def write_env_files(base_etc_dir, env_vars, run_cmd_func, dest_dir=""):
    if dest_dir:
        base_etc_dir = os.path.join(dest_dir, base_etc_dir.lstrip("/"))
    if not _planning():
        os.makedirs(base_etc_dir, exist_ok=True)

    for filename, keys in MANIFEST["env_groups"].items():
        filepath = os.path.join(base_etc_dir, filename)
        content = "".join(f"{k}={env_vars[k]}\n" for k in keys if k in env_vars)
        if _planning():
            state = _file_state(filepath, content)
            if state != "unchanged":
                present = [k for k in keys if k in env_vars]
                _plan("write", f"{filepath} ({state}; keys: {' '.join(present) or 'none'})")
            continue

        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(filepath, flags, 0o400)
        # os.open()'s own mode argument is silently ignored by the kernel
        # when filepath already exists (mode only applies to a brand-new
        # file) -- so if this file previously existed with looser
        # permissions (a legacy file, one created by hand, or one left over
        # from before this hardening existed), the O_TRUNC above would
        # start writing real secret content (DB_PASS, ODOO_ADMIN_PASSWORD,
        # CLOUDFLARE_API_TOKEN, etc.) into a file still readable by whoever
        # the old permissions allowed, for the whole duration of the write,
        # until the apply_permissions() call below finally tightens it.
        # fchmod here closes that window unconditionally, regardless of
        # whatever permissions the file had a moment ago.
        os.fchmod(fd, 0o400)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)

        apply_permissions(filepath, "root:root", 0o400)


def provision_custom_addons(run_cmd_func, env_vars, environment="prod", dest_dir=""):
    if environment not in ["prod", "test"]:
        return

    if not env_vars.get("REPO_ROOT"):
        return

    custom_addons_dir = (
        os.path.join(dest_dir, "opt/hams/odoo") if dest_dir else "/opt/hams/odoo"
    )

    if os.path.isdir(env_vars["REPO_ROOT"]):
        for item in os.listdir(env_vars["REPO_ROOT"]):
            item_path = os.path.join(env_vars["REPO_ROOT"], item)
            if os.path.isdir(item_path) and os.path.exists(
                os.path.join(item_path, "__manifest__.py")
            ):
                target = os.path.join(custom_addons_dir, item)
                shutil.rmtree(target, ignore_errors=True)
                os.makedirs(target, exist_ok=True)
                shutil.copytree(
                    item_path,
                    target,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("target", ".git", "__pycache__"),
                )

    apply_permissions(custom_addons_dir, "odoo:odoo", None)


# Written by hams_com's devbox_tools/deploy_to_production.py on every applied deployment. Its
# presence marks a production host whose /opt/hams/daemons is kept current by
# `deploy_to_production.py --sync-daemons`, not by provisioning.
DEPLOY_LOG_PATH = "/opt/hams/src/DEPLOY_LOG"


# [@ANCHOR: infrastructure:_skip_deployed_copy]
def _skip_deployed_copy(file_spec, path):
    """True when a `deployed_by_sync_daemons` src copy must not run: the target already exists and
    the host is updated by deploy_to_production.py (DEPLOY_LOG_PATH exists). On hams1 the src trees
    under /opt/hams/src are not what --sync-daemons updates, so copying them over /opt/hams/daemons
    reverted 79 newer daemon files (round-2 production runbook, 2026-10-03). A fresh install, with
    no DEPLOY_LOG yet, still copies."""
    return bool(
        file_spec.get("deployed_by_sync_daemons")
        and os.path.exists(DEPLOY_LOG_PATH)
        and os.path.exists(path)
    )


# [@ANCHOR: infrastructure:provision_static_files]
def provision_static_files(run_cmd_func, env_vars, environment="prod", dest_dir="", only_host_class=None):
    for file_spec in MANIFEST.get("static_files", []):
        if environment not in file_spec["environments"]:
            continue
        if not _spec_selected(file_spec, only_host_class):
            continue

        condition_env = file_spec.get("condition_env")
        if condition_env and not env_vars.get(condition_env):
            continue

        path = format_env(file_spec["path"], env_vars)
        if dest_dir:
            path = os.path.join(dest_dir, path.lstrip("/"))

        mode = int(file_spec.get("mode", "644"), 8)

        src = file_spec.get("src")
        url = file_spec.get("url")
        hooks = file_spec.get("post_provision_hooks", [])

        if src and _skip_deployed_copy(file_spec, path):
            _logger.warning(
                "[*] NOT copying %s over %s: this host is deployed by deploy_to_production.py (%s "
                "exists). Update it with `python3 devbox_tools/deploy_to_production.py --sync-daemons "
                "--apply` from the dev box instead.",
                format_env(src, env_vars), path, DEPLOY_LOG_PATH,
            )
            _plan("skip", f"copy {format_env(src, env_vars)} -> {path} (deployed host: use --sync-daemons)")
            continue

        if _planning():
            if src:
                src = format_env(src, env_vars)
                if os.path.exists(src):
                    _plan("copy", f"{src} -> {path} (owner {file_spec.get('owner')}, mode {mode:o})")
                else:
                    _plan("skip", f"copy {src} -> {path} (source missing)")
            elif url:
                _plan("download", f"{url} -> {path}")
            else:
                if "{DEB_CODENAME}" in file_spec.get("content", "") and "DEB_CODENAME" not in env_vars:
                    env_vars["DEB_CODENAME"] = get_os_codename()
                _ensure_local_hostname(file_spec.get("content", ""), env_vars)
                content = format_env(file_spec.get("content", ""), env_vars)
                state = _file_state(path, content)
                if state != "unchanged":
                    _plan("write", f"{path} ({state}, owner {file_spec.get('owner')}, mode {mode:o})")
            for hook in hooks:
                _plan("hook", f"{hook.__name__} on {path}")
            continue

        os.makedirs(os.path.dirname(path), exist_ok=True)

        if src:
            src = format_env(src, env_vars)
            if os.path.exists(src):
                if os.path.isdir(src):
                    # A dangling symlink in the source (hams_com's
                    # daemons/hams_local_relay/ham_digital_modes is an absolute link to a dev-box
                    # path) made copytree raise shutil.Error after copying everything else, and
                    # provision_environment() only catches CalledProcessError, so the whole run
                    # ended in a traceback. Dangling links are now skipped, and any other copy
                    # error is recorded as a degraded step instead of aborting provisioning.
                    try:
                        shutil.copytree(
                            src,
                            path,
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("target", ".git", "__pycache__"),
                            ignore_dangling_symlinks=True,
                        )
                    except shutil.Error as e:
                        _logger.warning("Copy of %s to %s was incomplete: %s", src, path, e)
                        record_hook_failure(f"copy:{path}", e)
                else:
                    shutil.copy2(src, path)
        elif url:
            if "{DEB_TARGET_ARCH_CPU}" in url and "DEB_TARGET_ARCH_CPU" not in env_vars:
                res = subprocess.run(
                    ["dpkg-architecture", "-q", "DEB_TARGET_ARCH_CPU"],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    env_vars["DEB_TARGET_ARCH_CPU"] = res.stdout.strip()
            if "{KOPIA_ARCH}" in url and "KOPIA_ARCH" not in env_vars:
                env_vars["KOPIA_ARCH"] = _kopia_release_arch(env_vars)
            download_file(format_env(url, env_vars), path, mode, env_vars)
        else:
            if (
                "{DEB_CODENAME}" in file_spec.get("content", "")
                and "DEB_CODENAME" not in env_vars
            ):
                env_vars["DEB_CODENAME"] = get_os_codename()
            _ensure_local_hostname(file_spec.get("content", ""), env_vars)
            content = format_env(file_spec.get("content", ""), env_vars)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(path, flags, mode)
            # See write_env_files' own comment: os.open()'s mode argument is
            # ignored when the file already exists, so a pre-existing file
            # with looser permissions than `mode` would be written to at its
            # old, looser permissions until apply_permissions() below runs.
            os.fchmod(fd, mode)
            with open(fd, "w", encoding="utf-8") as f:
                f.write(content)

        apply_permissions(path, file_spec.get("owner"), mode)

        for hook in hooks:
            hook(env_vars or {}, dest_dir, path, run_cmd_func)


# [@ANCHOR: infrastructure:provision_model_files]
def _sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:  # audit-ignore-path
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_file_verifies(path, spec):
    """True when `path` exists with exactly the pinned size and SHA-256."""
    try:
        return os.path.getsize(path) == spec["size"] and _sha256_of(path) == spec["sha256"]
    except OSError:
        return False


def _fetch_model_to(url, part_path, expected_size):
    """Streams `url` to `part_path`, refusing to read past `expected_size` bytes. Raises on any failure."""
    ua = "HamsComProvisioning/1.0 (+https://hams.com)"
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    got = 0
    with urllib.request.urlopen(req, timeout=60) as response, open(part_path, "wb") as out:  # audit-ignore-path
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            got += len(chunk)
            if got > expected_size:
                raise ValueError(f"{url}: more than the expected {expected_size} bytes")
            out.write(chunk)


def provision_model_files(environment="prod", dest_dir="", fetch=None):
    """Places the files of MANIFEST["model_files"] under /opt/hams/models, checksum-verified.

    Production only: anything but environment "prod" (a test environment), a HAMS_ISOLATED_NS=1
    run (test.py's own provisioning) or a `dest_dir` staging run does nothing and returns [], so a
    test host never downloads a model. A file that already verifies (size and SHA-256) is left alone
    with no network access. A missing or wrong one is fetched to `<path>.part`, verified, and only
    then renamed into place; a download that fails or does not verify leaves nothing at the path
    (an installed good copy is never replaced by a bad one) and is recorded as a hook failure.
    `fetch(url, part_path, expected_size)` is the injectable transport (tests pass a fake).
    Returns the list of paths that failed. Honours plan mode."""
    if environment != "prod" or dest_dir or os.environ.get("HAMS_ISOLATED_NS") == "1":
        return []
    fetch = fetch or _fetch_model_to
    failed = []
    for spec in MANIFEST.get("model_files", []):
        path = spec["path"]
        if _model_file_verifies(path, spec):
            continue
        if _plan("download", f"{spec['url']} -> {path} ({spec['size']} bytes, sha256 {spec['sha256']})"):
            continue
        part = path + ".part"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fetch(spec["url"], part, spec["size"])
            if not _model_file_verifies(part, spec):
                raise ValueError(f"{path}: downloaded file does not match the pinned size/SHA-256")
            apply_permissions(part, spec.get("owner"), int(spec.get("mode", "640"), 8))
            os.replace(part, path)
        except Exception as e:  # audit-ignore-catch-all
            _logger.error("[!] Model file %s was not installed: %s", path, e)
            record_hook_failure(f"model_files:{path}", e)
            failed.append(path)
            with contextlib.suppress(OSError):
                os.unlink(part)
    return failed


# [@ANCHOR: infrastructure:provision_systemd_override]
# [@ANCHOR: infrastructure:provision_firewall_rules]
def _ufw_active():
    """True when ufw is installed and reports `Status: active`; False otherwise (including any
    failure to ask: a firewall that cannot be read is left alone)."""
    if not shutil.which("ufw"):
        return False
    try:
        res = subprocess.run(
            ["ufw", "status"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0 and "Status: active" in res.stdout


def _ufw_rule_present(args):
    """True when `ufw status` already lists the allow rule `args` (the `port/proto` form)."""
    try:
        res = subprocess.run(
            ["ufw", "status"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    target = args[-1]
    for line in res.stdout.splitlines():
        fields = line.split()
        if fields and fields[0] == target and "ALLOW" in line and "(v6)" not in line:
            return True
    return False


def provision_firewall_rules(run_cmd_func, environment="prod", dest_dir=""):
    """Adds the ufw rules in MANIFEST["firewall_rules"] for `environment`, idempotently.

    Production only (every entry lists its environments; a test host's run finds none and does
    nothing, so provisioning a test machine never opens a public port). Skipped, with a log line,
    when ufw is absent or inactive: this function does not enable a firewall, it only opens what a
    service needs on one that is already running (hams1's ufw is active with WireGuard and SSH
    allowed). In plan mode it reports what it would add. Never removes a rule."""
    if dest_dir:
        return
    rules = [r for r in MANIFEST.get("firewall_rules", []) if environment in r["environments"]]
    if not rules:
        return
    if not _ufw_active():
        _logger.info("[*] ufw is not installed or not active; not adding %d firewall rule(s)", len(rules))
        return
    for rule in rules:
        if _ufw_rule_present(rule["args"]):
            continue
        if _plan("firewall", f"ufw {' '.join(rule['args'])} comment {rule['comment']!r}"):
            continue
        try:
            run_cmd_func(["ufw", *rule["args"], "comment", rule["comment"]])
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to add ufw rule %s: %s", rule["args"], e)
            record_hook_failure("firewall_rules", e)


def provision_systemd_override(
    run_cmd_func,
    env_vars,
    environment="prod",
    dest_dir="",
    manifest_key="systemd_odoo_override",
    unit_name="odoo",
):
    """Writes a systemd drop-in override for `<unit_name>.service` from
    `MANIFEST[manifest_key]`. Defaults match this function's original,
    odoo-only behavior exactly -- generalized (2026-09-22) to also cover
    pdns.service's own override (see systemd_pdns_override's comment for
    why that one exists) rather than duplicating this whole function for
    one more unit."""
    if environment not in ["prod", "test"]:
        return
    override_data = MANIFEST.get(manifest_key)
    if not override_data:
        return

    override_dir = (
        os.path.join(dest_dir, f"etc/systemd/system/{unit_name}.service.d".lstrip("/"))
        if dest_dir
        else f"/etc/systemd/system/{unit_name}.service.d"
    )
    override_file = os.path.join(override_dir, "override.conf")

    lines = []
    for section, items in override_data.items():
        lines.append(f"[{section}]")
        for k, v in items.items():
            if isinstance(v, list):
                for item in v:
                    lines.append(f"{k}={item}")
            else:
                lines.append(f"{k}={v}")
        lines.append("")

    state = _file_state(override_file, "\n".join(lines))
    if _planning():
        if state != "unchanged":
            _plan("write", f"{override_file} ({state})")
        return
    os.makedirs(override_dir, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(override_file, flags, 0o644)
    # See write_env_files' own comment: os.open()'s mode argument is ignored
    # when the file already exists.
    os.fchmod(fd, 0o644)
    with open(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    apply_permissions(override_file, "root:root", 0o644)

    is_isolated_ns = os.environ.get("HAMS_ISOLATED_NS") == "1"
    if not dest_dir and run_cmd_func and not is_isolated_ns:
        try:
            run_cmd_func(["systemctl", "daemon-reload"])
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("Failed to reload systemd daemons: %s", e)


# Memory limits for Odoo's single gevent (websocket/bus) worker, written into
# /etc/odoo/odoo.conf by initialize_odoo_database() below.
#
# Real production incident, 2026-09-30: with neither key set, the gevent
# worker fell back to Odoo's limit_memory_soft default (2 GiB) and, during a
# burst of websocket traffic, hit it 17 times in 14 minutes, SIGTERMing itself
# every 20-40 s and dropping every open browser's real-time connection
# ("real-time connection lost"). Odoo's check measures psutil's
# memory_info().vms -- VIRTUAL size, not RSS: the freshly forked worker
# already sits at ~1.1-1.3 GiB VSZ from loaded C extensions alone, and the
# burst peaked at 2.15-2.24 GiB. Not a leak; the ceiling was too low.
# 3 GiB was set by hand on hams1 2026-10-01 and held through the following
# daytime traffic with zero recurrences.
#
# The hard limit must be managed too: the gevent worker's RLIMIT_AS comes from
# limit_memory_hard_gevent, else limit_memory_hard, whose Odoo default is only
# 2.5 GiB -- below a 3 GiB soft limit, so on a fresh box the clean soft-limit
# restart could never fire and the worker would die of MemoryError instead.
# limit_memory_hard_gevent (4 GiB, the value hams1's limit_memory_hard already
# has) keeps soft < hard without changing the ordinary HTTP worker pool.
ODOO_CONF_GEVENT_MEMORY_LIMITS = (
    ("limit_memory_soft_gevent", 3 * 1024 * 1024 * 1024),
    ("limit_memory_hard_gevent", 4 * 1024 * 1024 * 1024),
)

# Worker limits for the ordinary HTTP/cron worker pool, written explicitly instead of left to Odoo's
# defaults (hams1 readiness audit 2026-10-04, row 16 / P6). hams1 ran `workers = 25` with
# limit_memory_hard 4 GiB and limit_memory_soft, limit_time_* and max_cron_threads unset, so the real
# ceilings were whatever Odoo's version happened to default to. `workers` itself is unchanged (cpu * 2 + 1).
# Soft 2 GiB (a worker is ~330 MB resident, so this only trips on a runaway request and recycles that
# worker cleanly), hard 4 GiB (the value already live, now stated), 120 s CPU and 240 s real time per
# request, 1800 s for a cron job, two cron threads. The gevent worker keeps its own pair above.
ODOO_CONF_WORKER_LIMITS = (
    ("limit_memory_soft", 2 * 1024 * 1024 * 1024),
    ("limit_memory_hard", 4 * 1024 * 1024 * 1024),
    ("limit_time_cpu", 120),
    ("limit_time_real", 240),
    ("limit_time_real_cron", 1800),
    ("max_cron_threads", 2),
)


# [@ANCHOR: infrastructure:initialize_odoo_database]
def initialize_odoo_database(
    run_cmd_func, hams_open_dir, hams_com_dir, db_name="hams_test"
):
    # db_name used to be the literal "hams_test" here even on a production run, where
    # provision_environment() had just created (and chowned) an empty database named
    # DB_NAME (default "hams_prod"), so every module was installed into a different,
    # stray database and the production one stayed empty. The caller now passes DB_NAME.
    _logger.info("[*] Initializing Odoo database %s with custom modules...", db_name)
    modules = set()
    for d in filter(None, [hams_open_dir, hams_com_dir]):
        if os.path.exists(d):
            for item in os.listdir(d):
                item_path = os.path.join(d, item)
                if os.path.isdir(item_path):
                    if os.path.exists(os.path.join(item_path, "__manifest__.py")):
                        modules.add(item)

    if not modules:
        _logger.info("No custom modules found to initialize.")
        return

    # [@ANCHOR: infrastructure:initialize_odoo_database_install_vs_update_split]
    # Bruce's own instruction, 2026-09-23: this function must be safe to re-run
    # against an existing, populated database to pick up new module changes, not
    # just to provision one from scratch. Before this split existed, every
    # discovered module -- including ones already 'installed' from a prior run --
    # was passed to `-i` unconditionally. Odoo's `-i` on a module already in
    # 'installed' state is a no-op (it does not re-run that module's own upgrade
    # logic), so a real, live database like hams_prod would silently never pick
    # up a later change to a module it already had -- a new field, a new model, a
    # new data file -- while a *fresh* database (nothing installed yet) would
    # still work correctly, since everything there legitimately needs `-i`. Split
    # the discovered modules by their actual current state in db_name and route
    # each into the right flag: `-i` for a module not yet installed (unchanged
    # behavior), `-u` for one that already is (the one that was missing).
    installed_modules = _get_installed_module_names(db_name)
    to_install, to_update = _split_modules_by_install_state(modules, installed_modules)
    mod_string = "base," + ",".join(sorted(modules))
    _logger.info("Initializing modules: %s", mod_string)
    _logger.info(
        "[*] Split by current state in %s: %d to install (%s), %d to update (%s)",
        db_name, len(to_install), ",".join(to_install) or "none",
        len(to_update), ",".join(to_update) or "none",
    )

    addons_path_str = ",".join(filter(None, [
        "/usr/lib/python3/dist-packages/odoo/addons",
        "/var/lib/odoo/.local/share/Odoo/addons/19.0",
        "/usr/lib/python3/dist-packages/addons",
        hams_com_dir,
        hams_open_dir
    ]))

    # Ensure Odoo daemon can access the addons paths if they are in a home directory.
    # Built from the path's own first three segments (not a hardcoded literal) so this
    # works under any developer's home directory, not just one specific machine's.
    for p in [hams_com_dir, hams_open_dir]:
        if p and p.startswith(os.sep + "home" + os.sep):
            home_dir = os.sep.join(p.split(os.sep)[:3])
            run_cmd_func(["sudo", "chmod", "a+x", home_dir])

    try:
        cpu_count = multiprocessing.cpu_count()
        workers = cpu_count * 2 + 1
        
        run_cmd_func(["sudo", "sed", "-i", "/^addons_path/d", "/etc/odoo/odoo.conf"])
        run_cmd_func(["sudo", "sed", "-i", "/^workers/d", "/etc/odoo/odoo.conf"])
        # addons_path_str is built from hams_com_dir/hams_open_dir (ultimately
        # operator/REPO_ROOT-controlled paths, not fixed literals) -- passing
        # it unescaped inside a `bash -c "echo '...'"` string let a single
        # quote anywhere in either path break out of the quoted literal and
        # inject arbitrary shell commands, run as root via sudo. shlex.quote()
        # neutralizes that regardless of what characters the path contains.
        run_cmd_func(["sudo", "bash", "-c", f"echo {shlex.quote(f'addons_path = {addons_path_str}')} >> /etc/odoo/odoo.conf"])
        run_cmd_func(["sudo", "bash", "-c", f"echo {shlex.quote(f'workers = {workers}')} >> /etc/odoo/odoo.conf"])
        # [@ANCHOR: infrastructure:odoo_conf_gevent_memory_limits]
        # Each delete regex is anchored on the key's own "=" so that
        # removing one key never also deletes a longer key sharing its
        # prefix (e.g. a bare /^limit_memory_soft/d would take out
        # limit_memory_soft_gevent too).
        for key, value in ODOO_CONF_GEVENT_MEMORY_LIMITS + ODOO_CONF_WORKER_LIMITS:
            run_cmd_func(["sudo", "sed", "-i", f"/^{key}[[:space:]]*=/d", "/etc/odoo/odoo.conf"])
            run_cmd_func(["sudo", "bash", "-c", f"echo {shlex.quote(f'{key} = {value}')} >> /etc/odoo/odoo.conf"])
    except Exception as e: # audit-ignore-catch-all
        _logger.warning("Failed to update odoo.conf: %s", e)

    # Stop Odoo service to prevent database/port conflicts during initialization
    try:
        run_cmd_func(["sudo", "systemctl", "stop", "odoo.service"])
    except Exception as e:  # audit-ignore-catch-all: best-effort stop before init (e.g. service already stopped/not installed yet on a fresh box); logged and initialization proceeds regardless.
        _logger.warning("Failed to stop odoo.service before init: %s", e)

    # base always goes through -i (a no-op once it's already installed, which it
    # always is on any real database) -- unchanged from this function's prior,
    # single-flag behavior; only the *custom* modules found above are split.
    install_string = "base," + (",".join(to_install) if to_install else "")
    cmd = [
        "sudo", "-u", "odoo", "odoo",
        "-c", "/etc/odoo/odoo.conf",
        "-d", db_name,
        "-i", install_string,
    ]
    if to_update:
        cmd += ["-u", ",".join(to_update)]
    cmd += [
        "--stop-after-init",
        "--without-demo=all",
        "--workers=0",
        "--max-cron-threads=0",
        "--no-http",
        "--addons-path", addons_path_str
    ]
    try:
        run_cmd_func(cmd)
    except subprocess.CalledProcessError as e:
        _logger.error("Failed to initialize Odoo database: %s", e)
        raise

# Real production incident, 2026-09-24: run_post_provision_smoketest()'s own
# `systemctl start` is a plain, blocking call -- fine for every OTHER service it
# starts, which either fails or finishes in seconds (confirmed from the real
# incident log), but callbook.geo.enrich.service is `Type=oneshot` with no
# `RemainAfterExit`, so a blocking `systemctl start` waits for its own `ExecStart`
# process to exit -- and that process can legitimately run for many hours on a
# large backlog (it geo-enriches ham.callbook records via the free, rate-limited
# US Census Bureau Geocoder, one HTTP call per record). On a first-ever backlog
# this smoketest blocked for 16+ hours, stalling every provisioning step after it
# (including initialize_odoo_database(), which stops/upgrades/restarts
# odoo.service). The service's own unit file already documents that it's
# normally driven by its own daily callbook.geo.enrich.timer in the background,
# not expected to run-to-completion synchronously here -- this smoketest only
# needs to confirm it can be STARTED, not wait for a multi-hour batch job to
# finish. `--no-block` returns as soon as systemd has queued the start job,
# matching that intent; the service still runs to completion on its own, just
# not on this process's clock. Deliberately a narrow, named exception (not a
# change to the shared loop's default behavior) so a future long-but-fast-
# failing service added to this list still gets the fast fail-detection a
# blocking start provides. Module-level (not a local inside the function it's
# used in) specifically so _service_start_command() below -- the actual decision
# this incident's fix hinges on -- stays a small, pure, independently-testable
# function, matching this file's own established shape for keeping the smaller
# real logic out of the genuinely destructive/host-dependent orchestration
# functions this test file's own docstring says it doesn't attempt to test.
NON_BLOCKING_START_SERVICES = {
    "callbook.geo.enrich.service",
}


def _service_start_command(svc):
    """The actual `systemctl start` command line for `svc`: `--no-block` for
    anything in NON_BLOCKING_START_SERVICES (see that constant's own comment),
    a plain blocking start otherwise. Pure and side-effect-free on purpose --
    this is the one piece of run_post_provision_smoketest()'s own logic worth
    testing in isolation, without mocking subprocess.run wholesale."""
    if svc in NON_BLOCKING_START_SERVICES:
        return ["systemctl", "start", "--no-block", svc]
    return ["systemctl", "start", svc]


# Standing rule (2026-10-03): a test or development machine never runs a unit
# that fetches from third-party servers -- a regulator's licence register, an
# event calendar, a public geocoder, a DX cluster, a paid API, an ACME CA.
# On a test host such a unit only loads someone else's server for data
# nobody uses, and repeated bulk pulls from one address get that address
# blocked. Incident, 2026-10-02: `provision.py --test` enabled every timer
# and the smoketest started every service on fresh test hosts, and one of
# them pulled the whole national licence database. Each such MANIFEST entry
# carries "external_fetch": "<why>" (the .service and its .timer both); the
# classification comes from reading each daemon's code, not its name.
# test_infrastructure.py's ExternalFetchUnitClassificationTests requires
# every systemd unit in the MANIFEST to be either flagged or listed there as
# audited local-only, so a new unit cannot skip the decision.
# [@ANCHOR: infrastructure:external_fetch_unit_names]
def external_fetch_unit_names():
    """Basenames of every systemd unit the MANIFEST flags external_fetch."""
    names = set()
    for spec in MANIFEST.get("static_files", []):
        if spec.get("external_fetch"):
            names.add(os.path.basename(spec["path"]))
    return names


# [@ANCHOR: infrastructure:host_classes]
# Host classes: a MANIFEST account, directory or file carrying "host_class": "<name>" exists only
# on a host that has been designated that class. "ca_signer" is the host that runs the three CA signer
# daemons of daemons/relay_ca (docs/proposals/CLOUD_HSM_CA_SIGNING.md): hams1, whose signers sign with
# keys held in Google Cloud KMS (HSM). No other host, the dev box and every test host included, gets
# their accounts, directories or units, so no other host ever holds a signing credential. A host is
# designated by `provision.py --host-class ca_signer`, which records it in HOST_CLASSES_FILE (so a
# later plain re-run keeps it), or by the HAMS_HOST_CLASSES environment variable (comma separated).
KNOWN_HOST_CLASSES = frozenset({"b2_backup", "ca_signer", "odoo_tenants"})
HOST_CLASSES_FILE = "/opt/hams/etc/host_classes"


def host_classes(environ=None, path=None):
    """The set of host classes this machine was designated, from HAMS_HOST_CLASSES and the
    host_classes file (one name per line, # comments). Unknown names are an error: a typo must not
    silently leave a host without its class."""
    environ = os.environ if environ is None else environ
    names = {n.strip() for n in environ.get("HAMS_HOST_CLASSES", "").split(",") if n.strip()}
    try:
        with open(path or HOST_CLASSES_FILE, "r", encoding="utf-8") as f:  # audit-ignore-path
            for line in f.read().splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    names.add(line)
    except (FileNotFoundError, PermissionError):
        # Provisioning runs as root and can always read it; a non-root caller (a test, a plan
        # by an unprivileged user) is not on a designated host and sees no classes.
        pass
    unknown = names - KNOWN_HOST_CLASSES
    if unknown:
        raise ValueError(f"unknown host class(es): {', '.join(sorted(unknown))}")
    return names


def _in_host_class(spec, classes=None):
    """True when `spec` (a MANIFEST entry) is for every host or for a class this host has."""
    wanted = spec.get("host_class")
    if not wanted:
        return True
    return wanted in (host_classes() if classes is None else classes)


# [@ANCHOR: infrastructure:daemon_family_selection]
# `provision.py --daemon-family hamsd_<family>`: provision one daemon family's account, key and state
# directories, unit files and sudoers grant, and nothing else (hams1 is installed from the MANIFEST by
# hand, unit by unit, because a full run starts every daemon and differs from the host in many other
# ways). While _DAEMON_FAMILY_ACCOUNTS is set, _spec_selected() answers by what the entry says about
# those accounts, so there is no second list of "the family's entries" to drift out of step:
#   * the account entry whose user is one of them, and any account whose group they join (member_of);
#   * a directory owned by one of them (user or group part), or one every family needs in a known state
#     (marked "daemon_family_shared": True, or carrying the traversal "acl" grant);
#   * a unit file whose [Service] says User=<account>, the .timer / .path of the same name, and a
#     sudoers.d file whose lines start with the account.
_DAEMON_FAMILY_ACCOUNTS = None


@contextlib.contextmanager
def only_daemon_families(accounts):
    """Restricts the provisioning helpers to `accounts` (hamsd_<family> names) inside the block."""
    global _DAEMON_FAMILY_ACCOUNTS
    previous = _DAEMON_FAMILY_ACCOUNTS
    _DAEMON_FAMILY_ACCOUNTS = frozenset(accounts)
    try:
        yield
    finally:
        _DAEMON_FAMILY_ACCOUNTS = previous


def _unit_user(spec):
    """The User= of a MANIFEST unit file entry, or None."""
    for line in (spec.get("content") or "").splitlines():
        if line.startswith("User="):
            return line.split("=", 1)[1].strip()
    return None


def daemon_family_unit_paths(accounts, manifest=None):
    """The /opt/hams/systemd paths of every unit file (service, and the timer or path of the same
    name) for the daemon accounts `accounts`."""
    manifest = MANIFEST if manifest is None else manifest
    accounts = frozenset(accounts)
    stems = set()
    for spec in manifest["static_files"]:
        if spec["path"].endswith(".service") and _unit_user(spec) in accounts:
            stems.add(os.path.splitext(spec["path"])[0])
    # A path unit named for a service that starts a different one (ticket.triage.path -> the event
    # service) is not matched by name; no family unit uses that shape yet.
    return sorted(
        spec["path"]
        for spec in manifest["static_files"]
        if os.path.splitext(spec["path"])[0] in stems
        and spec["path"].endswith((".service", ".timer", ".path"))
    )


def _spec_in_daemon_family(spec):
    accounts = _DAEMON_FAMILY_ACCOUNTS
    if "user" in spec and "group" in spec and "provision_mode" not in spec:
        if spec["user"] in accounts:
            return True
        # An account whose group a selected family account must join (hams_traverse) is created first.
        return any(
            spec["user"] in acc.get("member_of", [])
            for acc in MANIFEST["system_accounts"]
            if acc["user"] in accounts
        )
    if "provision_mode" in spec:
        if spec.get("daemon_family_shared") or spec.get("acl"):
            return True
        return bool(set((spec.get("owner") or ":").split(":")) & accounts)
    path = spec.get("path", "")
    if path in daemon_family_unit_paths(accounts):
        return True
    if path.startswith("/etc/sudoers.d/"):
        return any(
            line.split(" ", 1)[0] in accounts for line in (spec.get("content") or "").splitlines()
        )
    return False


def _spec_selected(spec, only_host_class=None):
    """With `only_host_class` (provision_host_class), exactly that class's entries; with
    only_daemon_families(), exactly those families' entries; otherwise every entry that is for all
    hosts or for a class this host has."""
    if _DAEMON_FAMILY_ACCOUNTS is not None:
        return _spec_in_daemon_family(spec)
    if only_host_class:
        return spec.get("host_class") == only_host_class
    return _in_host_class(spec)


def host_class_unit_names():
    """Basenames of every systemd unit the MANIFEST restricts to a host class."""
    return {
        os.path.basename(spec["path"])
        for spec in MANIFEST.get("static_files", [])
        if spec.get("host_class") and spec["path"].endswith((".service", ".timer", ".path"))
    }


def record_host_class(name, path=None):
    """Adds `name` to HOST_CLASSES_FILE (idempotent). Used by provision.py --host-class."""
    if name not in KNOWN_HOST_CLASSES:
        raise ValueError(f"unknown host class: {name}")
    path = path or HOST_CLASSES_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:  # audit-ignore-path
            existing = f.read().splitlines()
    except FileNotFoundError:
        existing = []
    if name in existing:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:  # audit-ignore-path
        f.write(name + "\n")
    return True


# [@ANCHOR: infrastructure:provision_host_class]
def provision_host_class(host_class, run_cmd_func, env_vars=None, environment="prod"):
    """Provisions ONLY the MANIFEST entries of one host class: its accounts, directories and
    files (the unit), links the unit into /etc/systemd/system and reloads systemd. It enables and
    starts nothing: the signer needs key material and an authorised client first, and its unit
    skips itself (ConditionPathExists) until they are in place. For a host that is not a full
    hams.com server (pi500-1 runs only the FCC sync daemon): `provision.py --only-host-class
    ca_signer` runs this instead of the whole provisioning. Honours plan mode."""
    if host_class not in KNOWN_HOST_CLASSES:
        raise ValueError(f"unknown host class: {host_class}")
    env_vars = dict(os.environ) if env_vars is None else env_vars
    provision_system_accounts(run_cmd_func, environment, only_host_class=host_class)
    apply_production_directories(run_cmd_func, environment, only_host_class=host_class)
    provision_static_files(run_cmd_func, env_vars, environment, only_host_class=host_class)
    systemd_dir = "/opt/hams/systemd"
    for spec in MANIFEST.get("static_files", []):
        unit_path = spec["path"]
        item = os.path.basename(unit_path)
        if spec.get("host_class") != host_class or os.path.dirname(unit_path) != systemd_dir:
            continue
        if not item.endswith((".service", ".timer", ".path")):
            continue
        dst = os.path.join("/etc/systemd/system", item)
        if not os.path.exists(dst) and not _plan("link", f"{dst} -> {unit_path}"):
            os.symlink(unit_path, dst)
    if not _plan("run", "systemctl daemon-reload"):
        run_cmd_func(["systemctl", "daemon-reload"])
    _logger.info("[*] Host class %s provisioned. Nothing was enabled or started.", host_class)


# [@ANCHOR: infrastructure:derived_env_files]
def _derived_env_values(target):
    """The KEY=value pairs for the MANIFEST env_groups file at `target`, taken from the environment files that
    already sit next to it, or None when the file is not a MANIFEST env group or any of its keys has no source.
    A split such as common.env (the non-secret values) or db_app.env (the application database role without the
    superuser password) is cut from the files an earlier provisioning run wrote, so no secret is re-entered."""
    name = os.path.basename(target)
    keys = MANIFEST["env_groups"].get(name)
    if not keys:
        return None
    found = {}
    for sibling in sorted(glob.glob(os.path.join(os.path.dirname(target), "*.env"))):
        if os.path.basename(sibling) == name:
            continue
        try:
            with open(sibling, "r", encoding="utf-8") as f:  # audit-ignore-path: provisioning reads its own env files
                for line in f.read().splitlines():
                    key, sep, value = line.partition("=")
                    if sep and key in keys and key not in found:
                        found[key] = value
        except OSError:
            continue
    return found if set(found) == set(keys) else None


def _write_derived_env_file(target, values):
    """Writes `target` (root:root, 0400, no secret in the plan or the log) from `values`."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    os.fchmod(fd, 0o400)
    with open(fd, "w", encoding="utf-8") as f:
        f.write("".join(f"{key}={values[key]}\n" for key in MANIFEST["env_groups"][os.path.basename(target)]))
    apply_permissions(target, "root:root", 0o400)


# [@ANCHOR: infrastructure:provision_daemon_families]
def provision_daemon_families(accounts, run_cmd_func, env_vars=None, environment="prod"):
    """Provisions ONLY the named daemon families (hamsd_<family> accounts): the account and its group
    (odoo joins it), the directories it owns, the unit files and sudoers grant that run as it, and the
    unit links, then `systemctl daemon-reload`. It enables, starts and restarts nothing and never
    rewrites a unit that is not the family's. A unit file whose EnvironmentFile= (not the `-` optional
    form) is missing is refused before anything is written, because the unit would not start, unless the
    file is a MANIFEST env group that can be cut from the environment files already on the host
    (common.env, db_app.env): then it is written, root:root 0400. Honours plan mode. `provision.py --daemon-family hamsd_<family>` runs this."""
    accounts = list(accounts)
    known = {acc["user"] for acc in MANIFEST["system_accounts"]}
    for account in accounts:
        if not account.startswith("hamsd_") or account not in known:
            raise ValueError(f"not a daemon family account in the MANIFEST: {account}")
    env_vars = dict(os.environ) if env_vars is None else env_vars
    wanted_paths = daemon_family_unit_paths(accounts)
    missing = []
    derive = {}
    for spec in MANIFEST["static_files"]:
        if spec["path"] in wanted_paths and spec["path"].endswith(".service"):
            for line in (spec.get("content") or "").splitlines():
                if line.startswith("EnvironmentFile=") and not line.startswith("EnvironmentFile=-"):
                    target = line.split("=", 1)[1].strip()
                    if os.path.exists(target):
                        continue
                    values = _derived_env_values(target)
                    if values is not None:
                        derive[target] = values
                    else:
                        missing.append(f"{target} (needed by {os.path.basename(spec['path'])})")
    if missing and not _planning():
        raise RuntimeError(
            "environment file(s) missing, nothing was changed: " + ", ".join(sorted(set(missing)))
        )
    for item in sorted(set(missing)):
        _plan("missing", item)
    for target, values in sorted(derive.items()):
        if _plan("write", f"{target} (derived from the existing environment files; keys: {' '.join(sorted(values))})"):
            continue
        _write_derived_env_file(target, values)
    with only_daemon_families(accounts):
        provision_system_accounts(run_cmd_func, environment)
        apply_production_directories(run_cmd_func, environment)
        provision_static_files(run_cmd_func, env_vars, environment)
    for unit_path in wanted_paths:
        dst = os.path.join("/etc/systemd/system", os.path.basename(unit_path))
        if not os.path.lexists(dst) and not _plan("link", f"{dst} -> {unit_path}"):
            os.symlink(unit_path, dst)
    if not _plan("run", "systemctl daemon-reload"):
        run_cmd_func(["systemctl", "daemon-reload"])
    _logger.info("[*] Daemon families %s provisioned. Nothing was enabled, started or restarted.", ", ".join(accounts))


# [@ANCHOR: infrastructure:opt_in_unit_names]
def opt_in_unit_names():
    """Basenames of every systemd unit the MANIFEST marks "opt_in": units that cost money or must
    be a deliberate decision (code.review.sweep calls the paid Gemini API). Provisioning links them
    but never enables or starts them, in any environment, unless the operator names them with
    provision.py --enable-opt-in. Found by the round-2 production runbook, 2026-10-03: a re-run
    would have enabled code.review.sweep.timer on hams1, where it is deliberately only linked."""
    return {
        os.path.basename(spec["path"])
        for spec in MANIFEST.get("static_files", [])
        if spec.get("opt_in")
    }


# [@ANCHOR: infrastructure:activation_units_to_enable]
def _activation_units_to_enable(linked_units, is_test_env, opt_in_units=()):
    """The linked .timer/.path units provisioning should `systemctl enable`.

    Opt-in units (opt_in_unit_names()) are enabled only when named in opt_in_units. In a test
    environment (provision.py --test, or test.py's isolated provisioning) external-fetch units
    are also left linked but never enabled, so no timer ever fires them; there, naming an
    external-fetch unit in opt_in_units does not override that rule."""
    held = opt_in_unit_names() - set(opt_in_units)
    if is_test_env:
        held |= external_fetch_unit_names()
    return [unit for unit in linked_units if unit not in held]


# Long-running daemons that production must start again after a reboot.
#
# Found by the 2026-10-04 production-readiness audit of hams1: provisioning linked every
# /opt/hams/systemd/*.service into /etc/systemd/system but only ever ran `systemctl enable`
# for .timer and .path units, so on hams1 sixteen daemons (adif.ingress, backup.worker,
# dx.firehose, hams.simulated.band, hams.simulated.bots ...) were "linked",
# running only because the smoketest had started them. `systemctl is-enabled` said "linked",
# `WantedBy=` was empty, and none appeared in multi-user.target's dependencies, so the first
# reboot (one is overdue: a newer kernel has been waiting since 2026-09-30) would have
# brought up Odoo and PostgreSQL and none of those daemons.
#
# A unit is enabled at boot when it is a plain long-running service (not a oneshot, not an
# instance template) that asks for multi-user.target, is shipped for "prod", and is not
# opt-in. Units that need a deliberate release step stay link-only (listed below). Only
# production-style runs enable them: a test environment and a --hold-odoo run never do.
BOOT_ENABLE_EXCLUDED_SERVICES = {
    # Binds TCP 443 for auth.hams.com and needs a certificate, anchors and a derived secret;
    # its release steps are documented in PROVISION_PRODUCTION_NOTES.md.
    "hams-auth-gateway.service": "release-time unit: needs certificate, anchors and secret first",
    # The relay certificate authority runs only on the custody host and needs its key unlocked.
    "hams-relay-ca.service": "CA custody host only; key must be unlocked deliberately",
}


# [@ANCHOR: infrastructure:boot_service_unit_names]
def boot_service_unit_names():
    """Basenames of the long-running MANIFEST services that should be enabled at boot in production.

    See BOOT_ENABLE_EXCLUDED_SERVICES and the comment above it. Derived from the MANIFEST so a
    new daemon is covered without a second list to forget; the audited exclusions are explicit."""
    opt_in = opt_in_unit_names()
    names = set()
    for spec in MANIFEST.get("static_files", []):
        path = spec.get("path", "")
        name = os.path.basename(path)
        content = spec.get("content", "")
        if (
            os.path.dirname(path) == "/opt/hams/systemd"
            and name.endswith(".service")
            and "@" not in name
            and "prod" in spec.get("environments", [])
            and "WantedBy=multi-user.target" in content
            and "Type=oneshot" not in content
            and name not in opt_in
            and name not in BOOT_ENABLE_EXCLUDED_SERVICES
        ):
            names.add(name)
    return names


# [@ANCHOR: infrastructure:smoketest_candidate_services]
def _smoketest_candidate_services(has_hams_com=True, is_test_env=False, opt_in_units=()):
    """Services run_post_provision_smoketest() may start, in start order.

    In a test environment external-fetch services are left out, so the
    smoketest never starts one (a blocking start of a oneshot sync runs
    the whole download)."""
    potential_services = [
        "postgresql",
        "redis-server",
        "rabbitmq-server",
        "pdns",
        "odoo",
    ]

    daemons_to_skip = {
        "system-startup.service",
        "hams-pycache.service"
    }
    external = external_fetch_unit_names() if is_test_env else set()
    # Never started by the smoketest unless the operator opted in (a start of
    # code.review.sweep.service runs a whole paid review).
    external |= opt_in_unit_names() - set(opt_in_units)

    for sf in MANIFEST.get("static_files", []):
        path = sf.get("path", "")
        if "systemd" in path and path.endswith(".service"):
            svc_name = os.path.basename(path)
            if not has_hams_com and svc_name != "hams-pycache.service":
                continue
            if not is_test_env and svc_name in daemons_to_skip:
                continue
            if svc_name in external:
                continue
            if not _in_host_class(sf):
                continue
            if svc_name not in potential_services and "@" not in svc_name:
                potential_services.append(svc_name)
    return potential_services


def run_post_provision_smoketest(has_hams_com=True, is_test_env=False, opt_in_units=()):
    _logger.info("[*] Running post-provisioning smoketest on all services...")

    try:
        subprocess.run(["systemctl", "daemon-reload"], check=False)
    except OSError as e:
        _logger.debug("Ignored OSError during daemon-reload: %s", e)

    potential_services = _smoketest_candidate_services(
        has_hams_com, is_test_env, opt_in_units
    )

    _logger.info("DEBUG potential_services: %s", potential_services)
    _logger.info("DEBUG has_hams_com: %s, is_test_env: %s", has_hams_com, is_test_env)

    services_to_test = []
    for svc in potential_services:
        res = subprocess.run(
            ["systemctl", "status", svc], capture_output=True, text=True
        )
        if (
            "could not be found" not in res.stderr
            and "could not be found" not in res.stdout
        ):
            services_to_test.append(svc)

    started_services = []
    already_active_services = []
    start_failures = []
    # A --test run must never leave a service it started running, however
    # the smoketest ends: both failure paths below sys.exit(1), and before
    # 2026-10-02 they exited before the stop loop, so a test host kept
    # every started daemon running. The finally runs on SystemExit too.
    # [@ANCHOR: infrastructure:smoketest_test_mode_always_stops]
    try:
        for svc in services_to_test:
            res_active = subprocess.run(
                ["systemctl", "is-active", svc], capture_output=True, text=True
            )
            if res_active.stdout.strip() == "active":
                if svc == "odoo":
                    subprocess.run(["systemctl", "restart", "odoo"])
                _logger.info("    %s is already active, skipping start.", svc)
                already_active_services.append(svc)
                continue

            if svc in NON_BLOCKING_START_SERVICES:
                # See NON_BLOCKING_START_SERVICES' own comment above: this specific
                # service can legitimately run for hours, so --no-block only waits
                # for systemd to accept/queue the start job, not for the service's
                # own ExecStart to finish. A returncode of 0 here means "dispatched,"
                # not "completed" -- distinct from every other service in this loop.
                _logger.info("    Starting %s (non-blocking -- may still be running when this smoketest finishes)...", svc)
            else:
                _logger.info("    Starting %s...", svc)
            res = subprocess.run(
                _service_start_command(svc), capture_output=True, text=True
            )
            started_services.append(svc)
            if res.returncode != 0:
                logs = subprocess.run(
                    ["journalctl", "-u", svc, "-n", "100", "--no-pager"],
                    capture_output=True,
                    text=True,
                )
                if "Address already in use" in logs.stdout or "Address already in use" in logs.stderr:
                    _logger.warning(
                        "    [~] %s failed to start due to port conflict ('Address already in use'). Assuming it is running externally or port is handled.", svc
                    )
                    started_services.remove(svc)
                else:
                    _logger.error(
                        "    [!] systemctl start %s returned non-zero exit code: %s",
                        svc,
                        res.returncode,
                    )
                    _logger.error("stdout: %s", res.stdout)
                    _logger.error("stderr: %s", res.stderr)
                    _logger.error(
                        "--- LOGS FOR %s ---\n%s\n-------------------", svc, logs.stdout
                    )
                    # Keep going and start the rest, so one run reports every service that will not
                    # start. Stopping at the first cost a full provisioning run per problem on the
                    # first production release (2026-09-21).
                    start_failures.append(svc)

        if start_failures:
            _logger.error(
                "[!] %d service(s) failed to start: %s", len(start_failures), ", ".join(start_failures)
            )
            sys.exit(1)

        _logger.info("[*] Waiting for services to stabilize (5 seconds)...")
        time.sleep(5)

        failed = False
        for svc in started_services + already_active_services:
            res = subprocess.run(
                ["systemctl", "is-failed", svc], capture_output=True, text=True
            )
            state = res.stdout.strip()
            if state == "failed":
                _logger.error("[!] Service %s failed to start or crashed.", svc)
                logs = subprocess.run(
                    ["journalctl", "-u", svc, "-n", "100", "--no-pager"],
                    capture_output=True,
                    text=True,
                )
                _logger.error(
                    "--- LOGS FOR %s ---\n%s\n-------------------", svc, logs.stdout
                )
                failed = True

        if failed:
            _logger.error(
                "[!] One or more services failed the smoketest. Aborting snapshot."
            )
            sys.exit(1)

        _logger.info("[*] All services started successfully.")
    finally:
        if is_test_env:
            _logger.info("[*] Shutting down the services this smoketest started (--test mode)...")
            for svc in reversed(started_services):
                _logger.info("    Stopping %s...", svc)
                subprocess.run(["systemctl", "stop", svc], capture_output=True)

    _logger.info("[*] Smoketest complete: %s", datetime.now())


def generate_secure_password(length=32):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

# [@ANCHOR: infrastructure:_refuse_env_files_from_other_provision_mode]
def _refuse_env_files_from_other_provision_mode(file_vars, is_test, env_dir):
    """
    Keeps test-mode and production provisioning from inheriting each other's saved values.

    load_and_prompt_env() reads every *.env under env_dir with setdefault before it applies either
    mode's defaults, so values saved by one mode win in the other: a test run on a box once
    provisioned for production picked up DB_NAME=hams_prod, DB_HOST, the generated secrets, DOMAIN
    and ODOO_URL, and a production run after a test run would pick up DB_PASS=odoo and
    ODOO_ADMIN_PASSWORD=admin.

    A separate test-mode directory does not fix that on its own. write_env_files() persists both
    modes into /opt/hams/etc because every systemd unit reads its EnvironmentFile from there, so a
    test run reading a different directory would then overwrite production's generated secrets
    with test defaults. Instead each run records its mode (HAMS_PROVISION_MODE, in core.env), and a
    run in the other mode stops here. HAMS_ALLOW_PROVISION_MODE_SWITCH=1 lets it proceed, and then
    nothing from the old files is used: the switch starts from that mode's own defaults and freshly
    generated secrets, and write_env_files() replaces the old files.

    Files written before this marker existed carry no mode and are read as before.
    _refuse_if_unsafe_test_db_drop still guards that case's most destructive outcome.
    """
    requested = "test" if is_test else "prod"
    recorded = file_vars.get("HAMS_PROVISION_MODE")
    if not recorded or recorded == requested:
        return file_vars
    if os.environ.get("HAMS_ALLOW_PROVISION_MODE_SWITCH") != "1":
        raise RuntimeError(
            f"{env_dir} was provisioned in {recorded!r} mode, and this run is {requested!r} mode. "
            f"Reusing its saved values would carry {recorded} settings and secrets into a "
            f"{requested} deployment. To switch this box to {requested} mode, starting from "
            f"{requested} defaults and replacing the saved files, rerun with "
            "HAMS_ALLOW_PROVISION_MODE_SWITCH=1."
        )
    _logger.warning(
        "[!] Switching %s from %s to %s provisioning; ignoring its saved values.",
        env_dir,
        recorded,
        requested,
    )
    return {}


# [@ANCHOR: infrastructure:set_pdns_zone_defaults]
# Verified by [@ANCHOR: infrastructure:test_pdns_zone_defaults]
def _set_pdns_zone_defaults(env_vars):
    """pdns_sync's public-DNS settings (hams_com daemons/pdns_sync/main.py):
    the nameservers every zone it creates names in its apex NS, and the one
    parent zone Cloudflare delegates to them, 'u.<DOMAIN>' (personal zones are
    '<alias>.u.<DOMAIN>'; ham_dns's PERSONAL_ZONE_LABEL must match). Derived
    from DOMAIN, which the caller has already required."""
    domain = env_vars["DOMAIN"].strip().strip(".")
    env_vars.setdefault("PDNS_ZONE_NAMESERVERS", f"ns1.{domain},ns2.{domain}")
    env_vars.setdefault("PDNS_PERSONAL_PARENT_ZONE", f"u.{domain}")


def load_and_prompt_env(env_vars, is_test):
    """
    Populate env_vars for provisioning, non-interactively.

    No longer prompts (formerly: a short interactive question-and-answer
    program, run by whoever was at the terminal during provisioning). Instead:
    site-specific values are read from any *.env file already dropped into
    env_dir (KEY=VALUE lines, one file per concern -- db.env, smtp.env, etc.,
    see MANIFEST["env_groups"]); anything still missing after that either
    gets a safe, non-secret default (SMTP/Gemini/Cloudflare settings), gets
    freshly generated (DB_PASS, ODOO_ADMIN_PASSWORD, and the other secrets
    below -- write_env_files() later persists whatever was generated back
    into env_dir at mode 400, so it's recoverable after the fact instead of
    only ever having existed in one operator's terminal history), or, for the
    one value with no safe default or generation strategy (DOMAIN -- which
    *site* this box is being provisioned for), raises loudly instead of
    guessing.

    This generalizes to more than one box: provisioning a given server is
    "populate env_dir on it, then run provision.py" regardless of how many
    servers, databases, or Odoo sites eventually exist. Today that's one
    flat env_dir on one server for the one hams.com site. A future
    multi-server/multi-DB/multi-site topology doesn't need a different
    mechanism, just more instances of this same one: each server or site
    gets its own env_dir populated with its own DOMAIN and its own secrets
    (e.g. a site-specific tar of *.env files, extracted onto a fresh box
    before provision.py runs), not a new provisioning path.
    """
    env_dir = "/opt/hams/etc"
    file_vars = {}
    if os.path.exists(env_dir):
        for env_file in glob.glob(os.path.join(env_dir, "*.env")):
            try:
                with open(env_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, val = line.split("=", 1)
                            file_vars.setdefault(key.strip(), val.strip())
            except OSError as e:
                _logger.warning("Failed to read %s: %s", env_file, e)
    file_vars = _refuse_env_files_from_other_provision_mode(file_vars, is_test, env_dir)
    for key, val in file_vars.items():
        env_vars.setdefault(key, val)
    env_vars["HAMS_PROVISION_MODE"] = "test" if is_test else "prod"

    # The key hams_relay_bridge presents to Odoo (ham_relay_bridge.api_key). Generated in both
    # modes: the bridge refuses to start without one unless HAMS_RELAY_BRIDGE_DEV=1 (a flag
    # provisioning never writes), and a
    # random key costs a test host nothing. Truthiness, not presence: hams1 ran 2026-09-23..10-03
    # with a 0-byte bridge.env, so no relay could uplink. _sync_bridge_api_key_to_odoo() writes the
    # same value into the database.
    if not env_vars.get("BRIDGE_API_KEY", "").strip():
        env_vars["BRIDGE_API_KEY"] = secrets.token_urlsafe(48)

    # stun.hams.com (hams_com docs/proposals/ICE_DIRECT_ROUTING.md section 11.2): the bridge's STUN
    # responder binds this address when BRIDGE_STUN_BIND is set, and not at all when it is not.
    # Production gets it by default (dual stack, UDP 3478; MANIFEST["firewall_rules"] opens the
    # port); a test host does not, so provisioning a test machine opens no public UDP port. An
    # operator who set it (or set it empty, to switch the responder off) keeps their value.
    if not is_test:
        env_vars.setdefault("BRIDGE_STUN_BIND", "[::]:3478")

    if is_test:
        env_vars.setdefault("ODOO_URL", "http://odoo:8069")
        env_vars.setdefault("REDIS_HOST", "redis")
        env_vars.setdefault("RABBITMQ_HOST", "rabbitmq")
        env_vars.setdefault("DB_NAME", "hams_test")
        env_vars.setdefault("DB_USER", "odoo")
        env_vars.setdefault("DB_PASS", "odoo")
        env_vars.setdefault("DB_HOST", "postgres")
        env_vars.setdefault("PDNS_API_URL", "http://powerdns:8081/api/v1/servers/localhost/zones")
        env_vars.setdefault("PDNS_API_KEY", "secret")
        env_vars.setdefault("DOMAIN", "localhost")
        _set_pdns_zone_defaults(env_vars)
        env_vars.setdefault("ODOO_ADMIN_PASSWORD", "admin")
        env_vars.setdefault("ODOO_SERVICE_PASSWORD", "service")
        env_vars.setdefault("SMTP_HOST", "localhost")
        env_vars.setdefault("SMTP_PORT", "1025")
        env_vars.setdefault("HAMS_CRYPTO_KEY", "0000000000000000000000000000000000000000000=")
        # A test box's broker only has the factory account (see MANIFEST["env_defaults"]).
        env_vars.setdefault("RMQ_USER", "guest")
        env_vars.setdefault("RMQ_PASS", "guest")
    else:
        # Set automatic sensible defaults
        env_vars.setdefault("DB_NAME", "hams_prod")
        env_vars.setdefault("DB_USER", "odoo")
        env_vars.setdefault("DB_HOST", "postgres")
        env_vars.setdefault("REDIS_HOST", "redis")
        env_vars.setdefault("REDIS_PORT", "6379")
        env_vars.setdefault("RABBITMQ_HOST", "rabbitmq")
        env_vars.setdefault("RMQ_PORT", "5672")
        # Real bug found and fixed 2026-09-13 hardware-qualifying pi500-1: this used to
        # default to the literal "guest" (RabbitMQ's well-known factory-default account),
        # which daemons/adif_processor/main.py's own _require_rabbitmq_credentials
        # deliberately refuses to accept for either RMQ_USER or RMQ_PASS -- see
        # _create_rabbitmq_user_if_missing's own docstring for the full investigation,
        # including confirmation this was never actually working on the dev box either.
        # A fixed, non-"guest" service-account name (mirroring DB_USER's own fixed "odoo"
        # default) is fine here -- RabbitMQ usernames don't need generated entropy the way
        # RMQ_PASS does.
        env_vars.setdefault("RMQ_USER", "hams_rabbitmq")
        env_vars.setdefault("PDNS_API_URL", "http://powerdns:8081/api/v1/servers/localhost/zones")
        # Real bug found live on hams1, 2026-09-22: this generic "WS_PORT" key (8080) had
        # nothing to do with dx_firehose's own actual port (8765, its own code default) --
        # it collided with hams_data_relay's own default bind address instead, and won over
        # dx.firehose.service's unit-level Environment="WS_PORT=8765" override because both
        # were set to the SAME env var name in the SAME shared core.env file. Renamed to a
        # daemon-specific key (see dx_firehose/main.py) so this can't collide with anything
        # else's port default again, regardless of EnvironmentFile=/Environment= ordering.
        env_vars.setdefault("DX_FIREHOSE_WS_PORT", "8765")
        env_vars.setdefault("PYTHONPYCACHEPREFIX", "/tmp/pycache")
        env_vars.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/hams/playwright")
        # Bruce, 2026-09-22: "HAMS/1.0" alone carries no way for a server
        # operator to reach us -- polite bot etiquette (and Bruce's own
        # explicit intent) is to self-identify with real contact info, so a
        # site that wants to allowlist or complain about this traffic can.
        # Real gap found live on hams1: the actual deployed value had never
        # included this at all, despite Bruce believing it already did.
        env_vars.setdefault(
            "SYSTEM_USER_AGENT",
            "HamsComSyncDaemon/1.0 (+https://crawler.hams.com)",
        )

        if "DB_PASS" not in env_vars:
            env_vars["DB_PASS"] = generate_secure_password()
        if "PDNS_API_KEY" not in env_vars:
            env_vars["PDNS_API_KEY"] = generate_secure_password()
        if "ODOO_SERVICE_PASSWORD" not in env_vars:
            env_vars["ODOO_SERVICE_PASSWORD"] = generate_secure_password()
        if "HAMS_CRYPTO_KEY" not in env_vars:
            env_vars["HAMS_CRYPTO_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')
        if "RMQ_PASS" not in env_vars:
            env_vars["RMQ_PASS"] = generate_secure_password()
        # Production Redis has no unauthenticated access (2026-10-03, after the hams1 test runner
        # flushed production Redis db 0 through it). Every client authenticates as this ACL user;
        # see _redis_acl_include_content.
        env_vars.setdefault("REDIS_USERNAME", "hams_redis")
        if "REDIS_PASSWORD" not in env_vars:
            env_vars["REDIS_PASSWORD"] = generate_secure_password()
        env_vars["REDIS_URL"] = redis_url(env_vars)

        # DOMAIN identifies which site this box is being provisioned for and
        # has no safe default -- silently assuming "hams.com" would mean a
        # second site's box gets provisioned as hams.com by accident the
        # first time someone forgets to set it. Fail fast instead of
        # guessing or blocking on an interactive prompt: whoever is
        # provisioning must supply it via an env file (see module docstring
        # above load_and_prompt_env).
        if not env_vars.get("DOMAIN", "").strip():
            raise RuntimeError(
                "DOMAIN is not set. Provisioning requires a DOMAIN=<site-domain> line in one "
                f"of {env_dir}/*.env (or in the environment already), naming which site this "
                "box is for -- e.g. DOMAIN=hams.com."
            )
        domain = env_vars["DOMAIN"]
        _set_pdns_zone_defaults(env_vars)

        env_vars.setdefault("ODOO_URL", "http://odoo:8069")
        env_vars.setdefault("SYSADMIN_EMAILS", f"admin@{domain}")

        # ODOO_ADMIN_PASSWORD is a real, human-facing login credential (unlike
        # DB_PASS/RMQ_PASS/etc above, which nothing but the software itself
        # ever needs to know) -- but it's handled the same way: generated if
        # absent, and recoverable afterward from wherever provision_environment()
        # persists env_vars (write_env_files() writes it to one of
        # /opt/hams/etc/*.env at mode 400, root:root, alongside every other
        # generated secret) rather than requiring a human to sit at a
        # terminal and type one in during provisioning.
        if "ODOO_ADMIN_PASSWORD" not in env_vars:
            env_vars["ODOO_ADMIN_PASSWORD"] = generate_secure_password()

        # Bruce, 2026-09-22: this used to default to smtp.mailgun.org -- stale from an
        # earlier provider, never actually configured deliberately. hams.com sends outbound
        # mail through Amazon SES (daemons/ses_inbound_mail_ingest handles the inbound half
        # of the same account), whose SMTP endpoint is region-specific
        # (email-smtp.<region>.amazonaws.com); AWS_REGION defaults to us-east-1 to match
        # that daemon's own default. SMTP_USER/SMTP_PASS for SES are an IAM access key id and
        # its SigV4-derived SMTP password (see hams_shared/tools/derive_ses_smtp_password.py),
        # not an email address -- "none" as a SMTP_PASS placeholder still fails closed exactly
        # as it did for the old default.
        env_vars.setdefault("AWS_REGION", "us-east-1")
        env_vars.setdefault("SMTP_HOST", f"email-smtp.{env_vars['AWS_REGION']}.amazonaws.com")
        env_vars.setdefault("SMTP_PORT", "587")
        env_vars.setdefault("SMTP_USER", "none")
        env_vars.setdefault("SMTP_PASS", "none")

        env_vars.setdefault("GEMINI_API_KEY", "none")
        env_vars.setdefault("GEMINI_MODEL", "gemini-2.5-pro")

        env_vars.setdefault("CLOUDFLARE_API_TOKEN", "none")

        # Auto-derive Cloudflare Zone ID from Domain
        if "CLOUDFLARE_ZONE_ID" not in env_vars:
            cf_token = env_vars.get("CLOUDFLARE_API_TOKEN", "none")
            if cf_token and cf_token != "none":
                print(f"[*] Attempting to derive Cloudflare Zone ID for {domain}...")
                try:
                    req = urllib.request.Request(
                        f"https://api.cloudflare.com/client/v4/zones?name={domain}",
                        headers={"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=5) as response:
                        data = json.loads(response.read().decode())
                        if data.get("success") and data.get("result"):
                            zone_id = data["result"][0]["id"]
                            print(f"[*] Successfully derived Zone ID: {zone_id}")
                            env_vars["CLOUDFLARE_ZONE_ID"] = zone_id
                        else:
                            print("[!] Could not find Zone ID for domain.")
                except Exception as e:  # audit-ignore-catch-all: best-effort Zone ID auto-derivation; falls back to "none" below regardless of cause (DNS/network failure, malformed JSON, unexpected API response shape), so provisioning must continue rather than abort.
                    print(f"[!] Failed to fetch Cloudflare Zone ID: {e}")
                    _logger.warning("Failed to fetch Cloudflare Zone ID for %s: %s", domain, e)
            
        env_vars.setdefault("CLOUDFLARE_ZONE_ID", "none")
        env_vars.setdefault("CLOUDFLARE_TUNNEL_TOKEN", "none")


# [@ANCHOR: infrastructure:_role_exists]
def _role_exists(role_name):
    """
    Checks (via a real, non-shell psql invocation) whether a PostgreSQL role
    named role_name exists, mirroring _database_exists's own pattern exactly
    -- see that function's docstring for why role_name is never spliced into
    a SQL string or shell command line, and for the real `-c`/`-tAc` bug
    this now avoids by piping the SQL over stdin instead.
    """
    res = subprocess.run(
        [
            "sudo", "-u", "postgres", "psql",
            "-v", f"role_name={role_name}",
            "-tA",
        ],
        input="SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'role_name';\n",
        capture_output=True,
        text=True,
    )
    return res.returncode == 0 and res.stdout.strip() == "1"


# [@ANCHOR: infrastructure:postgresql_lockdown]
POSTGRESQL_LOCKDOWN_SETTINGS = (
    ("listen_addresses", "'127.0.0.1, ::1'"),
    ("shared_preload_libraries", "'pg_stat_statements'"),
)


def _conf_value(line, key):
    """The value of an active `key = value` line of a postgresql.conf-style file, else None."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    name, sep, rest = stripped.partition("=")
    if not sep or name.strip() != key:
        return None
    value = rest.strip()
    if value.startswith("'"):
        close = value.find("'", 1)
        if close != -1:
            value = value[: close + 1]
    elif "#" in value:
        value = value.split("#", 1)[0].strip()
    return value


# [@ANCHOR: infrastructure:_set_conf_key]
def _set_conf_key(text, key, value):
    """Sets `key = value` in postgresql.conf-style text, replacing every active line for key with one.

    Returns (new_text, effective_changed). effective_changed is whether the value the server would
    use changes: PostgreSQL takes the LAST active line for a key, so collapsing duplicates that all
    hold the wanted value is a text change but not an effective one, and needs no restart. Until
    2026-10-03 provisioning appended both lockdown lines on every run; hams1 had eleven duplicate
    pairs."""
    effective = None
    first_index = None
    kept = []
    for line in text.splitlines(keepends=True):
        current = _conf_value(line, key)
        if current is None:
            kept.append(line)
            continue
        effective = current
        if first_index is None:
            first_index = len(kept)
    wanted = f"{key} = {value}\n"
    if first_index is None:
        if kept and not kept[-1].endswith("\n"):
            kept[-1] += "\n"
        kept.append(wanted)
    else:
        kept.insert(first_index, wanted)
    return "".join(kept), effective != value


# [@ANCHOR: infrastructure:_apply_postgresql_lockdown]
def _apply_postgresql_lockdown(conf_paths=None):
    """
    Restricts PostgreSQL to loopback in every cluster's postgresql.conf, idempotently, and returns
    True when a setting's effective value changed, i.e. when the server needs a restart. A file
    whose text changes only by losing duplicate lines is rewritten without asking for one: a
    PostgreSQL restart can crash odoo.service (hams_com CLAUDE.md, 2026-10-01).

    pg_hba.conf is deliberately left as Debian ships it.

    This step used to run `sed -i 's/peer/trust/g'` over pg_hba.conf, which
    turned `local all postgres peer` and `local all all peer` into `trust`:
    any local OS account could then connect as any role, including the
    superuser, which defeats the per-service OS accounts the zero-sudo
    design relies on. Nothing needed it. Stock authentication already covers
    every real connection path: Odoo and every daemon unit run as the `odoo`
    OS user (peer over the socket), provisioning's own psql calls run as
    `sudo -u postgres` (peer), and TCP clients -- test.py's
    PGHOST=localhost, dx_firehose's asyncpg pool, pager_duty's pg_dump check
    -- log in with the `odoo` role's password under the stock
    `host ... 127.0.0.1/32 scram-sha-256` lines.
    """
    if conf_paths is None:
        conf_paths = sorted(glob.glob("/etc/postgresql/*/main/postgresql.conf"))
    restart_needed = False
    for path in conf_paths:
        with open(path, "r", encoding="utf-8") as f:
            original = f.read()
        text = original
        effective_changed = False
        for key, value in POSTGRESQL_LOCKDOWN_SETTINGS:
            text, changed = _set_conf_key(text, key, value)
            effective_changed = effective_changed or changed
        restart_needed = restart_needed or effective_changed
        if text == original:
            continue
        if _plan("write", f"{path} (lockdown settings; restart needed: {effective_changed})"):
            continue
        st = os.stat(path)
        tmp_path = path + ".hams-provision.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chown(tmp_path, st.st_uid, st.st_gid)
        os.chmod(tmp_path, st.st_mode & 0o7777)
        os.replace(tmp_path, path)
    return restart_needed


# [@ANCHOR: infrastructure:_create_odoo_role_if_missing]
def _create_odoo_role_if_missing(run_cmd_func, db_pass):
    """
    Creates the `odoo` PostgreSQL role with the given password, IF it
    doesn't already exist -- checked in Python via _role_exists() first
    (mirroring _create_database_if_missing's own pattern), not via a SQL-side
    IF NOT EXISTS.

    Two real bugs found and fixed 2026-09-13 hardware-qualifying pi500-1 (a
    genuinely fresh Raspberry Pi 500), layered on top of each other:

    1. The original version of this function wrapped the CREATE ROLE in a
       `DO $$...$$` PL/pgSQL block so the SQL itself could check IF NOT
       EXISTS. psql's own `:'variable'` substitution does not apply inside
       a dollar-quoted (`$$...$$`) string body, so `PASSWORD :'db_pass'`
       inside the DO block was sent to the server as the literal,
       unsubstituted text, which PostgreSQL's own parser rejected outright
       with "syntax error at or near ':'" before the IF NOT EXISTS check
       ever ran. Fixed by moving the existence check into Python
       (_role_exists, mirroring _create_database_if_missing's own pattern)
       so the real CREATE ROLE could be a plain, non-dollar-quoted
       statement.
    2. That fix alone still failed identically -- re-tested and confirmed,
       not assumed. Root-caused further: `:'variable'` substitution does
       not apply AT ALL when the SQL is passed via `-c`/`-tAc` on the psql
       command line, regardless of dollar-quoting -- confirmed with a bare
       `sudo -u postgres psql -v x=hello -c "SELECT :'x';"`, which fails
       with the identical syntax error, and the identical query piped over
       stdin instead (`echo "SELECT :'x';" | psql -v x=hello`) succeeds.
       This is a genuine, version-independent psql behavior (confirmed on
       psql 18.6, the same version on both the dev box and pi500-1) -- not
       a Pi/bookworm-specific quirk. Every one of this project's psql calls
       using `:'var'`/`:"var"` substitution combined with `-c`/`-tAc`
       (this function, _role_exists, _database_exists,
       _alter_database_owner_to_odoo) had the identical, never-actually-
       worked-on-a-fresh-box bug -- all fixed together the same way: the
       SQL text now goes over stdin instead of `-c`/`-tAc`.

    This was never caught on the dev box because its own `odoo` role
    (and `hams_test`/`hams_prod` database) already existed from prior
    history predating this whole `:'var'`-substitution mechanism --
    masking the bug the same way this same qualification pass already
    found for python3-pypdf and python3-lxml-html-clean: nobody had run
    any of these four functions against a genuinely fresh Postgres
    instance before.

    db_pass reaches here from env_vars["DB_PASS"] -- machine-generated via
    generate_secure_password() on a fresh box (safe: letters/digits only),
    but on a re-provisioned box it can instead come from an operator-edited
    *.env file (see load_and_prompt_env), which is NOT guaranteed to be free
    of a single quote or other SQL-literal-breaking characters. The SQL text
    itself never has db_pass spliced into it directly -- the value is passed
    via psql's own `-v name=value` mechanism and referenced in the SQL as
    `:'db_pass'`, which psql expands using proper SQL string-literal quoting
    (quote_literal semantics) when the SQL is read as a script (stdin/`-f`),
    regardless of what characters the value contains. This safety property
    is unchanged from the prior version -- only the delivery mechanism
    (stdin instead of `-c`) is different, since that's the one that
    actually makes the substitution happen at all.
    """
    if _role_exists("odoo"):
        # Bug-hunt fix (2026-10-02, found live provisioning jetson-1, a
        # genuinely fresh Ubuntu box): the Debian/Ubuntu `odoo` apt package's
        # own postinst already creates a basic, non-superuser `odoo`
        # PostgreSQL role (`Create DB` only) as a side effect of package
        # installation, running BEFORE this function ever does -- so
        # `_role_exists("odoo")` was already true by the time provisioning
        # reached here, and the early `return` above meant the CREATE ROLE
        # ... WITH SUPERUSER below (needed for `CREATE EXTENSION vector`,
        # among other superuser-only DDL Odoo's own install step issues)
        # never ran. Confirmed live: `psql -c '\du odoo'` showed only
        # `Create DB`, and module installation failed with
        # "psycopg2.errors.InsufficientPrivilege: permission denied to
        # create extension \"vector\" ... Must be superuser." Never caught
        # on the dev box/hams1 because their own `odoo` role already had
        # superuser from earlier history predating the apt-package-creates-
        # a-role behavior (the same class of "never provisioned from truly
        # fresh" gap already found for the role/database bootstrap
        # functions' own `:'var'`-substitution bug, see this function's own
        # docstring above). Idempotent either way: ALTER ROLE ... SUPERUSER
        # on an already-superuser role is a harmless no-op.
        run_cmd_func(
            ["sudo", "-u", "postgres", "psql", "-c", "ALTER ROLE odoo WITH SUPERUSER;"]
        )
        return
    run_cmd_func(
        [
            "sudo", "-u", "postgres", "psql",
            "-v", f"db_pass={db_pass}",
        ],
        input="CREATE ROLE odoo WITH SUPERUSER LOGIN PASSWORD :'db_pass';\n",
        text=True,
    )


# [@ANCHOR: infrastructure:_read_env_value]
def _read_env_value(path, key):
    """The value of `key=` in a KEY=VALUE file, or "" when the file or the key is absent."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                name, sep, value = line.strip().partition("=")
                if sep and name.strip() == key:
                    return value.strip()
    except OSError:
        return ""
    return ""


# [@ANCHOR: infrastructure:_provision_cache_manager_role]
def _provision_cache_manager_role(
    run_cmd_func,
    db_name,
    role_name="cache_manager_ro",
    env_file="/opt/hams/etc/keys/cache_manager_db.env",
):
    """
    Creates (or rotates the password of) a dedicated, minimally-privileged PostgreSQL role for
    distributed_redis_cache/daemons/cache_manager.py, and writes its credentials to env_file.

    Mirrors distributed_redis_cache/scripts/provision_cache_manager_db_role.py's own role-creation
    SQL and env-file shape (see that script's own docstring for why a separate role and a separate
    env file: cache_manager.py only ever issues LISTEN and a constant-expression SELECT 1, so it
    needs no table privilege at all, and daemon_key_manager's own key_registry
    ._write_secure_env_file() truncates cache_manager.env on every module install/upgrade/key
    rotation, which would silently wipe DB_USER/DB_PASS if they were written there instead). That
    script remains the documented manual/remote-admin path (a Postgres host reachable only over
    TCP with a separately-held admin password); this function is the automated-provisioning path,
    using the same `sudo -u postgres psql` local peer-auth (and the same stdin-piped,
    `:'var'`/`:"var"`-substituted SQL) this module's other role/database bootstrap functions
    already use -- see _create_odoo_role_if_missing's own docstring for why the SQL must go over
    stdin, never `-c`, and _alter_database_owner_to_odoo's for the `:"var"` identifier form. No
    admin password of its own is needed.

    Generates a password only when it has to (2026-10-03: it used to rotate on every provisioning
    run). When env_file already records a DB_PASS and the role exists, that password is kept and
    no ALTER ROLE runs. When the role exists but nothing on disk records its password (a lost or
    never-written env_file: a re-provisioned box, or a run predating this function), the role is
    useless to the daemon, so it is rotated via ALTER ROLE, mirroring
    provision_cache_manager_db_role.py's own "already exists -- rotating its password" branch --
    night_shift_todo/low/cache-manager-odoo-password-fallback-0ea55461.md. A missing role is
    created with the recorded password if there is one, else a fresh one. env_file is rewritten
    only when its content would change. This does not detect a role whose password was changed by
    hand to something env_file does not record; delete env_file to force a rotation.
    """
    existing_password = _read_env_value(env_file, "DB_PASS")
    role_exists = _role_exists(role_name)
    password = existing_password or generate_secure_password()
    if not (role_exists and existing_password):
        verb = "ALTER" if role_exists else "CREATE"
        run_cmd_func(
            [
                "sudo", "-u", "postgres", "psql",
                "-v", f"role_name={role_name}",
                "-v", f"db_pass={password}",
            ],
            input=(
                f'{verb} ROLE :"role_name" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                "NOREPLICATION PASSWORD :'db_pass';\n"
            ),
            text=True,
        )
    run_cmd_func(
        [
            "sudo", "-u", "postgres", "psql",
            "-v", f"role_name={role_name}",
            "-v", f"db_name={db_name}",
        ],
        input='GRANT CONNECT ON DATABASE :"db_name" TO :"role_name";\n',
        text=True,
    )
    content = (
        "# Auto-generated by infrastructure.py's own provisioning flow.\n"
        "# Restricted Postgres role for cache_manager.py: CONNECT-only, no table access.\n"
        "DB_HOST=localhost\n"
        f"DB_NAME={db_name}\n"
        f"DB_USER={role_name}\n"
        f"DB_PASS={password}\n"
    )
    state = _file_state(env_file, content)
    if state != "unchanged":
        if _plan("write", f"{env_file} ({state}; keys DB_HOST DB_NAME DB_USER DB_PASS)"):
            return
        directory = os.path.dirname(env_file)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
    elif _planning():
        return
    # Unlike cache_manager.env (loaded via the systemd unit's own
    # EnvironmentFile=, read by systemd as root before it drops privileges
    # to User=odoo), this file is read directly by cache_manager.py's own
    # Python code (load_dotenv) running as the odoo OS user -- root:root
    # (write_env_files()'s own convention for systemd-loaded files) would
    # leave the daemon unable to read its own credentials. This function
    # itself typically runs as root (infrastructure.py's own provisioning
    # flow), so the file exists as root:root immediately after os.open()
    # above until this chown/chmod actually runs.
    apply_permissions(env_file, "odoo:odoo", 0o600)


# [@ANCHOR: infrastructure:_bridge_api_key_matches_odoo]
def _bridge_api_key_matches_odoo(db_name, api_key):
    """True when db_name's ir_config_parameter ham_relay_bridge.api_key equals api_key, False when it
    is missing or different. A read-only probe (it also runs under --plan); the comparison happens
    in SQL, so the stored value never leaves PostgreSQL, and api_key travels as a psql variable over
    the argument list, never in the SQL text. Raises RuntimeError when the query itself fails."""
    res = subprocess.run(
        [
            "sudo", "-u", "postgres", "psql",
            "-d", db_name,
            # terse: an error report carries no "LINE n:" excerpt of the interpolated query.
            "-v", "VERBOSITY=terse",
            "-v", f"bridge_api_key={api_key}",
            "-tA",
        ],
        input=(
            "SELECT count(*) FROM ir_config_parameter "
            "WHERE key = 'ham_relay_bridge.api_key' AND value = :'bridge_api_key';\n"
        ),
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        first_line = (res.stderr.strip().splitlines() or ["no error output"])[0]
        raise RuntimeError(f"could not read ir_config_parameter from {db_name}: {first_line}")
    return res.stdout.strip() == "1"


# [@ANCHOR: infrastructure:_sync_bridge_api_key_to_odoo]
def _sync_bridge_api_key_to_odoo(run_cmd_func, db_name, api_key):
    """
    Makes Odoo's ham_relay_bridge.api_key equal BRIDGE_API_KEY (bridge.env), the key
    hams_relay_bridge sends with every call to Odoo. Before 2026-10-03 nothing in provisioning
    wrote either side, and on hams1 both were empty: Odoo rejected every relay uplink
    (night_shift_todo/high/production-relay-bridge-api-key-never-provisioned-relays-cannot-uplink-5c1e9a27.md).

    bridge.env is the source of truth (load_and_prompt_env generates it when absent or empty, and
    write_env_files persists it at 0400 root:root); the database follows it. Odoo has no code path
    that reads secrets from its environment, and ham_relay_bridge reads this key with get_param()
    in a dozen places, so the provisioning run writes the parameter, the same way the coordinator
    fixed hams1 by hand. It runs after initialize_odoo_database() and before the smoketest starts
    Odoo again: get_param() is cached per process, so a direct write is only safe while Odoo is
    stopped. The write happens only when the stored value is missing or differs; it goes over psql
    stdin with the key as a `-v` variable, which redact_command() masks in logs and plan output.

    A failure to read back the expected value is recorded as a hook failure, which makes a
    production run exit 2 (print_hook_failure_summary).
    """
    if not api_key:
        record_hook_failure("bridge_api_key", RuntimeError("BRIDGE_API_KEY is empty; bridge.env was not provisioned"))
        return
    if not _database_exists(db_name):
        if not _plan("odoo", f"set ham_relay_bridge.api_key in {db_name} (database does not exist yet)"):
            record_hook_failure("bridge_api_key", RuntimeError(f"database {db_name} does not exist"))
        return
    try:
        if _bridge_api_key_matches_odoo(db_name, api_key):
            return
        if _plan("odoo", f"set ham_relay_bridge.api_key in {db_name} (missing or different from bridge.env)"):
            return
        run_cmd_func(
            [
                "sudo", "-u", "postgres", "psql",
                "-d", db_name,
                "-v", "ON_ERROR_STOP=1",
                "-v", "VERBOSITY=terse",
                "-v", f"bridge_api_key={api_key}",
            ],
            input=(
                "INSERT INTO ir_config_parameter (key, value, create_date, write_date) "
                "VALUES ('ham_relay_bridge.api_key', :'bridge_api_key', now() at time zone 'UTC', "
                "now() at time zone 'UTC') "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = EXCLUDED.write_date;\n"
            ),
            text=True,
        )
        if not _bridge_api_key_matches_odoo(db_name, api_key):
            raise RuntimeError(f"ham_relay_bridge.api_key in {db_name} still differs from bridge.env after writing it")
    except subprocess.CalledProcessError as e:
        # str(e) would quote the command line, key included.
        record_hook_failure(
            "bridge_api_key", RuntimeError(f"psql exited {e.returncode} writing ham_relay_bridge.api_key")
        )
    except (RuntimeError, OSError) as e:
        record_hook_failure("bridge_api_key", e)


# [@ANCHOR: infrastructure:_database_exists]
def _database_exists(db_name):
    """
    Checks (via a real, non-shell psql invocation) whether a PostgreSQL
    database named db_name exists, without ever splicing db_name into a SQL
    string or a shell command line -- see _create_odoo_role_if_missing's own
    docstring for why that matters, and for the real `-c`/`-tAc` bug this
    now avoids by piping the SQL over stdin instead of passing it as a
    `-tAc` argument (psql's `:'variable'` substitution does not apply at
    all in `-c`/`-tAc` mode, confirmed directly, only when SQL is read as a
    script over stdin or `-f`).
    """
    res = subprocess.run(
        [
            "sudo", "-u", "postgres", "psql",
            "-v", f"db_name={db_name}",
            "-tA",
        ],
        input="SELECT 1 FROM pg_database WHERE datname = :'db_name';\n",
        capture_output=True,
        text=True,
    )
    return res.returncode == 0 and res.stdout.strip() == "1"


# [@ANCHOR: infrastructure:_create_database_if_missing]
def _create_database_if_missing(run_cmd_func, db_name):
    if not _database_exists(db_name):
        run_cmd_func(["sudo", "-u", "postgres", "createdb", "-O", "odoo", db_name])


# [@ANCHOR: infrastructure:_get_installed_module_names]
def _get_installed_module_names(db_name):
    """
    Returns the set of module technical names currently in Odoo's own
    'installed' state in db_name, read directly from ir_module_module via
    the same non-shell, stdin-piped psql pattern _database_exists uses
    (see its own docstring for why -c/-tAc's :'var' substitution is
    avoided). This is what initialize_odoo_database() below uses to decide
    which already-installed modules need `-u` (upgrade -- picks up new
    fields/models/data files on a module that's already there) instead of
    `-i` (install -- a no-op for a module Odoo already considers
    'installed', so it would silently never apply a later change to an
    already-shipped module against a database that already has it).

    On a genuinely fresh database (ir_module_module doesn't exist yet, or
    the database itself doesn't exist), returns an empty set -- correctly
    treating every discovered module as needing `-i`, matching this
    function's only prior behavior before this distinction existed. Any
    other failure to read the list raises RuntimeError.
    """
    if not _database_exists(db_name):
        return set()
    res = subprocess.run(
        [
            "sudo", "-u", "postgres", "psql",
            "-d", db_name,
            "-tA",
        ],
        input="SELECT name FROM ir_module_module WHERE state = 'installed';\n",
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        # A fresh database has no ir_module_module table at all yet (the
        # query itself fails with "relation does not exist") -- that's
        # exactly the "nothing is installed" case, not an error worth
        # surfacing; anything else is logged so it isn't silently masked.
        if "does not exist" in res.stderr:
            return set()
        # Anything else means the database exists but its installed-module list could not be read (a transient
        # connection error, a permissions problem). Returning an empty set here would send every module to `-i`,
        # which Odoo treats as a no-op for an already-installed module, so the run would report success having
        # applied none of its updates. Stop instead.
        raise RuntimeError(
            f"Could not read ir_module_module from {db_name} ({res.stderr.strip()}); refusing to guess which "
            "modules are installed, because an empty answer would turn every module update into a silent no-op."
        )
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


# [@ANCHOR: infrastructure:_split_modules_by_install_state]
# Verified by [@ANCHOR: test_split_modules_by_install_state]
def _split_modules_by_install_state(modules, installed_modules):
    """
    Pure split of `modules` (every custom module directory discovered on
    disk) into (to_install, to_update) using `installed_modules` (the set
    already in Odoo's 'installed' state, from _get_installed_module_names)
    -- a module not yet installed needs `-i`; one that already is needs
    `-u` to actually pick up anything new about it (see
    initialize_odoo_database's own comment on this split for the full
    reasoning). Kept as its own small, pure function, deliberately not
    inlined into initialize_odoo_database, so it can be unit-tested
    directly without mocking that function's own much larger,
    host-dependent scope (odoo.conf rewriting, systemctl, subprocess) --
    matching this file's own established split between "genuinely
    destructive/host-dependent, not unit-tested" and "smaller,
    self-contained, real logic" (see this module's own test file docstring).
    """
    return (
        sorted(m for m in modules if m not in installed_modules),
        sorted(m for m in modules if m in installed_modules),
    )


# [@ANCHOR: infrastructure:_alter_database_owner_to_odoo]
def _alter_database_owner_to_odoo(run_cmd_func, db_name):
    """
    Alters db_name's owner to `odoo`, using psql's `:"db_name"` identifier-
    quoting syntax (quote_ident semantics) rather than splicing db_name
    directly into `ALTER DATABASE {db_name} OWNER TO odoo;` -- the prior
    version of this call also ran through `bash -c`, so an unescaped
    db_name containing a single quote or shell metacharacter was a combined
    shell-injection *and* SQL-injection vector, both closed by removing the
    shell entirely and letting psql's own variable substitution handle
    quoting.

    The SQL is piped over stdin (`input=`) rather than passed via `-c` --
    real bug found and fixed 2026-09-13 hardware-qualifying pi500-1: psql's
    `:'variable'`/`:"variable"` substitution does not apply at all when SQL
    is passed via `-c`, confirmed directly (see _create_odoo_role_if_missing's
    own docstring for the full investigation) -- only when it's read as a
    script over stdin or `-f`. This call had the identical never-actually-
    worked-on-a-fresh-box bug as the others.
    """
    run_cmd_func(
        [
            "sudo", "-u", "postgres", "psql",
            "-v", f"db_name={db_name}",
        ],
        input='ALTER DATABASE :"db_name" OWNER TO odoo;\n',
        text=True,
    )


# [@ANCHOR: infrastructure:_rabbitmq_user_exists]
def _rabbitmq_user_exists(user):
    """
    Checks (via `rabbitmqctl list_users --formatter json`) whether a
    RabbitMQ user named `user` already exists.
    """
    res = subprocess.run(
        ["rabbitmqctl", "list_users", "--formatter", "json"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return False
    try:
        users = json.loads(res.stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    return any(entry.get("user") == user for entry in users)


# [@ANCHOR: infrastructure:_create_rabbitmq_user_if_missing]
def _create_rabbitmq_user_if_missing(run_cmd_func, rmq_user, rmq_pass):
    """
    Creates (or, if it already exists, updates the password and re-grants
    permissions for) a real RabbitMQ user matching RMQ_USER/RMQ_PASS, and
    grants it full permissions on the default `/` vhost.

    Real, previously-undiscovered gap found 2026-09-13 hardware-qualifying
    pi500-1 (a genuinely fresh Raspberry Pi 500): this project's own
    `daemons/adif_processor/main.py` refuses to start with RabbitMQ
    credentials of literally "guest" for either RMQ_USER or RMQ_PASS
    (`_require_rabbitmq_credentials`, a deliberate security check --
    RabbitMQ's own well-known factory-default account), but
    `load_and_prompt_env`'s own non-test defaults set `RMQ_USER` to the
    literal string "guest" (`env_vars.setdefault("RMQ_USER", "guest")`),
    and NO code anywhere in this file ever actually created a real RabbitMQ
    user account for whatever RMQ_USER/RMQ_PASS ended up in env_vars --
    provisioning wrote real env files but never provisioned RabbitMQ itself
    to match. The result: `adif.processor.service` failed to start on a
    genuinely fresh box with "RMQ_USER and RMQ_PASS must both be set to
    real credentials -- refusing to fall back to the well-known
    'guest'/'guest' default." Confirmed this was never actually working
    anywhere, not just on this Pi: the dev box's own
    `/opt/hams/etc/rabbitmq.env` has RMQ_USER=guest, RMQ_PASS=guest too, and
    `systemctl status adif.processor.service` there reports
    `inactive (dead)` -- this service has never successfully run on the
    dev box either, simply because nobody had tried starting it fresh
    before this qualification pass surfaced it.

    Fixed in two parts: `load_and_prompt_env` (see its own comment) now
    defaults RMQ_USER to a real, fixed, non-"guest" service-account name
    instead of "guest" (mirroring DB_USER's own fixed "odoo" default --
    RabbitMQ usernames don't need generated entropy the way passwords do),
    and this new function actually provisions that account against the
    real, running RabbitMQ server -- `add_user` if it doesn't exist yet,
    `change_password` if it does (idempotent either way, matching
    `_create_odoo_role_if_missing`'s own pattern), then
    `set_permissions` granting it full access on the default `/` vhost
    (this daemon's own connection string uses no other vhost).
    """
    if rmq_user == "guest":
        raise RuntimeError(
            "_create_rabbitmq_user_if_missing refuses to provision a RabbitMQ "
            "account literally named 'guest' -- that defeats the entire point "
            "of this function (see its own docstring for the real bug this "
            "closes). Fix the caller's RMQ_USER default/config instead of "
            "relaxing this check."
        )
    if _rabbitmq_user_exists(rmq_user):
        run_cmd_func(["rabbitmqctl", "change_password", rmq_user, rmq_pass])
    else:
        run_cmd_func(["rabbitmqctl", "add_user", rmq_user, rmq_pass])
    run_cmd_func(["rabbitmqctl", "set_permissions", "-p", "/", rmq_user, ".*", ".*", ".*"])


# [@ANCHOR: infrastructure:_delete_rabbitmq_guest_user_if_present]
def _delete_rabbitmq_guest_user_if_present(run_cmd_func):
    """
    Deletes RabbitMQ's factory-default `guest` account on a production broker.

    RabbitMQ creates `guest` (administrator, full rights on `/`) on first boot, and nothing in this
    file ever removed it -- _create_rabbitmq_user_if_missing only ADDS the real service account.
    Found live on hams1, 2026-10-03: `guest` still existed, Odoo's hams_rabbitmq pool was silently
    using it (no rabbitmq.* parameters in hams_prod, so it fell back to guest/guest), and a test
    run on that host used it to feed test jobs to production's backup.worker. Only called for a
    production (not test) provisioning, after the real account has been provisioned, because a
    test box's private broker has no other account.
    """
    if _rabbitmq_user_exists("guest"):
        run_cmd_func(["rabbitmqctl", "delete_user", "guest"])


# [@ANCHOR: infrastructure:_ensure_line_in_file]
def _ensure_line_in_file(path, line):
    """Appends `line` to `path` (creating it) unless an identical line is already there. Returns True
    when it appended. Replaces a bare append that added another NODE_IP_ADDRESS line to
    /etc/rabbitmq/rabbitmq-env.conf on every provisioning run (eleven of them on hams1 by 2026-10-03)."""
    text = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    if line in text.splitlines():
        return False
    if _plan("append", f"{path}: {line}"):
        return True
    with open(path, "a", encoding="utf-8") as f:
        f.write(("\n" if text and not text.endswith("\n") else "") + line + "\n")
    return True


# [@ANCHOR: infrastructure:redis_url]
def redis_url(env_vars):
    """The redis:// URL for hams_data_relay (Rust), which reads REDIS_URL and nothing else. Built
    from the same REDIS_* values every other client reads, so the two can never disagree."""
    username = urllib.parse.quote(env_vars["REDIS_USERNAME"], safe="")
    password = urllib.parse.quote(env_vars["REDIS_PASSWORD"], safe="")
    host = env_vars.get("REDIS_HOST", "redis")
    port = env_vars.get("REDIS_PORT", "6379")
    return f"redis://{username}:{password}@{host}:{port}/0"


REDIS_CONF_PATH = "/etc/redis/redis.conf"
REDIS_ACL_INCLUDE_PATH = "/etc/redis/hams-acl.conf"


# [@ANCHOR: infrastructure:_redis_acl_include_content]
def _redis_acl_include_content(username, password):
    """
    The Redis configuration fragment that closes production Redis to unauthenticated clients.

    Until 2026-10-03 production Redis (127.0.0.1:6379) had no password: any local process could
    read or flush it, and the hams1 test runner did flush db 0. Every real client runs as the
    `odoo` Unix user -- and so did that test runner -- so a Unix-socket-plus-group scheme would not
    have stopped it; a password delivered only through the root-only (0400) /opt/hams/etc/redis.env
    that systemd injects does. (A process running as `odoo` could still read another `odoo`
    process's /proc/<pid>/environ: this stops accidents, it is not a boundary against a hostile
    `odoo` process.)

    A named ACL user rather than `requirepass` because it allows a rollout without downtime: a
    redis-py client that sends AUTH while the server's `default` user is still `nopass` gets an
    error, so with `requirepass` clients and server would have to switch at the same instant. With
    a named user, the user is created first (no effect on anyone), clients move to it one by one,
    and only then is `default` turned off.

    The password is stored as its SHA-256 (`#<hex>`), so the plaintext lives only in redis.env.
    `user` directives in a config file cannot be combined with `aclfile`; nothing here uses one.
    """
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return (
        "# Managed by hams_shared/tools/infrastructure.py (_redis_acl_include_content). Do not edit.\n"
        "user default off resetpass resetkeys resetchannels -@all\n"
        f"user {username} on #{digest} ~* &* +@all\n"
    )


# [@ANCHOR: infrastructure:_write_redis_acl_include]
def _write_redis_acl_include(env_vars, conf_path=REDIS_CONF_PATH, include_path=REDIS_ACL_INCLUDE_PATH):
    """Writes the ACL fragment (redis:redis, 0640) and makes redis.conf include it. Production only:
    test.py's private Redis starts from the host's own redis.conf, so a test box must not get it.

    Returns True when the fragment's content or the include line changed, i.e. when redis-server
    needs a restart to apply it. Production Redis runs with `appendonly no`, so an unneeded restart
    drops the whole cache (round-2 production runbook, 2026-10-03)."""
    username = env_vars.get("REDIS_USERNAME", "")
    password = env_vars.get("REDIS_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("REDIS_USERNAME and REDIS_PASSWORD must both be set to lock down production Redis.")
    if username == "default":
        raise RuntimeError("REDIS_USERNAME must not be 'default': that is the user this lock-down turns off.")
    content = _redis_acl_include_content(username, password)
    state = _file_state(include_path, content)
    if state != "unchanged" and not _plan("write", f"{include_path} ({state}; Redis ACL users)"):
        fd = os.open(include_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        os.fchmod(fd, 0o640)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
    if not _planning():
        apply_permissions(include_path, "redis:redis", 0o640)
    appended = _ensure_line_in_file(conf_path, f"include {include_path}")
    return state != "unchanged" or appended


# [@ANCHOR: infrastructure:_refuse_if_unsafe_test_db_drop]
def _refuse_if_unsafe_test_db_drop(db_name):
    """
    Refuses to let test-mode provisioning drop a database named "hams_prod"
    -- load_and_prompt_env's own well-known non-test DB_NAME default.

    load_and_prompt_env() reads every *.env file already under
    /opt/hams/etc UNCONDITIONALLY (regardless of is_test) and populates
    env_vars via `setdefault`, before is_test's own
    `env_vars.setdefault("DB_NAME", "hams_test")` ever runs. `setdefault`
    only takes effect when the key is still unset -- so on a shared box
    that was ever provisioned non-test (or that inherited a stale db.env
    left over from one), DB_NAME already carries whatever non-test value
    was persisted there, and a later is_test=True provisioning run silently
    inherits it instead of getting the intended fresh "hams_test" default.
    Without this guard, provision_environment's own test-mode branch would
    then run `dropdb --if-exists <db_name>` -- and this project's own
    conventions describe exactly this kind of shared, persistent dev box,
    not strictly separate per-environment machines.

    Since 2026-09-16 this is the second layer rather than the only one:
    _refuse_env_files_from_other_provision_mode stops a test run from reading
    env files a production run recorded. This guard still covers files saved
    before that mode marker existed, which carry no mode and are read as before.
    """
    if db_name == "hams_prod":
        raise RuntimeError(
            f"Refusing to drop database {db_name!r} during test-mode provisioning: "
            "this is load_and_prompt_env's own well-known production DB_NAME default, "
            "so it was very likely inherited from a stale /opt/hams/etc/db.env left "
            "over from a prior non-test provisioning run on this same box, not a "
            "database anyone actually intended to wipe. If a database genuinely named "
            "hams_prod needs to be used for a test run, set an explicit, different "
            "DB_NAME rather than relying on this default."
        )


# [@ANCHOR: infrastructure:_discover_hams_com_dir]
# Verified by [@ANCHOR: test_discover_hams_com_dir_disambiguates_from_hams_open]
def _discover_hams_com_dir(repo_root):
    """Finds hams_com's own directory relative to repo_root (hams_open's own
    root). Bug-hunt fix (2026-09-23, found live on hams1 immediately after
    fixing provision.py's own repo_root computation): every branch here used
    to check for a bare "daemons" subdirectory as the signal for "this path
    IS hams_com" -- but hams_open has its own, completely unrelated
    top-level daemons/ directory too, so once repo_root correctly pointed at
    hams_open's own root, the FIRST branch matched hams_open against itself
    and never fell through to the real, sibling hams_com directory at all --
    has_hams_com came back False, ham_com (and everything depending on it,
    including a real module upgrade a real release needed) never loaded, and
    the addons_path written to /etc/odoo/odoo.conf silently ended up with
    hams_open listed twice and hams_com missing entirely.
    ham_base/__manifest__.py is a real, specific signal only hams_com itself
    has (matching this function's own prior has_hams_com validation step,
    now folded directly into the discovery instead of only catching the
    wrong answer after the fact) -- checking it directly in every branch
    removes the ambiguity "daemons" had."""
    candidates = [
        repo_root,
        os.path.join(repo_root, "..", "hams_com"),
        os.path.join(repo_root, "..", "..", "hams_com"),
        "/hams_com",
    ]
    for candidate in candidates:
        if os.path.exists(os.path.join(candidate, "ham_base", "__manifest__.py")):
            return os.path.abspath(candidate)
    return None


# Packages an incidental dependency change must never upgrade or remove. On hams1 (2026-10-03) a
# plain `apt-get install -y <MANIFEST list>` would have upgraded odoo, pgbackrest and pgvector and
# replaced the cargo-web/rustc-web 1.96 toolchain with Debian's older cargo/rustc.
APT_PROTECTED_PREFIXES = (
    "odoo", "postgresql", "pgbackrest", "redis", "rabbitmq", "cargo", "rustc", "rust-", "libstd-rust",
)
# MANIFEST apt names that only provide the Rust toolchain: skipped when a `cargo` is already on PATH,
# whatever package (or rustup) provides it.
APT_RUST_TOOLCHAIN_NAMES = {"cargo", "cargo-web", "rustc", "rustc-web"}


# [@ANCHOR: infrastructure:_installed_apt_packages]
def _installed_apt_packages():
    """Names of the packages dpkg reports fully installed (read-only probe)."""
    res = subprocess.run(
        ["dpkg-query", "-W", "-f", "${db:Status-Abbrev} ${Package}\n"],
        capture_output=True, text=True,
    )
    names = set()
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("ii"):
            names.add(parts[1].split(":")[0])
    return names


# [@ANCHOR: infrastructure:_held_apt_packages]
def _held_apt_packages():
    """Names `apt-mark showhold` lists (read-only probe)."""
    res = subprocess.run(["apt-mark", "showhold"], capture_output=True, text=True)
    return {line.strip().split(":")[0] for line in res.stdout.splitlines() if line.strip()}


# [@ANCHOR: infrastructure:_filter_apt_install]
def _filter_apt_install(packages, installed, held, have_cargo):
    """Splits packages into (to_install, skipped): skipped maps a name to why it is left out.

    Provisioning installs only what is missing. An installed package is never re-requested (that
    is how a re-run upgraded odoo and friends), a held package is never named (apt-get -y aborts
    the whole command on a held package), and the Rust toolchain packages are skipped when cargo is
    already on PATH, so the installed rustc/cargo is never replaced."""
    to_install, skipped = [], {}
    for pkg in sorted(set(packages)):
        if pkg in held:
            skipped[pkg] = "held (apt-mark hold)"
        elif pkg in installed:
            skipped[pkg] = "already installed"
        elif pkg in APT_RUST_TOOLCHAIN_NAMES and have_cargo:
            skipped[pkg] = "a cargo toolchain is already installed"
        else:
            to_install.append(pkg)
    return to_install, skipped


# [@ANCHOR: infrastructure:_parse_apt_simulation]
def _parse_apt_simulation(output):
    """(installs, upgrades, removals) package names from `apt-get -s install` output."""
    installs, upgrades, removals = [], [], []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "Inst":
            (upgrades if len(parts) > 2 and parts[2].startswith("[") else installs).append(parts[1])
        elif parts[0] == "Remv":
            removals.append(parts[1])
    return installs, upgrades, removals


def _is_apt_protected(pkg):
    return pkg.startswith(APT_PROTECTED_PREFIXES)


# [@ANCHOR: infrastructure:_apt_install_missing]
def _apt_install_missing(run_cmd_func, packages, apt_opts, label):
    """Installs the packages from `packages` that are missing, never upgrading or removing anything
    that matters. Simulates first (`apt-get -s`, read-only) and refuses, recording a degraded step,
    when the install would remove any package or upgrade a protected one (APT_PROTECTED_PREFIXES),
    or when apt refuses because of a hold. The real install uses --no-upgrade. Returns the list
    installed (or, in plan mode, the list that would be)."""
    to_install, skipped = _filter_apt_install(
        packages, _installed_apt_packages(), _held_apt_packages(), shutil.which("cargo") is not None
    )
    for pkg, why in sorted(skipped.items()):
        if why != "already installed":
            _logger.info("[*] apt (%s): not installing %s: %s", label, pkg, why)
    if not to_install:
        _logger.info("[*] apt (%s): nothing to install.", label)
        return []
    sim = subprocess.run(
        ["apt-get", "-s", "install", "-y", "--no-upgrade"] + apt_opts + to_install,
        capture_output=True, text=True,
    )
    installs, upgrades, removals = _parse_apt_simulation(sim.stdout)
    protected = sorted(p for p in upgrades if _is_apt_protected(p))
    if sim.returncode != 0 or removals or protected:
        reason = (
            f"apt refused the simulation: {sim.stderr.strip()[-300:]}" if sim.returncode != 0
            else f"would remove {removals} / upgrade protected {protected}"
        )
        _logger.error("[!] apt (%s): NOT installing %s -- %s", label, " ".join(to_install), reason)
        if not _plan("refuse", f"apt install ({label}) {' '.join(to_install)}: {reason}"):
            record_hook_failure(f"apt_install:{label}", RuntimeError(reason))
        return []
    if upgrades:
        _logger.info("[*] apt (%s): dependencies to upgrade: %s", label, " ".join(upgrades))
    run_cmd_func(["apt-get", "install", "-y", "--no-upgrade"] + apt_opts + to_install)
    return to_install


def provision_environment(
    run_cmd_func,
    env_vars,
    orig_user,
    os_id=None,
    skip_apt=False,
    is_test=False,
    hold_odoo=False,
    plan=False,
    opt_in_units=(),
):
    """Provisions this host. plan=True (provision.py --plan) changes nothing: every action is
    recorded and printed instead (see ProvisionPlan); run_cmd_func must then only print, as
    provision.py's plan-mode run_sys does. opt_in_units names MANIFEST "opt_in" units the operator
    wants enabled (provision.py --enable-opt-in)."""
    if plan and not _planning():
        with planning() as recorded:
            provision_environment(
                run_cmd_func, env_vars, orig_user, os_id=os_id, skip_apt=skip_apt,
                is_test=is_test, hold_odoo=hold_odoo, plan=True, opt_in_units=opt_in_units,
            )
        print(f"PLAN ONLY: {len(recorded.actions)} action(s) listed above; nothing was changed.", flush=True)
        return recorded
    _logger.info("[*] Provision version 1")
    reset_hook_failures()
    os_id = os_id or get_os_identifier()
    repo_root = env_vars.get("REPO_ROOT", "/app")
    # Inject safe testing defaults for provisioning context so .env files populate
    for k, v in MANIFEST.get("env_defaults", {}).items():
        env_vars.setdefault(k, v)

    load_and_prompt_env(env_vars, is_test)

    hams_com_dir = _discover_hams_com_dir(repo_root)
    hams_community_dir = None
    has_hams_com = bool(hams_com_dir)

    # Bug-hunt fix (2026-10-02, found live on jetson-1's first-ever fresh
    # `provision.py --test` run against a checkout using the established
    # `hams_com/hams_shared -> ../hams_open/hams_shared` symlink convention):
    # a bare `hams_shared` existence check is ambiguous the same way the
    # bare "daemons" check already documented above for
    # `_discover_hams_com_dir` was -- hams_com ITSELF satisfies
    # `os.path.exists(os.path.join(repo_root, "hams_shared"))` via that
    # symlink even when repo_root is hams_com's own directory, not
    # hams_open's. That silently set `hams_community_dir = repo_root`
    # (hams_com again), so hams_open was never added to the addons_path at
    # all and any module depending on hams_open-only code (e.g. zero_sudo)
    # failed to install with "module ... depends on ... zero_sudo. But the
    # latter module is not available in your system." Each check now also
    # requires a real, hams_open-exclusive signal (zero_sudo's own
    # manifest) directly under the same candidate, removing the ambiguity.
    def _is_real_hams_open(path):
        return os.path.exists(os.path.join(path, "zero_sudo", "__manifest__.py"))

    if os.path.exists(os.path.join(repo_root, "hams_shared")) and _is_real_hams_open(
        repo_root
    ):
        hams_community_dir = repo_root
    elif os.path.exists(
        os.path.join(repo_root, "..", "hams_shared")
    ) and _is_real_hams_open(os.path.join(repo_root, "..")):
        hams_community_dir = os.path.abspath(os.path.join(repo_root, ".."))
    elif os.path.exists(
        os.path.join(repo_root, "..", "hams_community", "hams_shared")
    ) and _is_real_hams_open(os.path.join(repo_root, "..", "hams_community")):
        hams_community_dir = os.path.abspath(
            os.path.join(repo_root, "..", "hams_community")
        )
    elif os.path.exists(
        os.path.join(repo_root, "..", "..", "hams_community", "hams_shared")
    ) and _is_real_hams_open(os.path.join(repo_root, "..", "..", "hams_community")):
        hams_community_dir = os.path.abspath(
            os.path.join(repo_root, "..", "..", "hams_community")
        )
    elif os.path.exists(
        os.path.expanduser("~/workspace/hams_open/hams_shared")
    ) and _is_real_hams_open(os.path.expanduser("~/workspace/hams_open")):
        hams_community_dir = os.path.expanduser("~/workspace/hams_open")
    elif os.path.exists("/hams_community/hams_shared") and _is_real_hams_open(
        "/hams_community"
    ):
        hams_community_dir = "/hams_community"

    if not hams_com_dir:
        hams_com_dir = "/hams_com"
        _logger.warning(
            "[!] Primary repository hams_com not found. Cloning disabled due to headless auth constraints."
        )

    if not hams_community_dir:
        hams_community_dir = "/hams_community"
        _logger.info(
            "[*] Sibling repository not found. Cloning hams_community to %s...",
            hams_community_dir,
        )
        try:
            clone_env = dict(env_vars)
            clone_env["GIT_TERMINAL_PROMPT"] = "0"
            run_cmd_func(
                [
                    "git",
                    "clone",
                    "https://github.com/BrucePerens/hams_community",
                    hams_community_dir,
                ],
                env=clone_env,
            )
            if orig_user:
                try:
                    u_info = pwd.getpwnam(orig_user)
                    run_cmd_func(
                        [
                            "chown",
                            "-R",
                            f"{u_info.pw_uid}:{u_info.pw_gid}",
                            hams_community_dir,
                        ]
                    )
                except KeyError as e:  # burn-ignore-os-account-probe
                    _logger.debug("Original user %s not found: %s", orig_user, e)
        except subprocess.CalledProcessError as e:
            _logger.warning("[*] Failed to clone hams_community: %s", e)
            _logger.error(
                "[!] DIAGNOSTIC FOR AI: The sibling repository could not be cloned due to GitHub authentication restrictions in this headless VM."
            )
            _logger.error(
                "    If required modules are not present, tests will crash. Document this in hams_helpdesk/docs/KNOWN_TEST_ISSUES.md."
            )

    env_vars["HAMS_COM_DIR"] = hams_com_dir
    env_vars["HAMS_COMMUNITY_DIR"] = hams_community_dir

    try:
        with open("/etc/hosts", "r") as f:
            hosts_content = f.read()
        if "redis" not in hosts_content and not _plan(
            "append", "/etc/hosts: 127.0.0.1 redis rabbitmq postgres pdns memcached"
        ):
            _logger.info(
                "[*] Ensuring docker-compose hostnames resolve locally in /etc/hosts..."
            )
            with open("/etc/hosts", "a") as f:
                f.write("\n127.0.0.1 redis rabbitmq postgres pdns memcached\n")
    except OSError as e:
        _logger.warning("[*] Failed to update /etc/hosts: %s", e)

    _logger.info("[*] Initializing system accounts and static files...")
    try:
        provision_system_accounts(run_cmd_func, environment="prod")
        provision_system_accounts(run_cmd_func, environment="test")
        provision_static_files(run_cmd_func, env_vars, environment="early_prod")

        if not skip_apt:
            _logger.info("[*] Provisioning APT Sources and Packages...")
            apt_opts = [
                "-o",
                "Dpkg::Options::=--force-confdef",
                "-o",
                "Dpkg::Options::=--force-confold",
                "-o",
                "Dpkg::Lock::Timeout=120",
                "-o",
                "Acquire::Check-Valid-Until=false",
            ]

            run_cmd_func(["apt-get", "update"] + apt_opts)
            _apt_install_missing(run_cmd_func, ["gnupg"], apt_opts, "gnupg")
            run_cmd_func(
                ["apt-get", "update"] + apt_opts + ["--allow-insecure-repositories"]
            )

            all_packages = []

            for pkg_spec in MANIFEST.get("apt_packages", []):
                if "early_prod" in pkg_spec["environments"]:
                    pkg_name = (
                        pkg_spec.get("debian_name", pkg_spec["name"])
                        if os_id == "debian"
                        else pkg_spec["name"]
                    )
                    all_packages.append(pkg_name)

            pg_res = subprocess.run(
                [
                    "bash",
                    "-c",
                    "apt-cache depends postgresql | grep -Eo 'postgresql-[0-9]+' | head -n1 | grep -Eo '[0-9]+'",
                ],
                capture_output=True,
                text=True,
            )
            if pg_res.returncode == 0 and pg_res.stdout.strip():
                pg_major = pg_res.stdout.strip()
                all_packages.append(f"postgresql-{pg_major}-pgvector")

            # Real gap found live on jetson-1 (Ubuntu 24.04 "noble"), 2026-10-02:
            # apt-get install aborts the ENTIRE command -- installing nothing at all,
            # not just skipping the bad entry -- the moment any one package name has
            # no candidate in the configured repos. Ubuntu dropped the `awscli` apt
            # package starting with 24.04 (it now only ships via snap or pip). This
            # single package, needed only for daemons/ses_inbound_mail_ingest/main.py's
            # `aws` CLI shell-out (prod-only, no-op on a test box), was silently taking
            # down provisioning for every OTHER package in the same apt-get invocation.
            # Falls back to pip for exactly this one known case rather than building
            # generic any-package-might-be-missing handling for a problem only ever
            # seen here once.
            PIP_FALLBACK_APT_PACKAGES = {"awscli": "awscli"}
            missing_apt_packages = []
            for pkg in all_packages:
                probe = subprocess.run(
                    ["apt-cache", "show", pkg],
                    capture_output=True,
                    text=True,
                )
                if probe.returncode != 0 or not probe.stdout.strip():
                    missing_apt_packages.append(pkg)
            if missing_apt_packages:
                _logger.warning(
                    "[*] No apt candidate for: %s -- excluding from the apt-get install "
                    "so the rest of the package list still installs.",
                    ", ".join(missing_apt_packages),
                )
                all_packages = [p for p in all_packages if p not in missing_apt_packages]

            all_packages = sorted(list(set(all_packages)))
            _apt_install_missing(run_cmd_func, all_packages, apt_opts, "manifest")

            for pkg in missing_apt_packages:
                pip_name = PIP_FALLBACK_APT_PACKAGES.get(pkg)
                if pip_name:
                    _logger.info(
                        "[*] Installing %s via pip (no apt candidate on this OS release)",
                        pip_name,
                    )
                    run_cmd_func(["pip3", "install", "--break-system-packages", pip_name])
                else:
                    _logger.error(
                        "[!] %s has no apt candidate and no known pip fallback -- "
                        "left uninstalled. Anything that depends on it will fail "
                        "at runtime, not at provisioning time.",
                        pkg,
                    )

            _logger.info("[*] Installing pip packages...")
            run_cmd_func(
                [
                    "pip3",
                    "install",
                    "pgeocode",
                    "telnetlib3",
                    "mcp",
                    "adif-io",
                    # au_callsign_sync/main.py's `from playwright.sync_api
                    # import ...` is a real runtime dependency (the ACMA
                    # scraper), not test-only -- confirmed live on hams1,
                    # 2026-09-22: au.callsign.sync.service crashed with
                    # ModuleNotFoundError on every run, nothing anywhere
                    # installed this package. No Debian package exists
                    # (`apt-cache search python3-playwright` returns
                    # nothing), so pip is the only path, matching the other
                    # entries in this same list. The browser binary itself
                    # is NOT installed here -- the daemon's own
                    # scrape_acma_spa() already self-installs Chromium into
                    # /opt/hams/cache/ms-playwright on every run ("JIT
                    # Self-Healing", its own log line), idempotent and
                    # already the provisioned cache path; only the
                    # `playwright` package itself was ever missing.
                    "playwright",
                    # hams_config.py's smart_download() now imports curl_cffi
                    # unconditionally (this codebase's fail-fast policy forbids
                    # a try/except-guarded soft import for its optional
                    # `impersonate` parameter) -- real gap found live on
                    # hams1, 2026-09-22: fcc.uls.sync needed a real
                    # browser-TLS-fingerprint impersonation library to get
                    # past Akamai's bot detection (plain `requests`, and even
                    # real `curl` with matching headers, both still got 403).
                    # No Debian package exists for it, matching the other
                    # entries in this list.
                    "curl_cffi",
                    # daemons/relay_ca/cloudkms_shim.py build_client(): the three CA signer daemons on hams1
                    # (host class ca_signer) call Google Cloud KMS through this client. It is imported lazily and
                    # only there, so every other host merely carries an unused package. No Debian package
                    # exists (`apt-cache search google-cloud-kms` is empty), matching the entries around it.
                    "google-cloud-kms",
                    "--ignore-installed",
                    "typing_extensions",
                    "--break-system-packages",
                ]
            )
            # Remove pip's cryptography to prevent it from shadowing Debian's python3-cryptography, which breaks python3-openssl
            run_cmd_func(
                [
                    "pip3",
                    "uninstall",
                    "-y",
                    "cryptography",
                    "cffi",
                    "pycparser",
                    "--break-system-packages",
                ]
            )

            if is_test:
                # docs/proposals/CODE_REVIEW_PROCESS.md's own "Still not
                # formalized" note: hypothesis (ham_com's property-based
                # test suite) and pip-audit (the pip-side parallel to
                # cargo-deny/audit-check on the Rust side) were only ever
                # present on whichever box happened to get them installed
                # by hand -- invisible to a fresh checkout or CI runner.
                # Test-only tools, unlike pgeocode/adif-io/telnetlib3/mcp
                # above (real runtime deps this module needs to load), so
                # gated to is_test rather than added to the unconditional
                # block -- a prod box has no reason to carry either.
                # System-wide install, matching how the earlier
                # hypothesis-invisible-to-the-odoo-user bug (found the
                # night this note was written) was actually fixed --
                # --user installs are invisible to the odoo system user
                # test.py runs the Odoo process as.
                _logger.info("[*] Installing test-only pip packages (hypothesis, pip-audit)...")
                run_cmd_func(
                    [
                        "pip3",
                        "install",
                        "hypothesis",
                        "pip-audit",
                        "--ignore-installed",
                        "--break-system-packages",
                    ]
                )

                # Same "invisible to a fresh checkout" gap, found via
                # run_linters.py's own new daemons/ test-discovery step
                # (hams_shared/tools/run_linters.py step 29): four of
                # hams_com's daemons/ test suites import a package their
                # own daemon needs to run at all but that had only ever
                # been installed by hand on this box --
                # hams_local_relay/main.py (flask, flask-cors),
                # adif_ingress/main.py (aiohttp), and
                # event_sync/wa7bnm_contest_sync.py (feedparser). apt, not
                # pip, matching install_linux.sh's own dependency list for
                # the first two and avoiding this box's PEP 668
                # externally-managed-environment pip restriction
                # (confirmed directly: a bare `pip install feedparser`
                # here refuses without --break-system-packages).
                _logger.info(
                    "[*] Installing test-only apt packages for daemons/ test suites "
                    "(flask, flask-cors, aiohttp, feedparser)..."
                )
                _apt_install_missing(
                    run_cmd_func,
                    [
                        "python3-flask",
                        "python3-flask-cors",
                        "python3-aiohttp",
                        "python3-feedparser",
                    ],
                    apt_opts,
                    "test-only",
                )
        else:
            _logger.info("[*] Bypassing APT phase (skip_apt=True)...")

        provision_static_files(run_cmd_func, env_vars, environment="prod")
        provision_static_files(run_cmd_func, env_vars, environment="test")
        # Speech models for the bots: production-class runs only (see provision_model_files).
        provision_model_files(environment="test" if is_test else "prod")

        is_isolated_ns_early = os.environ.get("HAMS_ISOLATED_NS") == "1"
        if (
            not is_test
            and not is_isolated_ns_early
            and any(name == "hook_install_kopia_binary" for name, _ in get_hook_failures())
        ):
            # Per the to-do's own "middle ground" direction: kopia is the one
            # hook this run treats as FATAL rather than merely recorded, and
            # only on a real production run (never --test, never
            # HAMS_ISOLATED_NS=1, which is how test.py provisions for its own
            # test suite -- see its own `provision_environment(...,
            # is_test=True)` call). A box with no working backup tool is a
            # real, no-good silent gap in production; in test mode it's just
            # noise from a sandbox with no network access. This check reads
            # get_hook_failures() rather than raising from inside
            # hook_install_kopia_binary itself, so that hook's own
            # never-raises contract (pinned by a test in
            # HookInstallKopiaBinaryTests) stays intact regardless of which
            # environment it runs in.
            print_hook_failure_summary()
            _logger.error(
                "[!] FATAL: the kopia backup binary failed to install and this "
                "is a real production provisioning run -- refusing to report "
                "success with no working backup tool. See the summary above "
                "for the real cause."
            )
            sys.exit(3)

        provision_systemd_override(run_cmd_func, env_vars, environment="prod")
        provision_systemd_override(run_cmd_func, env_vars, environment="test")
        provision_firewall_rules(run_cmd_func, environment="test" if is_test else "prod")
        provision_systemd_override(
            run_cmd_func, env_vars, environment="prod",
            manifest_key="systemd_pdns_override", unit_name="pdns",
        )

        try:
            run_cmd_func(["usermod", "-a", "-G", "hams_com", "odoo"])
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to add odoo to hams_com group: %s", e)
            record_hook_failure("usermod_odoo_hams_com_group", e)

        # Bug found live on hams1, 2026-09-22: pdns.callbook.service reads its
        # config from /opt/hams/etc/pdns-callbook.conf (see the "directories"
        # manifest entry for that path), but /opt/hams itself is 0750
        # hams_com:hams_com -- without this, the pdns system account (created
        # by the pdns-server/pdns-backend-sqlite3 packages, never a member of
        # hams_com on its own) cannot even traverse into /opt/hams/etc to
        # read it, regardless of the file's own permissions. Found because
        # pdns.callbook.service had been crash-looping every ~5s since boot
        # ("Unable to open /opt/hams/etc/pdns-callbook.conf") on a box that
        # had never had provisioning re-run since this feature was added.
        try:
            run_cmd_func(["usermod", "-a", "-G", "hams_com", "pdns"])
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to add pdns to hams_com group: %s", e)
            record_hook_failure("usermod_pdns_hams_com_group", e)

        is_isolated_ns = os.environ.get("HAMS_ISOLATED_NS") == "1"
        is_test_env = is_isolated_ns or is_test

        _logger.info("[*] Preparing testing directories with production paths...")
        apply_production_directories(run_cmd_func, environment="prod")
        apply_production_directories(run_cmd_func, environment="test")

        # Bug found 2026-09-18: execute_hooks() was defined but had no tests
        # of its own and, worse, was never actually called from real
        # provisioning -- apply_production_directories() above creates the
        # directories in
        # MANIFEST["directories"] but never runs their post_provision_hooks,
        # so hook_generate_ssl (self-signed cert generation for
        # /opt/hams/nginx/ssl) and hook_clear_pycache
        # (/opt/hams/pycache) silently never ran during a real provisioning
        # run. Called here, right after the directories it hooks against are
        # actually created, matching provision_static_files()'s own
        # prod-then-test call pair immediately above and below in this
        # function. hook_generate_ssl already records its own failures via
        # record_hook_failure(); hook_clear_pycache's own failure modes are
        # already non-raising by construction (shutil.rmtree(ignore_errors=
        # True), safe_remove()'s internal OSError swallow), matching how
        # hook_install_odoo_key/hook_install_pg_key are likewise called
        # unwrapped from provision_static_files()'s own hook loop.
        _logger.info("[*] Running post-provision directory hooks...")
        execute_hooks("prod", run_cmd_func, env_vars)
        execute_hooks("test", run_cmd_func, env_vars)

        try:
            _logger.info("[*] Locking down RabbitMQ to local loopback...")
            if not _planning():
                os.makedirs("/etc/rabbitmq", exist_ok=True)
            rabbitmq_conf_changed = _ensure_line_in_file(
                "/etc/rabbitmq/rabbitmq-env.conf", "NODE_IP_ADDRESS=127.0.0.1"
            )
            if not is_isolated_ns:
                # Restart only when the bind address was just added: a restart drops every
                # consumer's connection (round-2 production runbook, 2026-10-03).
                if rabbitmq_conf_changed:
                    run_cmd_func(["systemctl", "restart", "rabbitmq-server"])
                # Real bug found and fixed 2026-09-13 hardware-qualifying pi500-1: nothing
                # here ever actually provisioned a real RabbitMQ user account matching
                # whatever RMQ_USER/RMQ_PASS ended up in env_vars/the written env files --
                # see _create_rabbitmq_user_if_missing's own docstring for the full
                # investigation (this daemon-startup failure was never caught on the dev
                # box either, since adif.processor.service had simply never been started
                # there before).
                rmq_user = env_vars.get("RMQ_USER", "")
                rmq_pass = env_vars.get("RMQ_PASS", "")
                if rmq_user and rmq_pass:
                    _create_rabbitmq_user_if_missing(run_cmd_func, rmq_user, rmq_pass)
                    # Production only, and only once the real account exists (a test box's
                    # private broker has no account but guest). See the function's docstring.
                    if not is_test:
                        _delete_rabbitmq_guest_user_if_present(run_cmd_func)
                else:
                    _logger.warning(
                        "[*] Skipping RabbitMQ user provisioning -- RMQ_USER/RMQ_PASS "
                        "not both present in env_vars."
                    )
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to configure RabbitMQ bindings: %s", e)
            record_hook_failure("rabbitmq_bindings", e)

        if not is_test:
            try:
                _logger.info("[*] Closing production Redis to unauthenticated clients (ACL user)...")
                redis_conf_changed = _write_redis_acl_include(env_vars)
                if not is_isolated_ns and redis_conf_changed:
                    run_cmd_func(["systemctl", "restart", "redis-server"])
            except Exception as e:  # audit-ignore-catch-all
                _logger.warning("[*] Failed to lock down Redis: %s", e)
                record_hook_failure("redis_acl", e)

        try:
            _logger.info("[*] Locking down PostgreSQL to local loopback...")
            postgresql_restart_needed = _apply_postgresql_lockdown()

            if not is_isolated_ns:
                # Only when a lockdown setting's effective value changed: restarting PostgreSQL
                # can crash odoo.service (hams_com CLAUDE.md, 2026-10-01).
                if postgresql_restart_needed:
                    run_cmd_func(["systemctl", "restart", "postgresql"])

                db_name = env_vars.get("DB_NAME", "hams_test")
                _logger.info(
                    "[*] Bootstrapping initial Odoo PostgreSQL role and database (%s)...", db_name
                )
                db_pass = env_vars.get("DB_PASS", "odoo")
                _create_odoo_role_if_missing(run_cmd_func, db_pass)

                if is_test:
                    _refuse_if_unsafe_test_db_drop(db_name)
                    run_cmd_func(["sudo", "-u", "postgres", "dropdb", "--if-exists", db_name])
                    run_cmd_func(["sudo", "-u", "postgres", "createdb", "-O", "odoo", db_name])
                else:
                    _create_database_if_missing(run_cmd_func, db_name)

                # Unconditionally ensure the database is owned by odoo to fix pre-existing DBs
                _alter_database_owner_to_odoo(run_cmd_func, db_name)

                _logger.info(
                    "[*] Provisioning cache_manager.py's own minimally-privileged "
                    "PostgreSQL role..."
                )
                _provision_cache_manager_role(run_cmd_func, db_name)
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to configure PostgreSQL settings: %s", e)
            record_hook_failure("postgresql_settings", e)

        _logger.info("[*] Writing environment configuration files...")
        write_env_files("/opt/hams/etc", env_vars, run_cmd_func)

        if orig_user:
            try:
                u_info = pwd.getpwnam(orig_user)
                user_tmp = os.path.join(u_info.pw_dir, "tmp")
                if not os.path.isdir(user_tmp) and _plan("mkdir", user_tmp):
                    pass
                elif not _planning():
                    os.makedirs(user_tmp, exist_ok=True)
                    apply_permissions(user_tmp, f"{orig_user}:{orig_user}", None)

            except KeyError as e:  # burn-ignore-os-account-probe
                _logger.debug("Original user %s not found: %s", orig_user, e)

        _logger.info("[*] Linking custom systemd units...")
        # .path units (first used 2026-10-01, hams-pgbackrest-backup.path) are an
        # [Install] WantedBy= activation unit exactly like a .timer -- they need the
        # same explicit `systemctl enable` below, not just the symlink, to actually
        # fire. Named linked_activation_units (was linked_timers) to reflect that.
        linked_activation_units = []
        # Long-running daemons are enabled at boot too (see boot_service_unit_names()), but only
        # by a production-style run: never in a test environment, and never in a prepare-only
        # --hold-odoo run, whose units must stay link-only until the real release run.
        boot_services = set() if (is_test_env or hold_odoo) else boot_service_unit_names()
        try:
            systemd_dir = "/opt/hams/systemd"
            if os.path.exists(systemd_dir):
                for item in os.listdir(systemd_dir):
                    if item.endswith((".service", ".timer", ".path")):
                        if not has_hams_com and item != "hams-pycache.service":
                            continue
                        src = os.path.join(systemd_dir, item)
                        dst = os.path.join("/etc/systemd/system", item)
                        if not os.path.exists(dst) and not _plan("link", f"{dst} -> {src}"):
                            os.symlink(src, dst)
                        if item.endswith((".timer", ".path")) or item in boot_services:
                            linked_activation_units.append(item)
            if _planning():
                # Units provision_static_files() would write but which are not on disk yet.
                for spec in MANIFEST.get("static_files", []):
                    unit_path = spec["path"]
                    item = os.path.basename(unit_path)
                    if (
                        os.path.dirname(unit_path) == systemd_dir
                        and item.endswith((".service", ".timer", ".path"))
                        and not os.path.exists(unit_path)
                        and (has_hams_com or item == "hams-pycache.service")
                        and set(spec.get("environments", [])) & {"prod", "test"}
                        and _in_host_class(spec)
                        and not os.path.exists(os.path.join("/etc/systemd/system", item))
                    ):
                        _plan("link", f"/etc/systemd/system/{item} -> {unit_path}")
                        if (
                            item.endswith((".timer", ".path")) or item in boot_services
                        ) and item not in linked_activation_units:
                            linked_activation_units.append(item)
        except OSError as e:
            _logger.warning("Failed to link systemd units: %s", e)
            record_hook_failure("systemd_unit_linking", e)

        # Bug fix, 2026-09-23 (night-watch): linking a unit into
        # /etc/systemd/system with a bare symlink leaves it in systemd's own
        # "linked" state, not "enabled" -- [Install] WantedBy=timers.target
        # is only acted on by an explicit `systemctl enable`, which this
        # provisioning step never issued. Confirmed live on hams1: ~18 of
        # ~20 hams.com-specific timers sat "linked (dead)", zero
        # timer-driven run history, since the box was first provisioned --
        # silently never syncing regulator callbook data, AMSAT TLE,
        # contest calendars, POTA/SOTA, or ingesting inbound support email,
        # until a night-watch session found and manually enabled them one
        # at a time days later. Plain `enable` (not `enable --now`)
        # deliberately: it wires each timer into timers.target.wants/ so it
        # fires at its own next OnCalendar tick going forward, without
        # forcing an immediate first run -- safe to re-run this same
        # provisioning step against an already-running system without an
        # unwanted stampede of first-ever executions across every daemon.
        #
        # A test environment (is_test_env: provision.py --test, or test.py's
        # own isolated provisioning) enables only the units that never fetch
        # from third-party servers -- see external_fetch_unit_names(). The
        # rest stay linked but disabled, so no timer ever fires them there.
        units_to_enable = _activation_units_to_enable(
            linked_activation_units, is_test_env, opt_in_units
        )
        held_back = sorted(set(linked_activation_units) - set(units_to_enable))
        if held_back:
            _logger.info(
                "[*] NOT enabling %d opt-in or (test environment) external-fetch unit(s): %s",
                len(held_back),
                ", ".join(held_back),
            )
        if units_to_enable:
            _logger.info(
                "[*] Enabling %d linked systemd timer/path/boot-service unit(s)...",
                len(units_to_enable),
            )
            try:
                if not _plan("run", "systemctl daemon-reload"):
                    subprocess.run(["systemctl", "daemon-reload"], check=False)
                for unit in units_to_enable:
                    if _planning():
                        state = subprocess.run(
                            ["systemctl", "is-enabled", unit], capture_output=True, text=True, check=False
                        ).stdout.strip()
                        if state != "enabled":
                            _plan("enable", f"{unit} (now: {state or 'not loaded'})")
                        continue
                    result = subprocess.run(
                        ["systemctl", "enable", unit],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if result.returncode != 0:
                        _logger.warning(
                            "Failed to enable %s: %s", unit, result.stderr.strip()
                        )
            except OSError as e:
                _logger.warning("Failed to enable systemd timers: %s", e)
                record_hook_failure("systemd_timer_enabling", e)

        if hold_odoo:
            # Prepare-only run (provision.py --hold-odoo): everything above (packages, accounts,
            # env files, directories, code, PostgreSQL role and empty database, RabbitMQ user,
            # systemd units linked but not enabled) is done, but Odoo is neither run nor
            # initialised. initialize_odoo_database() installs every custom module into the
            # database (a direct `odoo -i` command, so masking the odoo unit would not stop it)
            # and run_post_provision_smoketest() starts Odoo and every daemon. A later normal run
            # (without --hold-odoo) does both; this run is safe to repeat.
            _logger.warning(
                "[*] --hold-odoo: NOT initialising the Odoo database or starting Odoo and the "
                "daemons. Re-run provision.py without --hold-odoo to finish."
            )
        elif not is_isolated_ns and _planning():
            if has_hams_com:
                # Read-only under --plan: probes the database and records what it would set.
                _sync_bridge_api_key_to_odoo(
                    run_cmd_func,
                    env_vars.get("DB_NAME", "hams_test"),
                    env_vars.get("BRIDGE_API_KEY", ""),
                )
            _plan(
                "odoo",
                f"initialize_odoo_database on {env_vars.get('DB_NAME', 'hams_test')} (stops odoo, "
                "installs/upgrades modules, rewrites odoo.conf), sets ham_relay_bridge.api_key to "
                "match bridge.env when it differs, and run_post_provision_smoketest (starts every "
                "service)",
            )
        elif not is_isolated_ns:
            initialize_odoo_database(
                run_cmd_func,
                hams_community_dir,
                hams_com_dir,
                db_name=env_vars.get("DB_NAME", "hams_test"),
            )
            if has_hams_com:
                # ham_relay_bridge and hams_relay_bridge are hams_com code.
                _sync_bridge_api_key_to_odoo(
                    run_cmd_func,
                    env_vars.get("DB_NAME", "hams_test"),
                    env_vars.get("BRIDGE_API_KEY", ""),
                )
            run_post_provision_smoketest(
                has_hams_com, is_test_env=is_test_env, opt_in_units=opt_in_units
            )
        else:
            _logger.info(
                "[*] Skipping systemd smoketest inside isolated unshare namespace."
            )

        # End-of-run summary for the to-do's "middle ground": none of the
        # non-fatal failures recorded above aborted this run, but they must
        # not be silently missable either. Print the summary unconditionally
        # (so a human skimming output sees it in test mode too), but only
        # turn it into a non-zero exit code for a real production run --
        # test.py's own provisioning call (is_test=True, HAMS_ISOLATED_NS=1)
        # must keep exiting 0 on a clean run even if some host-dependent step
        # legitimately can't succeed in a sandbox, or provisioning-based
        # tests would start failing for reasons unrelated to what they test.
        if print_hook_failure_summary() and not is_test_env and not _planning():
            sys.exit(2)

    except subprocess.CalledProcessError as e:
        _logger.error("Failed to provision system packages: %s", e)
        sys.exit(1)
