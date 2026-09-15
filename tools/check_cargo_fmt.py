#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
cargo-fmt gate
--------------
`build-relay.yml` and `build-server-daemons.yml` both run `cargo fmt -- --check`, but for the relay
only on the ubuntu-22.04 leg, and on the single self-hosted runner that leg can start hours after
the push. On 2026-09-15 three separate rustfmt-only fix commits landed on hams_local_relay
(ff6bd117, 984bf172, 42e2833a), each after a feature commit that was tested and clippy-clean but
never formatted. This gate runs the same check whenever run_linters.py runs, so the break shows up
then instead of in CI. hams_open's own crates have no CI formatting step at all, and two of them
had drifted (ham_digital_modes, rust_function_scan) before this gate existed.

Runs `cargo fmt -- --check` only, never plain `cargo fmt`: run_linters.py runs in the shared
working tree, and rewriting files there would change other sessions' uncommitted work. The same
reason means a peer's unformatted work in progress is reported too; that is a real finding for
whoever commits it.

Each crate is checked with the toolchain cargo resolves in that crate's own directory, so
hams_local_relay's `rust-toolchain.toml` pin applies, matching CI. Crate discovery and the two-repo
sweep are check_cargo_clippy.py's own, so both gates always cover the same crates.
"""

import os
import subprocess
import sys

from check_cargo_clippy import _resolve_repo_roots, find_cargo_crates

FMT_CHECK_COMMAND = ["cargo", "fmt", "--", "--check"]


def check_crate(crate_dir):
    """Return (is_formatted, output) for one crate."""
    res = subprocess.run(FMT_CHECK_COMMAND, capture_output=True, text=True, cwd=crate_dir)
    return res.returncode == 0, res.stdout + res.stderr


def main():
    if len(sys.argv) < 2:
        print("Usage: check_cargo_fmt.py <repo_root>")
        sys.exit(1)

    crate_dirs = [
        (repo_root, crate_dir)
        for repo_root in _resolve_repo_roots(sys.argv[1])
        for crate_dir in find_cargo_crates(repo_root)
    ]
    if not crate_dirs:
        sys.exit(0)

    check = subprocess.run(["cargo", "fmt", "--version"], capture_output=True, text=True)
    if check.returncode != 0:
        print("❌ rustfmt is not installed (rustup component add rustfmt) -- required by the Rust formatting gate.")
        sys.exit(1)

    any_failed = False
    for repo_root, crate_dir in crate_dirs:
        is_formatted, output = check_crate(crate_dir)
        if is_formatted:
            continue
        if not any_failed:
            print("❌ cargo fmt --check findings (run `cargo fmt` in the crate's directory, then commit):")
        any_failed = True
        print(f"-- {os.path.relpath(crate_dir, os.path.dirname(repo_root))} --")
        print(output, end="")

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
