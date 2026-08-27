#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
cargo-deny gate (see docs/proposals/CODE_REVIEW_PROCESS.md, "Formal verification tooling")
-------------------------------------------------------------------------------------------
The Rust supply-chain/license/advisory scan check_pip_audit.py's own docstring already
describes as its "direct Python parallel" -- but was never actually wired into
run_linters.py at all, a real gap found 2026-08-26 while running cargo-deny by hand for
the first time in this codebase's history and finding 11 real, previously-unremediated
advisory/license/unmaintained findings in hams_local_relay alone (an HTTP/2
unbounded-DATA-frame DoS vector among them). Runs `cargo deny check` in every daemons/
subdirectory that has both a Cargo.toml and a deny.toml -- a crate with no deny.toml is
skipped, not failed, the same way check_pip_audit.py skips a repo with no requirements.txt
rather than treating "not configured yet" as a failure.

Scans the whole repo rather than a possibly-scoped `targets` list, same reasoning as
check_pip_audit.py: a vulnerable dependency can be introduced in any crate, not just
near whatever file a targeted lint run happens to be scoped to.
"""

import os
import subprocess
import sys

IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git"}


def _resolve_repo_root(given_path):
    """Same hams_shared-redirect fix as check_pip_audit.py/check_model_extension_collisions.py:
    run_linters.py's own dir_path resolves to the hams_shared directory itself, not a real
    repo root, when invoked from within hams_shared."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """The fix above only ever redirects to ONE repo (hams_open) -- but every crate with a real
    deny.toml (hams_local_relay, hams_relay_bridge, hams_data_relay, hams_simulated_band) lives
    under hams_com/daemons/; hams_open's own daemons/ham_digital_modes has no deny.toml at all.
    run_linters.py's own actual invocation was therefore finding zero deny.toml crates and
    silently exiting 0 without ever once running `cargo deny` on any of the four real,
    already-configured crates. Same sibling-repo shape as the other fixed checkers."""
    repo_root = _resolve_repo_root(given_path)
    roots = [repo_root]
    sibling_name = "hams_open" if os.path.basename(repo_root) != "hams_open" else "hams_com"
    sibling = os.path.abspath(os.path.join(repo_root, "..", sibling_name))
    if os.path.isdir(sibling) and any(
        os.path.isfile(os.path.join(sibling, d, "__manifest__.py"))
        for d in os.listdir(sibling)
        if os.path.isdir(os.path.join(sibling, d))
    ):
        roots.append(sibling)
    return roots


def find_deny_crates(repo_root):
    found = []
    for root, dirs, filenames in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES]
        if "Cargo.toml" in filenames and "deny.toml" in filenames:
            found.append(root)
    return sorted(found)


def main():
    if len(sys.argv) < 2:
        print("Usage: check_cargo_deny.py <repo_root>")
        sys.exit(1)

    crate_dirs = [
        (repo_root, crate_dir)
        for repo_root in _resolve_repo_roots(sys.argv[1])
        for crate_dir in find_deny_crates(repo_root)
    ]
    if not crate_dirs:
        sys.exit(0)

    check = subprocess.run(["cargo", "deny", "--version"], capture_output=True, text=True)
    if check.returncode != 0:
        print(
            "❌ cargo-deny is not installed (cargo install cargo-deny, or "
            "`rustup component add` does not cover it -- it's a separate cargo subcommand) "
            "-- required by CODE_REVIEW_PROCESS.md's supply-chain scanning gate."
        )
        sys.exit(1)

    any_failed = False
    for repo_root, crate_dir in crate_dirs:
        rel_path = os.path.relpath(crate_dir, repo_root)
        res = subprocess.run(
            ["cargo", "deny", "check"],
            capture_output=True,
            text=True,
            cwd=crate_dir,
        )
        if res.returncode != 0:
            if not any_failed:
                print("❌ cargo-deny findings:")
            any_failed = True
            print(f"-- {rel_path} --")
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
