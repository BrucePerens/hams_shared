#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
"""Proves a migrated daemon unit can do its first step as its own account, without starting the daemon.

    sudo python3 daemon_family_probe.py ncvec.sync.service pota.sync.service

For each unit it reads the installed unit file, builds a transient systemd unit (`systemd-run --wait --pipe`)
that has the unit's own User, Group, EnvironmentFile, Environment, WorkingDirectory, sandbox directives and
writable paths, and runs a few lines of Python in it instead of the daemon: they read the key file named by
ODOO_KEY_FILE and authenticate to Odoo through the daemon's own hams_config client, then print one line. A unit
without a key file only has its environment files and working directory checked. Nothing the daemon fetches
from a third-party server is contacted, nothing is written, and the unit itself is not started.

Why: a family account can fail in ways no test host shows (a missing execute bit on a path, a key the account
cannot open, an environment file that is not there, a sandbox that hides a path). The plan's rehearsal
(DAEMON_OS_ISOLATION_PLAN.md, Section 10) runs this per unit.

[@ANCHOR: daemon_family_probe:probe]
Tested by [@ANCHOR: daemon_family_probe:test]
"""
import argparse
import os
import subprocess
import sys

UNIT_DIRS = ("/etc/systemd/system", "/opt/hams/systemd")

# Directives copied from the unit into the transient one. Exec lines, Restart and the like are not.
COPIED = (
    "User", "Group", "WorkingDirectory", "UMask", "ProtectSystem", "ProtectHome", "PrivateTmp", "PrivateDevices",
    "NoNewPrivileges", "RestrictAddressFamilies", "CapabilityBoundingSet", "ProtectProc", "ProcSubset",
    "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs", "ProtectControlGroups", "ProtectClock",
    "RestrictNamespaces", "RestrictRealtime", "RestrictSUIDSGID", "LockPersonality", "SystemCallArchitectures",
    "ReadWritePaths", "EnvironmentFile", "LimitNOFILE",
)

PROBE = r"""
import logging, os, sys
key_file = os.environ.get("ODOO_KEY_FILE")
if not key_file:
    print("PROBE-OK no key file named; environment and working directory are as the unit has them")
    sys.exit(0)
with open(key_file, "r") as handle:
    handle.read(1)
sys.path.insert(0, os.environ.get("PYTHONPATH", "/opt/hams/daemons").split(":")[0])
from hams_config import get_odoo_client
client = get_odoo_client(logging.getLogger("probe"))
print("PROBE-OK authenticated as uid %s using %s" % (client.uid, key_file))
"""


def read_unit(name):
    for directory in UNIT_DIRS:
        path = os.path.join(directory, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:  # audit-ignore-path: a unit file named on the command line by an operator
                return f.read()
    raise FileNotFoundError(f"{name} is not in {' or '.join(UNIT_DIRS)}")


def service_section(text):
    """The lines of the [Service] section."""
    lines, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped == "[Service]"
            continue
        if inside and stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def properties(text):
    """(directive, value) pairs to pass to systemd-run as -p, from a unit file's text. Environment values keep
    their quotes removed; an optional EnvironmentFile (-) stays optional."""
    pairs = []
    for line in service_section(text):
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "Environment":
            for assignment in _split_environment(value):
                pairs.append(("Environment", assignment))
        elif key in COPIED:
            pairs.append((key, value))
    return pairs


def _split_environment(value):
    """`Environment="A=1" "B=2 3"` or `Environment=A=1`: the assignments."""
    value = value.strip()
    if not value.startswith('"'):
        return [value]
    parts, current, quoted = [], "", False
    for char in value:
        if char == '"':
            if quoted:
                parts.append(current)
                current = ""
            quoted = not quoted
        elif quoted:
            current += char
    return parts


def command(unit_text):
    argv = ["systemd-run", "--wait", "--pipe", "--quiet"]
    for key, value in properties(unit_text):
        argv += ["-p", f"{key}={value}"]
    return argv + ["/usr/bin/python3", "-c", PROBE]


def probe(unit):
    argv = command(read_unit(unit))
    result = subprocess.run(argv, capture_output=True, text=True, check=False)  # audit-ignore-subprocess: argv built from the installed unit
    output = (result.stdout + result.stderr).strip().splitlines()
    ok = result.returncode == 0 and any(line.startswith("PROBE-OK") for line in output)
    return ok, output[-1] if output else f"exit {result.returncode}, no output"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("units", nargs="+")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("run as root (systemd-run starts the transient unit as the unit's own account)", file=sys.stderr)
        return 2
    failures = 0
    for unit in args.units:
        ok, last = probe(unit)
        print(f"{'PASS' if ok else 'FAIL'} {unit}: {last}")
        failures += 0 if ok else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
