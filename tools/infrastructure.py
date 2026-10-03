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


def hook_create_pdns_sqlite_schema(env_vars, dest_dir, path, run_cmd_func):
    """Creates the main PowerDNS instance's gsqlite3 database (empty:
    zones are created via the API, not by this schema load) from the
    pdns-backend-sqlite3 package's own reference schema. Found missing
    live on hams1, 2026-09-22: nothing in this codebase ever created
    this file, so pdns.service could bind port 53 (no schema needed for
    that) but its API returned 404 for every zone -- there was no
    database for it to have created any in. Idempotent: skips if the
    file already exists, matching every other hook here."""
    db_path = os.path.join(path, "pdns.sqlite3")
    if os.path.exists(db_path):
        return
    schema = _PDNS_SQLITE_SCHEMA_PATH
    if not os.path.exists(schema):
        _logger.warning("pdns-backend-sqlite3's own schema file is missing: %s", schema)
        record_hook_failure(
            "hook_create_pdns_sqlite_schema",
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
        record_hook_failure("hook_create_pdns_sqlite_schema", e)


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
RUST_DAEMON_CRATES = [
    "hams_data_relay",
    "hams_relay_bridge",
    "hams_simulated_band",
    # hams_com daemons/hams_auth_gateway: the auth.hams.com certificate login gateway (LoTW
    # client certificates over TLS 1.2, TCP 443). See its README and hams_com
    # docs/deploy/LOTW_CA_CHAIN.md section 7.
    "hams_auth_gateway",
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


# [@ANCHOR: infrastructure:migrate_signing_key]
# hams_com's privilege-isolated signer daemons (daemons/relay_signer,
# daemons/subcarrier_signer, daemons/device_command_signer) each own one
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


def hook_migrate_device_command_signing_key(env_vars, dest_dir, path, run_cmd_func):
    _run_signing_key_migration(
        "hook_migrate_device_command_signing_key",
        "hams_device_command_signing_ed25519.key",
        "hams_device_command_signer:hams_device_command_signer",
        dest_dir,
        path,
    )


def hook_migrate_relay_signing_key(env_vars, dest_dir, path, run_cmd_func):
    # The relay key keeps its historical "noise" file name (daemons/
    # relay_signer/main.py's SIGNING_KEY_PATH), unlike the other two.
    _run_signing_key_migration(
        "hook_migrate_relay_signing_key",
        "hams_noise_signing_ed25519.key",
        "hams_relay_signer:hams_relay_signer",
        dest_dir,
        path,
    )


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
            # daemons/localhost_cert_renewal (ADR 0100): the dedicated account that owns the
            # shared localhost.hams.com certificate's private key. odoo joins its group so the
            # relay-bridge endpoint can read the served copy (files are 0640); nothing else can.
            "user": "localhost_cert",
            "group": "localhost_cert",
            "home": "/opt/hams/etc/localhost_cert_renewal",
            "shell": "/usr/sbin/nologin",
            "add_to_users": ["odoo"],
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
            # hams_com daemons/device_command_signer: same shape, for the
            # device_command_authority key (it signs device check-in responses, including
            # "wipe_now", so it is the most damaging of the signing keys to leak).
            "user": "hams_device_command_signer",
            "group": "hams_device_command_signer",
            "home": "/opt/hams/etc/device_command_signer",
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
            # never as root. Not a member of any other group, and no other account joins its
            # group. Prod-only: auth.hams.com exists only on the production server.
            "user": "hams-auth",
            "group": "hams-auth",
            "home": "/etc/hams/auth",
            "shell": "/usr/sbin/nologin",
            "environments": ["prod"],
        },
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
            # backup_management/daemon/main.py's _run_pgbackrest_via_sidecar():
            # the backup worker daemon's own unit (NoNewPrivileges=true, ProtectSystem=strict)
            # cannot perform a real pgbackrest backup itself -- PostgreSQL's own
            # data directory is 0700 postgres:postgres. This spool directory is
            # how it hands that one operation off to hams-pgbackrest-backup's
            # privileged, unsandboxed sidecar instead of granting itself
            # standing root/postgres filesystem access. odoo:odoo 700, same
            # shape as relay_cert_renew above: the sidecar runs as root before
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
            "path": "/opt/hams/etc/localhost_cert_renewal",
            "owner": "localhost_cert:localhost_cert",
            "provision_mode": "750",
            # The renewal daemon runs on the host as localhost_cert; the Odoo tier only reads the
            # served copy (ham_relay_bridge's shared_tls_cert_bundle route), so it never needs rw.
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
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
            # hams_com daemons/device_command_signer: same shape as subcarrier_signer above.
            "path": "/opt/hams/etc/device_command_signer",
            "owner": "hams_device_command_signer:hams_device_command_signer",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_migrate_device_command_signing_key],
        },
        {
            "path": "/opt/hams/etc/device_command_signer_public",
            "owner": "hams_device_command_signer:hams_device_command_signer",
            "provision_mode": "755",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            # hams_com daemons/relay_signer: same shape as subcarrier_signer above. The
            # hook moves /var/lib/odoo/hams_noise_signing_ed25519.key, if present.
            "path": "/opt/hams/etc/relay_signer",
            "owner": "hams_relay_signer:hams_relay_signer",
            "provision_mode": "700",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_migrate_relay_signing_key],
        },
        {
            "path": "/opt/hams/etc/relay_signer_public",
            "owner": "hams_relay_signer:hams_relay_signer",
            "provision_mode": "755",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        # /opt/hams/nginx and its self-signed ssl/ pair: nginx is NOT the
        # production front end. On hams1 nginx is installed but disabled
        # and was never in the request path; ingress is the Cloudflare
        # Tunnel plus hams_auth_gateway on auth.hams.com (hams_com
        # docs/proposals/PROVISION_PRODUCTION_NOTES.md, "Update
        # 2026-10-03"). Nothing on hams1 reads these files.
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
            "path": "/opt/hams/spool/adif_uploads",
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
            # pdns.callbook.service's own setup already created it.
            "path": "/var/lib/powerdns/callbook",
            "owner": "pdns:pdns",
            # setgid (leading 2): callbook_dns_export runs as User=odoo with
            # SupplementaryGroups=pdns, not User=pdns, so a file it creates
            # here only lands group=pdns if this directory's own group is
            # inherited -- setgid is what makes that automatic. Without it,
            # new files land group=odoo and pdns.callbook.service (running
            # as User=pdns Group=pdns, no supplementary groups) gets zero
            # access to its own database. Found live on hams1, 2026-09-22,
            # after fixing the group ownership by hand and still hitting
            # "attempt to write a readonly database" on every publish after
            # the first, once a WAL-mode connection had already been opened.
            "provision_mode": "2775",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
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
            "path": "/opt/hams/hamcall",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            # hams_ai_agent's home directory -- see this MANIFEST's own "system_accounts" entry
            # for the account itself. `provision_system_accounts()`'s `useradd` call deliberately
            # never passes `-m` (matches the existing convention for every other account here,
            # e.g. hams_com/localhost_cert above, which also get their home directories from a
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
            "path": "/opt/hams/etc/pdns-gsqlite3.conf",
            "content": """\
launch=gsqlite3
gsqlite3-database=/var/lib/powerdns/pdns.sqlite3
gsqlite3-dnssec=no
local-address=0.0.0.0
api=yes
api-key={PDNS_API_KEY}
webserver=yes
webserver-address=127.0.0.1
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
# noaa_swpc_sync/main.py polls at this interval (default 1800). NOAA publishes the Kp bins every three hours and the
# flux a few times a day, so 30 minutes keeps the homepage within a bin of the source without re-downloading.
Environment="POLL_INTERVAL=1800"
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
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/ses_inbound_mail_ingest

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
# Real IAM identity for the `odoo` user, resolved 2026-09-22 --
# AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY for the dedicated
# ses-inbound-mail-ingest-service IAM user (S3 read/delete/tag on
# incoming/*, write on processed/*+failed/*, scoped to
# hams-com-inbound-mail only -- see docs/proposals/EMAIL_SEND_RECEIVE.md).
# One-time provisioning, same convention as localhost_cert_renewal's
# cloudflare.ini: a person places this file, the daemon never touches it.
EnvironmentFile=-/opt/hams/etc/aws.env
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
            "path": "/opt/hams/systemd/relay.cert.renew.service",
            "external_fetch": "renews a certificate with a public ACME CA and a DNS provider API",
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
            "external_fetch": "activates relay.cert.renew.service",
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
            "path": "/opt/hams/systemd/localhost.cert.renewal.service",
            "external_fetch": "renews a certificate with a public ACME CA and a DNS provider API",
            "content": """\
[Unit]
Description=Shared localhost.hams.com TLS Certificate Renewal (ADR 0100)
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
ReadWritePaths=/opt/hams/etc/localhost_cert_renewal
Type=oneshot
# A dedicated account owns the shared certificate's private key (ADR-0095 credential locality);
# hams_com is only for traversing /opt/hams to reach the daemon code and its own directory.
User=localhost_cert
Group=localhost_cert
SupplementaryGroups=hams_com
WorkingDirectory=/opt/hams/daemons/localhost_cert_renewal
UMask=0027

Environment="LOCALHOST_CERT_BASE_DIR=/opt/hams/etc/localhost_cert_renewal"

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/localhost_cert_renewal/main.py --start-test

# A nonzero exit (failed or backing-off renewal, or 14 days or fewer left) fails this unit, which
# the pager_duty "Systemd Failed Services Tracker" check turns into an operator alert.
ExecStart=/usr/bin/python3 /opt/hams/daemons/localhost_cert_renewal/main.py

StandardOutput=journal
StandardError=journal
SyslogIdentifier=localhost.cert.renewal
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/localhost.cert.renewal.timer",
            "external_fetch": "activates localhost.cert.renewal.service",
            "content": """\
[Unit]
Description=Check The Shared localhost.hams.com Certificate For Renewal Daily

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=2h
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
            # hams_com daemons/device_command_signer: a long-running signing helper, one
            # per key. odoo reaches it over the Unix socket in
            # /run/hams_device_command_signer (RuntimeDirectory, 0750; odoo is in that
            # group). It writes both its 0700 key directory and the public sibling, so
            # both are ReadWritePaths. SupplementaryGroups=hams_com lets it traverse
            # /opt/hams (0750 hams_com) to reach its code and directories.
            "path": "/opt/hams/systemd/hams-device-command-signer.service",
            "content": """\
[Unit]
Description=Privilege-isolated device-command-authority signing helper
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
ReadWritePaths=/opt/hams/etc/device_command_signer /opt/hams/etc/device_command_signer_public
RuntimeDirectory=hams_device_command_signer
RuntimeDirectoryMode=0750
Type=simple
User=hams_device_command_signer
Group=hams_device_command_signer
SupplementaryGroups=hams_com
WorkingDirectory=/opt/hams/daemons/device_command_signer
UMask=0027

Environment="DEVICE_COMMAND_SIGNER_BASE_DIR=/opt/hams/etc/device_command_signer"
Environment="DEVICE_COMMAND_SIGNER_PUBLIC_DIR=/opt/hams/etc/device_command_signer_public"
Environment="DEVICE_COMMAND_SIGNER_SOCKET_PATH=/run/hams_device_command_signer/signer.sock"

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/device_command_signer/main.py --start-test
ExecStart=/usr/bin/python3 /opt/hams/daemons/device_command_signer/main.py

Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hams.device.command.signer

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
            "external_fetch": "activates uk.ofcom.sync.service",
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
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
Environment="SYSTEM_USER_AGENT=Hams.com Sync Daemon (bruce@perens.com, run from pi500-1 -- see night_shift_todo/high/fcc-uls-sync-needs-non-datacenter-egress-path-a8e5f3c1.md)"
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
ReadWritePaths=
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/hamcall_idx_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=hamcall_verify_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/hamcall_verify_sync_service_internal.key"
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/ncvec_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=ncvec_sync_service_internal"
Environment="HAMS_NCVEC_DATA_DIR=/opt/hams/spool/ncvec"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/ncvec_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Execution via system Python
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
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
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

# Added 2026-10-02 -- like hams.simulated.bots.service's own 2026-09-30 fold-in
# below, this previously existed ONLY in a hand-maintained, untracked drop-in
# on hams1 (/etc/systemd/system/hams.simulated.band.service.d/bot-keys.conf,
# dated 2026-09-24). It is a file *path*, not a secret: the per-host bot key
# lines (minted with daemons/hams_simulated_band/tools/new_bot_key.sh) stay
# hand-placed at this path, mode 0600/0640, never in this unit text. Without
# the variable the SFU starts but refuses every QAI (bot) client; with it set
# and the file missing, the SFU fails fast at startup by design (bot_auth.rs).
# Provisioning only links this unit, it never enables it, so a host without
# the key file is unaffected until someone deliberately turns the band on.
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
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/hams_simulated_bots

Environment="HF_HOME=/opt/hams/cache/whisper"
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
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/hams_simulated_bots

Environment="HF_HOME=/opt/hams/cache/whisper"
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
exec /usr/bin/python3 /opt/hams/daemons/hams_ticket_triage_mcp/main.py
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
        # Installed, but not production's front end: hams1 keeps
        # nginx.service disabled (ingress is the Cloudflare Tunnel; see the
        # /opt/hams/nginx entry above). The dev box uses it for its local
        # 127.0.0.1:8080 bus proxy (hams_com CLAUDE.md).
        {"name": "nginx", "debian_name": "nginx", "environments": ["early_prod"]},
        # hook_build_rust_daemons() runs `cargo build`; without this a fresh box (no rustup)
        # reports PROVISIONING DEGRADED and the three Rust daemons are never built.
        # Debian 13's plain `cargo` is 1.85, too old for the daemons' dependencies (icu_* crates need
        # 1.86); Debian's security-maintained cargo-web/rustc-web (1.96) replace it and provide
        # /usr/bin/cargo, so the hook's plain `cargo` command works.
        {"name": "cargo", "debian_name": "cargo-web", "environments": ["early_prod"]},
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
        for key, value in ODOO_CONF_GEVENT_MEMORY_LIMITS:
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


# [@ANCHOR: infrastructure:activation_units_to_enable]
def _activation_units_to_enable(linked_units, is_test_env):
    """The linked .timer/.path units provisioning should `systemctl enable`.

    In a test environment (provision.py --test, or test.py's isolated
    provisioning) external-fetch units are left linked but never enabled,
    so no timer ever fires them. Production enables every linked unit."""
    if not is_test_env:
        return list(linked_units)
    external = external_fetch_unit_names()
    return [unit for unit in linked_units if unit not in external]


# [@ANCHOR: infrastructure:smoketest_candidate_services]
def _smoketest_candidate_services(has_hams_com=True, is_test_env=False):
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
            if svc_name not in potential_services and "@" not in svc_name:
                potential_services.append(svc_name)
    return potential_services


def run_post_provision_smoketest(has_hams_com=True, is_test_env=False):
    _logger.info("[*] Running post-provisioning smoketest on all services...")

    try:
        subprocess.run(["systemctl", "daemon-reload"], check=False)
    except OSError as e:
        _logger.debug("Ignored OSError during daemon-reload: %s", e)

    potential_services = _smoketest_candidate_services(
        has_hams_com, is_test_env
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
            "HamsComSyncDaemon/1.0 (+https://hams.com; bruce@perens.com; +1-510-473-7367)",
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


def provision_environment(
    run_cmd_func,
    env_vars,
    orig_user,
    os_id=None,
    skip_apt=False,
    is_test=False,
    hold_odoo=False,
):
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
            run_cmd_func(["apt-get", "install", "-y"] + apt_opts + all_packages)

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
        # .path units (first used 2026-10-01, hams-pgbackrest-backup.path) are an
        # [Install] WantedBy= activation unit exactly like a .timer -- they need the
        # same explicit `systemctl enable` below, not just the symlink, to actually
        # fire. Named linked_activation_units (was linked_timers) to reflect that.
        linked_activation_units = []
        try:
            systemd_dir = "/opt/hams/systemd"
            if os.path.exists(systemd_dir):
                for item in os.listdir(systemd_dir):
                    if item.endswith((".service", ".timer", ".path")):
                        if not has_hams_com and item != "hams-pycache.service":
                            continue
                        src = os.path.join(systemd_dir, item)
                        dst = os.path.join("/etc/systemd/system", item)
                        if not os.path.exists(dst):
                            os.symlink(src, dst)
                        if item.endswith((".timer", ".path")):
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
            linked_activation_units, is_test_env
        )
        held_back = sorted(set(linked_activation_units) - set(units_to_enable))
        if held_back:
            _logger.info(
                "[*] Test environment: NOT enabling %d external-fetch unit(s): %s",
                len(held_back),
                ", ".join(held_back),
            )
        if units_to_enable:
            _logger.info(
                "[*] Enabling %d linked systemd timer/path unit(s)...",
                len(units_to_enable),
            )
            try:
                subprocess.run(["systemctl", "daemon-reload"], check=False)
                for unit in units_to_enable:
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
        elif not is_isolated_ns:
            initialize_odoo_database(
                run_cmd_func,
                hams_community_dir,
                hams_com_dir,
                db_name=env_vars.get("DB_NAME", "hams_test"),
            )
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
