#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
cargo-clippy gate (see docs/proposals/CODE_REVIEW_PROCESS.md, "Formal verification tooling")
-------------------------------------------------------------------------------------------
The Rust code-quality scan that is flake8/ESLint's direct parallel on the Rust side, but --
like cargo-deny (check_cargo_deny.py) -- was never actually wired into run_linters.py at
all, a real gap found 2026-08-26 running `cargo clippy` by hand for the first time in this
codebase's history: 3 real findings in hams_local_relay alone (a genuinely dead struct
field, an unused-but-load-bearing match-arm variable that turned out to be an honestly
documented MVP stub, and two more findings in each of the other 3 daemon crates). Runs
`cargo clippy -- -W clippy::all` in every daemons/ subdirectory that has a Cargo.toml.

Scans the whole repo rather than a possibly-scoped `targets` list, same reasoning as
check_cargo_deny.py/check_pip_audit.py: a clippy-flagged pattern can be introduced in any
crate, not just near whatever file a targeted lint run happens to be scoped to.
"""

import os
import subprocess
import sys

IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git"}


def _resolve_repo_root(given_path):
    """Same hams_shared-redirect fix as check_pip_audit.py/check_cargo_deny.py."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def find_cargo_crates(repo_root):
    """Top-level crate directories only (a Cargo.toml with a [package] table) -- workspace-member
    sub-crates (e.g. hams_local_relay/ham_digital_modes) are checked automatically as part of
    their parent workspace's own `cargo clippy` run, so listing them separately would just
    duplicate work."""
    found = []
    for root, dirs, filenames in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES]
        if "Cargo.toml" not in filenames:
            continue
        with open(os.path.join(root, "Cargo.toml")) as f:
            if "[package]" in f.read():
                found.append(root)
    return sorted(found)


def main():
    if len(sys.argv) < 2:
        print("Usage: check_cargo_clippy.py <repo_root>")
        sys.exit(1)

    repo_root = _resolve_repo_root(sys.argv[1])
    crate_dirs = find_cargo_crates(repo_root)
    if not crate_dirs:
        sys.exit(0)

    check = subprocess.run(["cargo", "clippy", "--version"], capture_output=True, text=True)
    if check.returncode != 0:
        print(
            "❌ cargo-clippy is not installed (rustup component add clippy) -- required by "
            "CODE_REVIEW_PROCESS.md's Rust code-quality gate."
        )
        sys.exit(1)

    any_failed = False
    for crate_dir in crate_dirs:
        rel_path = os.path.relpath(crate_dir, repo_root)
        res = subprocess.run(
            ["cargo", "clippy", "--", "-W", "clippy::all"],
            capture_output=True,
            text=True,
            cwd=crate_dir,
        )
        # clippy's own exit code is 0 even for warnings unless -D is passed; this gate treats
        # any "warning:" line in stderr as a real finding, matching how -W clippy::all is meant
        # to surface style/correctness issues without hard-denying the whole build on every one.
        if "warning:" in res.stderr:
            if not any_failed:
                print("❌ cargo-clippy findings:")
            any_failed = True
            print(f"-- {rel_path} --")
            print(res.stderr, end="")
        if res.returncode != 0:
            if not any_failed:
                print("❌ cargo-clippy findings:")
            any_failed = True
            print(f"-- {rel_path} (build error) --")
            if res.stdout:
                print(res.stdout, end="")
            print(res.stderr, end="")

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
