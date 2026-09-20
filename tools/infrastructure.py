#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
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
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import secrets
import string
import base64
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


def apply_permissions(path, owner_str, mode_int):
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


def download_file(url, path, mode, env_vars):
    ua = env_vars.get(
        "SYSTEM_USER_AGENT",
        "Hams.com Bruce Perens K6BP <bruce@perens.com> +1 510-394-5627",
    )
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = response.read()
    except Exception as e:  # audit-ignore-catch-all
        _logger.warning("Network partition fallback safety hit fetching %s: %s", url, e)
        record_hook_failure(f"download_file:{url}", e)
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


def hook_daemons_perms(env_vars, dest_dir, path, run_cmd_func):
    target = path
    if os.path.exists(target):
        run_cmd_func(["chown", "-R", "hams_com:hams_com", target])
        run_cmd_func(["chmod", "-R", "a+rX", target])


# Rust-crate daemons under daemons/ with no Python entry point -- each of
# these needs a real compiled release binary at target/release/<crate_name>
# before its systemd unit's ExecStart can run. Runs before
# hook_daemons_perms in the same directory entry's hook list so the
# perms fixup below also covers the freshly built binaries.
RUST_DAEMON_CRATES = ["hams_data_relay", "hams_relay_bridge", "hams_simulated_band"]


def hook_build_rust_daemons(env_vars, dest_dir, path, run_cmd_func):
    for crate in RUST_DAEMON_CRATES:
        manifest_path = os.path.join(path, crate, "Cargo.toml")
        if os.path.exists(manifest_path):
            try:
                run_cmd_func(["cargo", "build", "--release", "--manifest-path", manifest_path])
            except Exception as e:  # audit-ignore-catch-all
                _logger.warning("Rust daemon build failed for %s: %s", crate, e)
                record_hook_failure(f"hook_build_rust_daemons:{crate}", e)


MANIFEST = {
    "system_accounts": [
        {
            "user": "hams_com",
            "group": "hams_com",
            "home": "/opt/hams",
            "shell": "/bin/bash",
            "add_to_users": ["odoo"],
            "environments": ["prod", "test"],
        }
    ],
    "directories": [
        {
            "path": "/opt/hams",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/etc",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/etc/keys",
            "owner": "odoo:odoo",
            "provision_mode": "700",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/etc/relay_cert_renew",
            "owner": "odoo:odoo",
            "provision_mode": "700",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
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
            "path": "/deploy/ssl",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["docker"],
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
            "path": "/opt/hams/cache",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/cache/ms-playwright",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
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
            "path": "/opt/hams/spool/ncvec",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
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
        "pdns.env": ["PDNS_API_KEY", "PDNS_API_URL"],
        "odoo.env": [
            "ODOO_ADMIN_PASSWORD",
            "ODOO_SERVICE_PASSWORD",
            "ODOO_URL",
            "CLOUDFLARE_API_TOKEN",
            "CLOUDFLARE_ZONE_ID",
        ],
        "rabbitmq.env": ["RMQ_PASS", "RABBITMQ_HOST", "RMQ_PORT", "RMQ_USER"],
        "redis.env": ["REDIS_HOST", "REDIS_PORT"],
        "bridge.env": ["BRIDGE_API_KEY"],
        "smtp.env": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS"],
        "core.env": [
            "DOMAIN",
            "SYSTEM_USER_AGENT",
            "SYSADMIN_EMAILS",
            "HAMS_CRYPTO_KEY",
            "CLOUDFLARE_TUNNEL_TOKEN",
            "PYTHONPYCACHEPREFIX",
            "WS_PORT",
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
            "path": "/opt/hams/etc/pdns_gsqlite3.conf",
            "content": """\
launch=gsqlite3
gsqlite3-database=/var/lib/powerdns/pdns.sqlite3
gsqlite3-dnssec=no
local-address=0.0.0.0
api=yes
api-key={PDNS_API_KEY}
webserver=yes
webserver-address=localhost
webserver-port=8081
webserver-allow-from=127.0.0.0/8,::1/128
dnsupdate=yes
allow-dnsupdate-from=127.0.0.0/8,::1/128
loglevel=6
""",
            "owner": "pdns:pdns",
            "mode": "640",
            "environments": ["prod"],
        },
        {
            "path": "/etc/hosts",
            "content": """\
127.0.0.1 localhost
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
Type=oneshot
User=odoo
Environment="ODOO_RC=/etc/odoo/odoo.conf"
Environment="HAMS_KEYS_DIR=/opt/hams/etc/keys"
EnvironmentFile=-/opt/hams/etc/odoo.env
EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
ExecStart=/bin/bash -c "DB=${DB_NAME}; if [ -z \\"$$DB\\" ]; then DB=hams_test; fi; echo \\"env['daemon.key.registry'].action_force_provision_all(); env.cr.commit()\\" | /usr/bin/python3 /usr/bin/odoo shell -c /etc/odoo/odoo.conf -d $$DB --no-http"
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
            "src": "{HAMS_COM_DIR}/daemons",
            "path": "/opt/hams/daemons",
            "owner": "hams_com:hams_com",
            "mode": "755",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_build_rust_daemons, hook_daemons_perms],
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
            "owner": "hams_com:hams_com",
            "mode": "755",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_daemons_perms],
        },
        {
            "path": "/opt/hams/systemd/system-startup.service",
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
ReadWritePaths=/opt/hams/spool/adif_queue
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/adif_processor

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
ReadWritePaths=/var/lib/odoo/backups /var/lib/odoo/backup_repo /var/backups/global /opt/hams/backup /opt/hams/etc/keys /mnt/backup /var/lib/pgbackrest /var/log/pgbackrest
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/dx_firehose

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="WS_PORT=8765"
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/ham_dx_daemon

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=dx_daemon_service"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/dx_daemon_service.key"
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/noaa_swpc_sync

# Odoo JSON2-RPC Credentials
EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=space_weather_service"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/space_weather_service.key"
Environment="POLL_INTERVAL=14400"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/noaa_swpc_sync/main.py --start-test

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/pdns_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=dns_api_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/dns_api_service_internal.key"
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/amsat_tle_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=satellite_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/satellite_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/amsat_tle_sync/main.py --start-test

# Execution via system Python
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
            "path": "/opt/hams/systemd/ses.inbound.mail.ingest.service",
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
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/ses_inbound_mail_ingest

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
# TODO(bruce): AWS credentials for this service account are not yet
# provisioned -- see docs/proposals/EMAIL_SEND_RECEIVE.md's "Also not
# yet a real, persistent home" section. This needs a real IAM identity
# for the `odoo` user (or an EnvironmentFile= line here sourcing one),
# not a copy of the dev box's personal SSO session. --start-test warns
# rather than failing until this is resolved.
Environment="ODOO_USER=mail_ingest_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/mail_ingest_service_internal.key"
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
            "path": "/opt/hams/systemd/relay.cert.renew.service",
            "content": """\
[Unit]
Description=Relay Wildcard TLS Cert Renewal Service
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
ReadWritePaths=/opt/hams/etc/relay_cert_renew
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/relay_cert_renew

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=relay_cert_renew_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/relay_cert_renew_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/relay_cert_renew/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/relay_cert_renew/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=relay.cert.renew
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/relay.cert.renew.timer",
            "content": """\
[Unit]
Description=Check Relay Wildcard Cert For Renewal Twice Daily

[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
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
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/gdpr_csv_export

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=gdpr_export_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/gdpr_export_service_internal.key"
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/qrz_scraper

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=onboarding_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/onboarding_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/qrz_scraper/main.py --start-test

# Execution via system Python
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
            "content": """\
[Unit]
Description=Ham Radio Australia ACMA Callsign Sync (One-Shot)
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/au_acma_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads /opt/hams/cache/ms-playwright
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/au_callsign_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/br_anatel_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/de_bnetza_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/nz_rsm_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/uk_ofcom_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
            "path": "/opt/hams/systemd/uk.ofcom.sync.timer",
            "content": """\
[Unit]
Description=Ham Radio UK Ofcom Callsign Sync Daily

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
            "path": "/opt/hams/systemd/wa7bnm.contest.sync.service",
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/event_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/event_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
            "path": "/opt/hams/systemd/rac.events.sync.service",
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/event_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/event_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=event_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/event_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
            "path": "/opt/hams/systemd/fcc.uls.sync.service",
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/fcc_uls_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/fcc_uls_sync/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fcc.uls.sync

[Install]
WantedBy=multi-user.target
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
ReadWritePaths=
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/ham_callbook_geo_enrich

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_geo_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_geo_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
            # Callbook DNS service, phase 1 (docs/proposals/CALLBOOK_DNS_SERVICE.md). Second
            # PowerDNS instance serving only callbook.<DOMAIN> from its own SQLite file. Loopback
            # only: nothing is public until phase 2 delegates the zone and picks the public
            # listener (the main pdns already holds port 53 on every address).
            "path": "/opt/hams/etc/pdns-callbook.conf",
            "content": """\
launch=gsqlite3
gsqlite3-database=/var/lib/powerdns/callbook/callbook.sqlite3
gsqlite3-dnssec=no
local-address=127.0.0.1
local-port=5301
api=yes
api-key={PDNS_API_KEY}
webserver=yes
webserver-address=127.0.0.1
webserver-port=8082
webserver-allow-from=127.0.0.0/8,::1/128
loglevel=4
""",
            "owner": "pdns:pdns",
            "mode": "640",
            "environments": ["prod"],
        },
        {
            "path": "/var/lib/powerdns/callbook",
            "owner": "pdns:pdns",
            "provision_mode": "775",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/pdns.callbook.service",
            "content": """\
[Unit]
Description=PowerDNS Authoritative Server, callbook zone (loopback only)
After=network.target

[Service]
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
Type=simple
User=pdns
Group=pdns
# Group-writable database files, so the exporter (SupplementaryGroups=pdns) can replace rows.
UMask=0002
RuntimeDirectory=pdns-callbook
ReadWritePaths=/var/lib/powerdns/callbook
ExecStart=/usr/sbin/pdns_server --daemon=no --guardian=no --config-dir=/opt/hams/etc --config-name=callbook --socket-dir=/run/pdns-callbook
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pdns.callbook

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/callbook.dns.export.service",
            "content": """\
[Unit]
Description=Ham Radio Callbook DNS Zone Exporter (One-Shot)
After=network.target pdns.callbook.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/var/lib/powerdns/callbook
Type=oneshot
User=odoo
SupplementaryGroups=pdns
UMask=0002
WorkingDirectory=/opt/hams/daemons/callbook_dns_export

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_dns_export_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_dns_export_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="CALLBOOK_DNS_DB=/var/lib/powerdns/callbook/callbook.sqlite3"
Environment="CALLBOOK_PDNS_API_URL=http://127.0.0.1:8082/api/v1/servers/localhost"

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
Description=Ham Radio Callbook DNS Zone Export, Nightly

[Timer]
OnCalendar=daily
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
            # Response rate limiting in front of the callbook PowerDNS instance. Loopback listener
            # until phase 2 chooses the public address (binding port 53 needs
            # CAP_NET_BIND_SERVICE, and the main pdns already owns 53 on every address).
            "path": "/opt/hams/systemd/callbook.dns.rrl.service",
            "content": """\
[Unit]
Description=Ham Radio Callbook DNS Response Rate Limiter
After=network.target pdns.callbook.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6
CapabilityBoundingSet=
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/callbook_dns_export
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="CALLBOOK_DNS_LISTEN=127.0.0.1:5300"
Environment="CALLBOOK_DNS_UPSTREAM=127.0.0.1:5301"

ExecStart=/usr/bin/python3 /opt/hams/daemons/callbook_dns_export/rrl_proxy.py

Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=callbook.dns.rrl

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/systemd/ised.canada.sync.service",
            "content": """\
[Unit]
Description=Ham Radio ISED Canada Callbook Sync Daemon
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/ised_canada_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/ised_canada_sync/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ised.canada.sync

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/ncvec.sync.service",
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/ncvec_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=ncvec_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/ncvec_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/ncvec_sync/main.py $DAEMON_ARGS

Restart=always
RestartSec=60
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ncvec.sync

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/pota.sync.service",
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/pota_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=activator_data_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/activator_data_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/sota_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=activator_data_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/activator_data_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/hams_com/docs/code_review_reports
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
            "path": "/opt/hams/systemd/aprs.is.sync.service",
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/aprs_is_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=aprs_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/aprs_sync_service_internal.key"
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/au_pii_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=callbook_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/callbook_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/au_pii_sync/main.py $DAEMON_ARGS

Restart=always
RestartSec=60
StandardOutput=journal
StandardError=journal
SyslogIdentifier=au.pii.sync

[Install]
WantedBy=multi-user.target
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
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/hams_simulated_band

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
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/cache/whisper
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/hams_simulated_bots

Environment="HF_HOME=/opt/hams/cache/whisper"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

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
        {"name": "nginx", "debian_name": "nginx", "environments": ["early_prod"]},
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
        {"name": "black", "debian_name": "black", "environments": ["early_prod"]},
        {
            "name": "python3-psutil",
            "debian_name": "python3-psutil",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-ephem",
            "debian_name": "python3-ephem",
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
    ],
    "env_defaults": {
        "DB_PORT": "5432",
        "RMQ_PORT": "5672",
        "REDIS_PORT": "6379",
        "WS_PORT": "8765",
        "RMQ_USER": "guest",
        "RMQ_PASS": "guest",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/hams/cache/ms-playwright",
    },
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
                "/var/log/odoo"
            ],
            "PrivateTmp": "true",
            "PrivateDevices": "true",
            "NoNewPrivileges": "true",
            "KillSignal": "SIGINT",
            "TimeoutStopSec": "15",
        },
    },
}


def scaffold_test_environment(args_db, provision_dirs=True):
    for k, v in MANIFEST["env_defaults"].items():
        os.environ.setdefault(k, v)

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
    ]


def provision_system_accounts(run_cmd_func, environment="prod", dest_dir=""):
    for acc in MANIFEST.get("system_accounts", []):
        if environment not in acc.get("environments", ["prod", "test"]):
            continue

        user = acc["user"]
        group = acc["group"]
        home = acc.get("home", "/opt/hams")
        shell = acc.get("shell", "/bin/bash")
        add_to_users = acc.get("add_to_users", [])

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
        if environment in d["environments"] and "post_provision_hooks" in d:
            for hook in d["post_provision_hooks"]:
                physical_path = (
                    os.path.join(dest_dir, d["path"].lstrip("/"))
                    if dest_dir
                    else d["path"]
                )
                hook(env_vars or {}, dest_dir, physical_path, run_cmd_func)


def apply_production_directories(run_cmd_func=None, environment="prod", dest_dir=""):
    for d in MANIFEST["directories"]:
        if environment in d["environments"]:
            path = (
                os.path.join(dest_dir, d["path"].lstrip("/")) if dest_dir else d["path"]
            )
            mode = int(d["provision_mode"], 8)
            os.makedirs(path, mode=mode, exist_ok=True)
            apply_permissions(path, d.get("owner"), mode)


# [@ANCHOR: infrastructure:write_env_files]
def write_env_files(base_etc_dir, env_vars, run_cmd_func, dest_dir=""):
    if dest_dir:
        base_etc_dir = os.path.join(dest_dir, base_etc_dir.lstrip("/"))
    os.makedirs(base_etc_dir, exist_ok=True)

    for filename, keys in MANIFEST["env_groups"].items():
        filepath = os.path.join(base_etc_dir, filename)
        content = "".join(f"{k}={env_vars[k]}\n" for k in keys if k in env_vars)

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


# [@ANCHOR: infrastructure:provision_static_files]
def provision_static_files(run_cmd_func, env_vars, environment="prod", dest_dir=""):
    for file_spec in MANIFEST.get("static_files", []):
        if environment not in file_spec["environments"]:
            continue

        condition_env = file_spec.get("condition_env")
        if condition_env and not env_vars.get(condition_env):
            continue

        path = format_env(file_spec["path"], env_vars)
        if dest_dir:
            path = os.path.join(dest_dir, path.lstrip("/"))

        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = int(file_spec.get("mode", "644"), 8)

        src = file_spec.get("src")
        url = file_spec.get("url")

        if src:
            src = format_env(src, env_vars)
            if os.path.exists(src):
                if os.path.isdir(src):
                    shutil.copytree(
                        src,
                        path,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("target", ".git", "__pycache__"),
                    )
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

        if "post_provision_hooks" in file_spec:
            for hook in file_spec["post_provision_hooks"]:
                hook(env_vars or {}, dest_dir, path, run_cmd_func)


# [@ANCHOR: infrastructure:provision_systemd_override]
def provision_systemd_override(run_cmd_func, env_vars, environment="prod", dest_dir=""):
    if environment not in ["prod", "test"]:
        return
    override_data = MANIFEST.get("systemd_odoo_override")
    if not override_data:
        return

    override_dir = (
        os.path.join(dest_dir, "etc/systemd/system/odoo.service.d".lstrip("/"))
        if dest_dir
        else "/etc/systemd/system/odoo.service.d"
    )
    os.makedirs(override_dir, exist_ok=True)
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


# [@ANCHOR: infrastructure:initialize_odoo_database]
def initialize_odoo_database(run_cmd_func, hams_open_dir, hams_com_dir):
    _logger.info("[*] Initializing Odoo database with custom modules...")
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

    mod_string = "base," + ",".join(modules)
    _logger.info("Initializing modules: %s", mod_string)

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
    except Exception as e: # audit-ignore-catch-all
        _logger.warning("Failed to update odoo.conf: %s", e)

    # Stop Odoo service to prevent database/port conflicts during initialization
    try:
        run_cmd_func(["sudo", "systemctl", "stop", "odoo.service"])
    except Exception as e:  # audit-ignore-catch-all: best-effort stop before init (e.g. service already stopped/not installed yet on a fresh box); logged and initialization proceeds regardless.
        _logger.warning("Failed to stop odoo.service before init: %s", e)

    cmd = [
        "sudo", "-u", "odoo", "odoo",
        "-c", "/etc/odoo/odoo.conf",
        "-d", "hams_test",
        "-i", mod_string,
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

def run_post_provision_smoketest(has_hams_com=True, is_test_env=False):
    _logger.info("[*] Running post-provisioning smoketest on all services...")

    try:
        subprocess.run(["systemctl", "daemon-reload"], check=False)
    except OSError as e:
        _logger.debug("Ignored OSError during daemon-reload: %s", e)

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

    for sf in MANIFEST.get("static_files", []):
        path = sf.get("path", "")
        if "systemd" in path and path.endswith(".service"):
            svc_name = os.path.basename(path)
            if not has_hams_com and svc_name != "hams-pycache.service":
                continue
            if not is_test_env and svc_name in daemons_to_skip:
                continue
            if svc_name not in potential_services and "@" not in svc_name:
                potential_services.append(svc_name)

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

        _logger.info("    Starting %s...", svc)
        res = subprocess.run(
            ["systemctl", "start", svc], capture_output=True, text=True
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

    if is_test_env:
        _logger.info("[*] All services started successfully. Shutting them down (--test mode)...")
        for svc in reversed(started_services):
            _logger.info("    Stopping %s...", svc)
            subprocess.run(["systemctl", "stop", svc], capture_output=True)
    else:
        _logger.info("[*] All services started successfully and are running.")

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
        env_vars.setdefault("ODOO_ADMIN_PASSWORD", "admin")
        env_vars.setdefault("ODOO_SERVICE_PASSWORD", "service")
        env_vars.setdefault("SMTP_HOST", "localhost")
        env_vars.setdefault("SMTP_PORT", "1025")
        env_vars.setdefault("HAMS_CRYPTO_KEY", "0000000000000000000000000000000000000000000=")
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
        env_vars.setdefault("WS_PORT", "8080")
        env_vars.setdefault("PYTHONPYCACHEPREFIX", "/tmp/pycache")
        env_vars.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/hams/playwright")
        env_vars.setdefault("SYSTEM_USER_AGENT", "HAMS/1.0")

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

        env_vars.setdefault("SMTP_HOST", "smtp.mailgun.org")
        env_vars.setdefault("SMTP_PORT", "587")
        env_vars.setdefault("SMTP_USER", f"postmaster@{domain}")
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


def _postgresql_lockdown_commands():
    """
    The shell commands provision_environment() runs to restrict PostgreSQL to
    loopback. pg_hba.conf is deliberately left as Debian ships it.

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
    return [
        [
            "bash",
            "-c",
            "echo \"listen_addresses = '127.0.0.1, ::1'\" >> /etc/postgresql/*/main/postgresql.conf",
        ],
        [
            "bash",
            "-c",
            "echo \"shared_preload_libraries = 'pg_stat_statements'\" >> /etc/postgresql/*/main/postgresql.conf",
        ],
    ]


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
        return
    run_cmd_func(
        [
            "sudo", "-u", "postgres", "psql",
            "-v", f"db_pass={db_pass}",
        ],
        input="CREATE ROLE odoo WITH SUPERUSER LOGIN PASSWORD :'db_pass';\n",
        text=True,
    )


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

    Always writes a fresh password and env_file, even when role_name already exists in PostgreSQL:
    the role can outlive a lost or never-written env_file (a re-provisioned box, or a provisioning
    run predating this function), and a role whose real password nothing on disk records is as
    useless to the daemon as no role at all. Rotates via ALTER ROLE in that case, mirroring
    provision_cache_manager_db_role.py's own provision()'s "already exists -- rotating its
    password" branch -- night_shift_todo/low/cache-manager-odoo-password-fallback-0ea55461.md.
    """
    password = generate_secure_password()
    verb = "ALTER" if _role_exists(role_name) else "CREATE"
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
    directory = os.path.dirname(env_file)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Auto-generated by infrastructure.py's own provisioning flow.\n")
        f.write("# Restricted Postgres role for cache_manager.py: CONNECT-only, no table access.\n")
        f.write("DB_HOST=localhost\n")
        f.write(f"DB_NAME={db_name}\n")
        f.write(f"DB_USER={role_name}\n")
        f.write(f"DB_PASS={password}\n")
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


def provision_environment(
    run_cmd_func, env_vars, orig_user, os_id=None, skip_apt=False, is_test=False
):
    _logger.info("[*] Provision version 1")
    reset_hook_failures()
    os_id = os_id or get_os_identifier()
    repo_root = env_vars.get("REPO_ROOT", "/app")
    # Inject safe testing defaults for provisioning context so .env files populate
    for k, v in MANIFEST.get("env_defaults", {}).items():
        env_vars.setdefault(k, v)

    load_and_prompt_env(env_vars, is_test)

    hams_com_dir = None
    hams_community_dir = None

    if os.path.exists(os.path.join(repo_root, "daemons")):
        hams_com_dir = repo_root
    elif os.path.exists(os.path.join(repo_root, "..", "hams_com", "daemons")):
        hams_com_dir = os.path.abspath(os.path.join(repo_root, "..", "hams_com"))
    elif os.path.exists(os.path.join(repo_root, "..", "..", "hams_com", "daemons")):
        hams_com_dir = os.path.abspath(os.path.join(repo_root, "..", "..", "hams_com"))
    elif os.path.exists("/hams_com/daemons"):
        hams_com_dir = "/hams_com"
        
    has_hams_com = False
    if hams_com_dir:
        has_hams_com = os.path.exists(os.path.join(hams_com_dir, "ham_base", "__manifest__.py"))

    if os.path.exists(os.path.join(repo_root, "hams_shared")):
        hams_community_dir = repo_root
    elif os.path.exists(os.path.join(repo_root, "..", "hams_shared")):
        hams_community_dir = os.path.abspath(os.path.join(repo_root, ".."))
    elif os.path.exists(os.path.join(repo_root, "..", "hams_community", "hams_shared")):
        hams_community_dir = os.path.abspath(
            os.path.join(repo_root, "..", "hams_community")
        )
    elif os.path.exists(os.path.join(repo_root, "..", "..", "hams_community", "hams_shared")):
        hams_community_dir = os.path.abspath(
            os.path.join(repo_root, "..", "..", "hams_community")
        )
    elif os.path.exists(os.path.expanduser("~/workspace/hams_open/hams_shared")):
        hams_community_dir = os.path.expanduser("~/workspace/hams_open")
    elif os.path.exists("/hams_community/hams_shared"):
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
        if "redis" not in hosts_content:
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
            run_cmd_func(["apt-get", "install", "-y"] + apt_opts + ["gnupg"])
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

            all_packages = sorted(list(set(all_packages)))
            run_cmd_func(["apt-get", "install", "-y"] + apt_opts + all_packages)

            _logger.info("[*] Installing pip packages...")
            run_cmd_func(
                [
                    "pip3",
                    "install",
                    "pgeocode",
                    "telnetlib3",
                    "mcp",
                    "adif-io",
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
                run_cmd_func(
                    ["apt-get", "install", "-y"]
                    + apt_opts
                    + [
                        "python3-flask",
                        "python3-flask-cors",
                        "python3-aiohttp",
                        "python3-feedparser",
                    ]
                )
        else:
            _logger.info("[*] Bypassing APT phase (skip_apt=True)...")

        provision_static_files(run_cmd_func, env_vars, environment="prod")
        provision_static_files(run_cmd_func, env_vars, environment="test")

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

        try:
            run_cmd_func(["usermod", "-a", "-G", "hams_com", "odoo"])
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to add odoo to hams_com group: %s", e)
            record_hook_failure("usermod_odoo_hams_com_group", e)

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
        # /opt/hams/nginx/ssl and /deploy/ssl) and hook_clear_pycache
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
            os.makedirs("/etc/rabbitmq", exist_ok=True)
            with open("/etc/rabbitmq/rabbitmq-env.conf", "a") as f:
                f.write("NODE_IP_ADDRESS=127.0.0.1\n")
            if not is_isolated_ns:
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
                else:
                    _logger.warning(
                        "[*] Skipping RabbitMQ user provisioning -- RMQ_USER/RMQ_PASS "
                        "not both present in env_vars."
                    )
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to configure RabbitMQ bindings: %s", e)
            record_hook_failure("rabbitmq_bindings", e)

        try:
            _logger.info("[*] Locking down PostgreSQL to local loopback...")
            for cmd in _postgresql_lockdown_commands():
                run_cmd_func(cmd)

            if not is_isolated_ns:
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
                os.makedirs(user_tmp, exist_ok=True)
                apply_permissions(user_tmp, f"{orig_user}:{orig_user}", None)

            except KeyError as e:  # burn-ignore-os-account-probe
                _logger.debug("Original user %s not found: %s", orig_user, e)

        _logger.info("[*] Linking custom systemd units...")
        try:
            systemd_dir = "/opt/hams/systemd"
            if os.path.exists(systemd_dir):
                for item in os.listdir(systemd_dir):
                    if item.endswith(".service") or item.endswith(".timer"):
                        if not has_hams_com and item != "hams-pycache.service":
                            continue
                        src = os.path.join(systemd_dir, item)
                        dst = os.path.join("/etc/systemd/system", item)
                        if not os.path.exists(dst):
                            os.symlink(src, dst)
        except OSError as e:
            _logger.warning("Failed to link systemd units: %s", e)
            record_hook_failure("systemd_unit_linking", e)

        if not is_isolated_ns:
            initialize_odoo_database(run_cmd_func, hams_community_dir, hams_com_dir)
            run_post_provision_smoketest(has_hams_com, is_test_env=is_test_env)
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
        if print_hook_failure_summary() and not is_test_env:
            sys.exit(2)

    except subprocess.CalledProcessError as e:
        _logger.error("Failed to provision system packages: %s", e)
        sys.exit(1)
