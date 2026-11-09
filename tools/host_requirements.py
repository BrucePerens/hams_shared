#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a test host needs besides the packages, for an Odoo test run that is already inside its own account
(`sudo -u odoo env HAMS_ISOLATED_NS=1 ... test.py`, the convention in hams_com CLAUDE.md).

That run skips the namespaced provisioning `provision.py --test` does, so nothing in it creates the host-wide
pieces below. A host without them used to fail deep in a test ("DB_USER and DB_PASS must both be set", a missing
backups directory, git refusing a checkout it does not own) hours into a first night. `test.py` now calls
`require_test_host()` first and stops with the list and the one command that fixes it; `provision.py
--test-environment` provisions every item (provision_test_environment.provision_test_host_requirements) and checks
the list again, so the two cannot drift. Standard library only apart from the account list, which comes from the
MANIFEST. Full entries for every anchor: tools/README.md.
"""
import os
import pwd
import socket
import subprocess
import sys

import infrastructure


# [@ANCHOR: host_requirements:constants]
CRYPTO_KEY_VARIABLE = "HAMS_CRYPTO_KEY"
CACHE_MANAGER_LOGIN_FILE = "/opt/hams/etc/keys/cache_manager/cache_manager_db.env"
BACKUPS_DIRECTORY = "/var/lib/odoo/backups"
SERVICE_HOST_NAME = "redis"
FIX_COMMAND = "sudo python3 hams_shared/tools/provision.py --test-environment"
PROFILE_FILE = "/etc/profile.d/hams_test_environment.sh"


def _account_exists(name):
    try:
        pwd.getpwnam(name)
    except KeyError:  # burn-ignore-os-account-probe
        return False
    return True


def git_trusts_every_directory():
    """True when the SYSTEM git config holds `safe.directory = *`. `--system` is deliberate: provisioning writes only that scope, and the
    odoo account that runs the tests does not read the global config of whoever ran provisioning (a user-level `*` must not pass)."""
    result = subprocess.run(["git", "config", "--system", "--get-all", "safe.directory"], capture_output=True, text=True)
    return "*" in result.stdout.split()


def _cache_manager_login_keys():
    """The names with a non-empty value in the cache_manager login file, or None when it cannot be read."""
    try:
        with open(CACHE_MANAGER_LOGIN_FILE, encoding="utf-8") as login:
            lines = login.read().splitlines()
    except OSError:
        return None
    pairs = (line.split("=", 1) for line in lines if "=" in line and not line.lstrip().startswith("#"))
    return {name.strip() for name, value in pairs if value.strip()}


def _resolves(name):
    try:
        socket.gethostbyname(name)
    except OSError:
        return False
    return True


# [@ANCHOR: host_requirements:test_account_names]
def test_account_names(manifest=None):
    """The system accounts a test host has: every MANIFEST `system_accounts` entry for the "test" environment that
    applies to this host's classes (the entries provision_system_accounts(environment="test") creates)."""
    manifest = infrastructure.MANIFEST if manifest is None else manifest
    return [acc["user"] for acc in manifest.get("system_accounts", [])
            if "test" in acc.get("environments", ["prod", "test"]) and infrastructure._spec_selected(acc)]


# [@ANCHOR: host_requirements:missing_requirements]
def missing_requirements(environ=None, check_crypto_key=True, account_exists=_account_exists,
                         is_directory=os.path.isdir, login_keys=_cache_manager_login_keys,
                         git_trusts_all=git_trusts_every_directory, resolves=_resolves, account_names=None):
    """[(code, text)] of what this host lacks, in a fixed order; [] when it has everything. Codes: `crypto_key`
    (HAMS_CRYPTO_KEY absent from `environ`; skipped with check_crypto_key=False, which provisioning uses because it
    writes the key to the profile and not to its own process), `accounts` (a MANIFEST test account is missing:
    the hamsd_* accounts), `backups_directory`, `cache_manager_login` (the file is unreadable by this user or lacks
    DB_USER or DB_PASS), `git_safe_directory` (git does not trust a checkout the odoo user does not own) and
    `service_host` (the name `redis` does not resolve). The keyword arguments are the probes, replaced in tests."""
    environ = os.environ if environ is None else environ
    missing = []
    if check_crypto_key and not environ.get(CRYPTO_KEY_VARIABLE):
        missing.append(("crypto_key", f"{CRYPTO_KEY_VARIABLE} is not set in this process (sudo drops it unless it follows "
                        f"`sudo -u odoo env`)"))
    names = test_account_names() if account_names is None else account_names
    absent = [name for name in names if not account_exists(name)]
    if absent:
        missing.append(("accounts", f"system account(s) missing: {', '.join(absent)}"))
    if not is_directory(BACKUPS_DIRECTORY):
        missing.append(("backups_directory", f"{BACKUPS_DIRECTORY} does not exist"))
    keys = login_keys()
    if keys is None:
        missing.append(("cache_manager_login", f"{CACHE_MANAGER_LOGIN_FILE} is missing or not readable by this user"))
    elif not {"DB_USER", "DB_PASS"} <= keys:
        missing.append(("cache_manager_login", f"{CACHE_MANAGER_LOGIN_FILE} lacks DB_USER or DB_PASS"))
    if not git_trusts_all():
        missing.append(("git_safe_directory", "git has no system `safe.directory = *` (the odoo user does not own the "
                        "checkouts)"))
    if not resolves(SERVICE_HOST_NAME):
        missing.append(("service_host", f"the name {SERVICE_HOST_NAME!r} does not resolve (/etc/hosts needs "
                        f"127.0.0.1 {SERVICE_HOST_NAME})"))
    return missing


# [@ANCHOR: host_requirements:refusal_text]
def refusal_text(missing):
    """The message `require_test_host` prints for a non-empty `missing`: one line per item, then NEXT: lines naming
    the fix. A missing HAMS_CRYPTO_KEY gets its own line (the profile has the key; the provisioning command does
    not), everything else the provisioning command. Pure."""
    lines = ["❌ test.py: this host is not set up for a HAMS_ISOLATED_NS=1 run:"]
    lines += [f"   - {text}" for _code, text in missing]
    codes = {code for code, _text in missing}
    if codes - {"crypto_key"}:
        lines.append(f"   NEXT: run `{FIX_COMMAND}` (as root, from a hams_shared checkout; it is safe to repeat) "
                     "and start again.")
    if "crypto_key" in codes:
        lines.append(f"   NEXT: `source {PROFILE_FILE}`, then pass the key through sudo, whose env_reset drops it: "
                     f"`sudo -n -u odoo env HAMS_ISOLATED_NS=1 {CRYPTO_KEY_VARIABLE}=\"${CRYPTO_KEY_VARIABLE}\" "
                     "GIT_DISCOVERY_ACROSS_FILESYSTEM=1 python3 hams_shared/tools/test.py ...`.")
    return "\n".join(lines)


# [@ANCHOR: host_requirements:require_test_host]
def require_test_host(environ=None, missing=None):
    """Called first by test.py when HAMS_ISOLATED_NS=1: prints `refusal_text` to stderr and exits 1 when
    missing_requirements() is not empty, and returns None otherwise. `missing` is for tests. Starts nothing and
    changes nothing."""
    missing = missing_requirements(environ) if missing is None else missing
    if missing:
        print(refusal_text(missing), file=sys.stderr)
        sys.exit(1)
