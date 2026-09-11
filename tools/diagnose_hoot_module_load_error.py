#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Diagnose an Odoo hoot "Error while loading" module-load failure
-------------------------------------------------------------------
Odoo's own JS module loader (odoo/addons/web/static/src/module_loader.js,
`ModuleLoader.startModule`) catches every error a module factory throws and
rethrows a brand-new `Error('Error while loading "${name}":\n${error}')` --
this discards the ORIGINAL error's `.stack` entirely (only `${error}`'s
`.toString()` survives), so the browser console only ever shows a flat
one-line message like `TypeError: Illegal invocation` with no indication of
which native call inside the module's own factory actually threw. Built
2026-09-08 after exactly this happened for `@barcodes/barcode_service`
(root cause: Odoo's own hoot test-window mocking replaces `window.navigator`
with a look-alike object -- see `hoot/mock/navigator.js`'s `createMock()` --
whose override list omits `navigator.platform`, so `feature_detection.js`'s
`isIOS()` falls through to the real native `Navigator.prototype.platform`
getter with the mock as `this`, which V8's branded-accessor check rejects).
That specific bug was Odoo-core, not hams-owned, and turned out to be a
one-off dead end to chase further -- but the DIAGNOSTIC TECHNIQUE (recover
the real stack trace Odoo's own loader throws away) is generically useful
for any future "Error while loading X: <flat error text>" failure, so it's
kept here as a reusable tool rather than a one-off scratch script.

What this does: temporarily patches the installed Odoo package's
module_loader.js to log the real `error.stack` right before it's discarded,
runs the given failing test tag through the real Odoo test framework
(--test-enable, a real headless Chrome via HttpCase.browser_js, not a
mock), captures and prints every real stack trace that surfaces, and always
restores the original module_loader.js afterward -- even on a crash (the
patch target is a real, git-tracked-nowhere system package file outside
both hams repos, so leaving it patched would silently corrupt every other
JS test/page load on this box until someone noticed and reinstalled Odoo).

Run directly (needs the same sudo/env access as any real Odoo test run --
see the hams-odoo-test-runner-sudo-and-polkit convention):
    python3 hams_shared/tools/diagnose_hoot_module_load_error.py \\
        --test-tags /ham_shack:TestBandmapHoot

Not wired into run_linters.py or any CI gate -- this spins up a full,
separate Odoo process against the real database and is meant to be run
by hand when a specific hoot failure needs its real stack trace, not on
every commit.
"""

import argparse
import os
import re
import socket
import subprocess
import sys

CATCH_TARGET = "this.failed.add(name);"
STACK_CAPTURE_MARKER = "STACK_CAPTURE_FOR_"


def find_odoo_module_loader_path():
    """Locate the real, installed module_loader.js next to the running Odoo package --
    computed via `import odoo`, not a hardcoded distro path, so this works regardless of
    where Odoo happens to be installed on a given box."""
    # odoo is a namespace package on this box (no single __init__.py), so __file__ is None --
    # __path__[0] is the real, reliable way to find it either way.
    result = subprocess.run(
        [sys.executable, "-c", "import odoo; print(list(odoo.__path__)[0])"],
        capture_output=True,
        text=True,
        check=True,
    )
    odoo_pkg_dir = result.stdout.strip()
    return os.path.join(odoo_pkg_dir, "addons", "web", "static", "src", "module_loader.js")


def patch_module_loader_content(original_content):
    """Insert a console.error() of the real error.stack immediately before
    ModuleLoader.startModule's own catch block discards it into a flat
    `new Error(...)` wrapper. Raises ValueError if the target text isn't found
    (a real, useful failure mode if a future Odoo version restructures this
    method -- silently patching nothing would be worse than erroring here)."""
    content_count = original_content.count(CATCH_TARGET)
    if content_count == 0:
        raise ValueError(
            f"{CATCH_TARGET!r} not found in module_loader.js -- Odoo's own module loader "
            "structure may have changed since this tool was written"
        )
    if content_count != 1:
        raise ValueError(
            f"expected exactly one occurrence of {CATCH_TARGET!r}, found {content_count} -- "
            "module_loader.js's structure may have changed; refusing to guess which one to patch"
        )
    injected = (
        f"\n                console.error('{STACK_CAPTURE_MARKER}' + name + ':', "
        "(error && error.stack) || String(error));"
    )
    return original_content.replace(CATCH_TARGET, CATCH_TARGET + injected)


def extract_stack_captures(test_output):
    """Pull every real captured stack trace out of a test run's raw combined output.
    Each capture starts with the STACK_CAPTURE_FOR_ marker this tool's own patch emits
    and runs until the next blank-ish log boundary (a line that doesn't look like a
    continuation of a JS stack frame)."""
    captures = []
    lines = test_output.splitlines()
    i = 0
    while i < len(lines):
        idx = lines[i].find(STACK_CAPTURE_MARKER)
        if idx == -1:
            i += 1
            continue
        header = lines[i][idx:]
        block = [header]
        i += 1
        while i < len(lines) and re.match(r"^\s*at \S", lines[i]):
            block.append(lines[i])
            i += 1
        captures.append("\n".join(block))
    return captures


def write_as_root(path, content):
    """The real module_loader.js lives under /usr/lib/python3/dist-packages, owned by root --
    this shell script runs as plain bruce, so writing it needs a real sudo invocation rather
    than requiring the whole diagnostic tool (argument parsing, port allocation, subprocess
    orchestration) to run as root just for these two file writes."""
    result = subprocess.run(
        ["sudo", "tee", path], input=content, capture_output=True, text=True, check=True
    )
    return result


def restore_and_verify(loader_path, original_content):
    """Always attempt to restore `loader_path` to `original_content` and report the real
    outcome, even if the restore's own write fails.

    Real bug found 2026-09-10: main()'s own `finally` block used to call `write_as_root()`
    directly with no exception handling of its own. `write_as_root` uses `check=True`, so a
    real, plausible failure on a shared dev box (an expired sudo timestamp, a transient
    permission glitch) would raise `CalledProcessError` INSIDE the `finally` block -- which
    replaces whatever exception (if any) was already propagating from the `try` body, with no
    trace of what that original error was, and with NO attempt to verify or report the real
    on-disk state of a shared system file every other JS test on this box depends on. This
    function makes the restore-and-verify step itself fail closed: a failure at any point
    (the write, or the follow-up read-back to verify it) prints a clear, actionable warning
    and returns, rather than letting an exception escape the caller's own `finally` block.
    """
    print(f"[*] Restoring original {loader_path}...")
    try:
        write_as_root(loader_path, original_content)
    except Exception as e:
        print(f"🛑 WARNING: failed to restore {loader_path}: {e}")
        print(
            f"🛑 WARNING: {loader_path} is left in an UNKNOWN, possibly-patched state -- "
            "check it by hand before running any other JS test on this box."
        )
        return
    try:
        with open(loader_path, "r", encoding="utf-8") as f:
            restored = f.read()
    except OSError as e:
        print(f"🛑 WARNING: could not re-read {loader_path} to verify the restore: {e}")
        return
    if restored != original_content:
        print(f"🛑 WARNING: {loader_path} did not verify as restored correctly -- check it by hand.")
    else:
        print("[+] Restore verified.")


def get_free_port():
    """OS-assigned free TCP port -- inherently racy (nothing stops another process
    from binding it between this call and the real server starting), but good enough
    for a one-shot diagnostic run, same tradeoff any \"find a free port\" helper makes."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_odoo_service_env(systemd_unit="odoo.service"):
    """Read the real running Odoo service's own environment (ODOO_URL, DB_NAME, etc.) --
    several hams modules import daemon helper code at test-discovery time that hard-requires
    these vars (see e.g. daemons/hams_config.py), and they're only reliably present in the
    real systemd-managed service's own environment, not this shell's."""
    pid_result = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", systemd_unit],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = pid_result.stdout.strip()
    if not pid or pid == "0":
        raise RuntimeError(f"{systemd_unit} does not appear to be running (MainPID={pid!r})")
    # The service runs as a different OS user (odoo), so reading its /proc/<pid>/environ
    # needs a real sudo, same as write_as_root() needs one for the system JS file.
    environ_result = subprocess.run(
        ["sudo", "cat", f"/proc/{pid}/environ"], capture_output=True, check=True
    )
    raw = environ_result.stdout
    env = {}
    for entry in raw.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        key, _, value = entry.decode("utf-8", errors="replace").partition("=")
        env[key] = value
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test-tags", required=True, help="Odoo --test-tags value, e.g. /ham_shack:TestBandmapHoot")
    parser.add_argument("--db", default="hams_dev")
    parser.add_argument("--config", default="/etc/odoo/odoo.conf")
    parser.add_argument("--systemd-unit", default="odoo.service")
    parser.add_argument("--os-user", default="odoo")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    loader_path = find_odoo_module_loader_path()
    print(f"[*] Patching {loader_path} to capture real error stacks...")
    with open(loader_path, "r", encoding="utf-8") as f:
        original_content = f.read()
    patched_content = patch_module_loader_content(original_content)

    try:
        write_as_root(loader_path, patched_content)

        print(f"[*] Reading {args.systemd_unit}'s real environment...")
        service_env = get_odoo_service_env(args.systemd_unit)

        port = get_free_port()
        print(f"[*] Running --test-tags {args.test_tags!r} against a one-shot Odoo process on port {port}...")

        # Real bug found 2026-09-10: this used to build a single shell string
        # (`env -i $(cat <tempfile>) /usr/bin/odoo --config {args.config} ...`) run via
        # `bash -c`, interpolating every CLI arg AND every real environment variable value
        # unquoted. `bash -c` then word-splits both the `$(cat ...)` substitution and the
        # interpolated args on whitespace -- any real env var value from the live Odoo
        # service's own environment (or any CLI arg this tool's own operator passes) that
        # contains a space or shell metacharacter would silently corrupt the resulting argv
        # (a value split into multiple bogus `env` assignments, or a stray shell token). Fixed
        # by passing every argument and every KEY=VALUE environment pair as its own real argv
        # element to `sudo`/`env`/`odoo` directly -- no shell involved, so no word-splitting or
        # re-parsing of any value regardless of its content. This also removes the temp
        # env-file/chmod/unlink dance entirely, since `env -i KEY=VAL ...` accepts the pairs
        # directly as leading arguments.
        odoo_cmd = [
            "/usr/bin/odoo",
            "--config", args.config,
            "-d", args.db,
            "--test-enable",
            "--test-tags", args.test_tags,
            "--stop-after-init",
            "--workers=0",
            f"--http-port={port}",
            "--log-level=test",
        ]
        env_pairs = [f"{key}={value}" for key, value in service_env.items()]
        result = subprocess.run(
            ["sudo", "-u", args.os_user, "env", "-i"] + env_pairs + odoo_cmd,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
        output = result.stdout + result.stderr

        captures = extract_stack_captures(output)
        if captures:
            print(f"\n🔎 {len(captures)} real stack trace(s) recovered:\n")
            for capture in captures:
                print(capture)
                print("-" * 60)
        else:
            print("\n[+] No STACK_CAPTURE_FOR_ markers found -- either the test passed, or it failed a "
                  "different way than a module-load error (check the raw output below).")

        print("\n--- raw test output tail ---")
        print("\n".join(output.splitlines()[-40:]))

        return 0 if captures == [] and result.returncode == 0 else 1
    finally:
        restore_and_verify(loader_path, original_content)


if __name__ == "__main__":
    sys.exit(main())
