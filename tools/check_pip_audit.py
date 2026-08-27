#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
pip-audit gate (see docs/proposals/CODE_REVIEW_PROCESS.md, "Formal verification tooling")
-------------------------------------------------------------------------------------------
The direct Python parallel to `cargo-deny`/`cargo-audit` on the Rust side
(RELAY_SUPPLY_CHAIN_SECURITY.md section 3): scans every `requirements*.txt`
in the repo against the Python Packaging Advisory Database (via PyPI's JSON
API) and fails the linter run if any pinned or resolvable dependency has a
known vulnerability. Requires network access, the same way the Rust
advisory scan does.

Scans the whole repo rather than a possibly-scoped `targets` list, same
reasoning as the other full-repo steps in run_linters.py: a vulnerable
dependency can be introduced by a `requirements.txt` anywhere, not just
near whatever file a targeted lint run happens to be scoped to.
"""

import os
import subprocess
import sys

IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git"}


def _resolve_repo_root(given_path):
    """run_linters.py's own `dir_path` resolves to the hams_shared directory itself, not a real
    repo root (same bug found and fixed in check_model_extension_collisions.py and others) --
    confirmed directly: this checker was silently finding 0 requirements.txt files via
    run_linters.py's actual invocation, versus 2 at a real repo root -- this codebase's Python
    supply-chain vulnerability gate (CODE_REVIEW_PROCESS.md) has never actually scanned anything
    in CI. Detect the hams_shared case by name and redirect to its real parent repo."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """The fix above only ever redirects to ONE repo (hams_open) -- but real requirements.txt
    files exist in both: hams_open's own root, and two under hams_com/daemons/
    (gdpr_csv_export, hams_simulated_bots). run_linters.py's own actual invocation was catching
    hams_open's but silently missing both hams_com ones. Same sibling-repo shape as the other
    fixed checkers."""
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


def find_requirements_files(repo_root):
    found = []
    for root, dirs, filenames in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES]
        for name in filenames:
            if name.startswith("requirements") and name.endswith(".txt"):
                found.append(os.path.join(root, name))
    return sorted(found)


def main():
    if len(sys.argv) < 2:
        print("Usage: check_pip_audit.py <repo_root>")
        sys.exit(1)

    requirements_files = [
        (repo_root, req_file)
        for repo_root in _resolve_repo_roots(sys.argv[1])
        for req_file in find_requirements_files(repo_root)
    ]
    if not requirements_files:
        sys.exit(0)

    check = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--version"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        print(
            "❌ pip-audit is not installed (python3 -m pip install --user "
            "--break-system-packages pip-audit) -- required by "
            "CODE_REVIEW_PROCESS.md's supply-chain scanning gate."
        )
        sys.exit(1)

    any_failed = False
    for repo_root, req_file in requirements_files:
        rel_path = os.path.relpath(req_file, repo_root)
        res = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "-r",
                req_file,
                "--progress-spinner",
                "off",
            ],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        if res.returncode != 0:
            if not any_failed:
                print("❌ pip-audit findings:")
            any_failed = True
            print(f"-- {rel_path} --")
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
