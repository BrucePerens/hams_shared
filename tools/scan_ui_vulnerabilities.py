#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Scans the codebase for Python string literals containing raw XML/HTML tags
or suspicious empty strings that may indicate tags stripped by LLM UIs.

Real gap found 2026-09-12, same class already found and fixed in ~9 sibling checkers
(check_pip_audit.py/check_cargo_deny.py/check_model_extension_collisions.py and others): with no
CLI argument, main()'s `base_dir` is `os.path.dirname(__file__)/..`, which always resolves to the
`hams_shared/` directory itself when this script is run from its real installed location --
`hams_shared/` contains only `agents/`, `docs/`, `tools/`, `scripts/`, and config files, ZERO real
Odoo application modules (confirmed directly: `ls hams_shared/`). This checker's entire purpose is
catching raw XML/HTML leaking into Python string literals in USER-FACING code (portal controllers,
QWeb-adjacent view-generation helpers) -- code that lives in ham_logbook/ham_satellite/
user_websites/etc, never in hams_shared. Its real, no-argument invocation could therefore
structurally never find what it exists to find; two prior bug-hunt review passes tested the
os.walk exclusion logic correctly but never asked whether base_dir ever points at the actual risk
surface. Fixed by adding an OPTIONAL positional CLI argument (mirroring check_pip_audit.py's own
`_resolve_repo_root`/`_resolve_repo_roots` convention) so this script CAN be pointed at a real repo
root and will auto-include its sibling repo the same way those checkers do. The no-argument
default is left exactly as it was (still `os.path.dirname(__file__)/..`) so this fix doesn't
silently change behavior for any existing caller relying on it -- wiring this into
run_linters.py (so a real invocation always passes an explicit, correct root) is a deliberately
separate follow-up, tracked in night_shift_todo.md, since it will surface real findings across the
whole real application tree for the first time ever and that's its own triage job.
"""
import argparse
import os
import re
import sys
import ast
import logging

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
_logger = logging.getLogger(__name__)

IGNORE_DIR_NAMES = {"venv", "node_modules", "__pycache__", ".git"}


def _resolve_repo_root(given_path):
    """Same hams_shared-redirect fix as check_pip_audit.py/check_cargo_deny.py: an explicitly
    given path of `.../hams_shared` is redirected to its real parent repo root, since
    `hams_shared` itself is a shared submodule with no application modules of its own."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """Same sibling-repo discovery as check_pip_audit.py/check_cargo_deny.py: a real Python
    string-literal vulnerability can be introduced in either hams_com or hams_open, not just
    whichever one `given_path` happens to point at."""
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


def scan_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        tree = ast.parse(content, filename=filepath)
    except Exception as e:  # audit-ignore-catch-all
        _logger.warning("Failed to parse %s: %s", filepath, e)
        return False

    vulnerabilities = []

    class StringScanner(ast.NodeVisitor):
        def check_string(self, s, lineno):
            # 1. Look for raw un-escaped HTML/XML tags (e.g., <record>, <group>)
            # We ignore < and > with spaces (e.g. math operators like x < y)
            if re.search(r"<[a-zA-Z/][^>]*>", s):
                vulnerabilities.append(
                    (lineno, "RAW XML TAG (Vulnerable to UI Stripping)", s.strip())
                )

            # 2. Look for suspiciously stripped UI tour anchors or empty strings
            if re.search(r"using\s+['\"]['\"]\s+if", s) or re.search(
                r"\(\s*\)\s+and\s+templates", s
            ):
                vulnerabilities.append(
                    (
                        lineno,
                        "SUSPICIOUS EMPTY STRING (Likely stripped comment/tag)",
                        s.strip(),
                    )
                )

        def visit_Constant(self, node):
            if isinstance(node.value, str):
                self.check_string(node.value, node.lineno)
            self.generic_visit(node)

        def visit_JoinedStr(self, node):
            parts = []
            for val in node.values:
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    parts.append(val.value)
                elif isinstance(val, ast.FormattedValue):
                    parts.append("{...}")
            full_str = "".join(parts)
            self.check_string(full_str, node.lineno)
            self.generic_visit(node)

    StringScanner().visit(tree)

    if vulnerabilities:
        print(f"\n[!] UI Stripping Vulnerabilities detected in {filepath}:")
        for lineno, vtype, text in vulnerabilities:
            # Truncate for display
            snippet = text.replace("\n", " ")
            snippet = snippet[:120] + "..." if len(snippet) > 120 else snippet
            print(f"  Line {lineno} | {vtype}: {snippet}")
        return True
    return False


def _scan_dir(base_dir):
    """Walks one directory tree, returning (scanned_count, vulnerable_files) for it."""
    scanned_count = 0
    vulnerable_files = 0

    for root, dirs, files in os.walk(base_dir):
        if "radae" in dirs:
            dirs.remove("radae")
        # Ignore virtual environments, node modules, and caches
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES]
        for file in files:
            if file.endswith(".py"):
                filepath = os.path.join(root, file)
                if scan_file(filepath):
                    vulnerable_files += 1
                scanned_count += 1

    return scanned_count, vulnerable_files


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scans a repo root (and its real hams_com/hams_open sibling, if present) for "
            "Python string literals containing raw XML/HTML tags."
        )
    )
    parser.add_argument(
        "repo_root",
        nargs="?",
        default=None,
        help=(
            "Repo root to scan. If given, redirects a `hams_shared` path to its real parent "
            "repo and auto-includes the sibling hams_com/hams_open repo, same as "
            "check_pip_audit.py/check_cargo_deny.py. If omitted, preserves this script's "
            "original behavior: scans only its own containing directory "
            "(os.path.dirname(__file__)/..) -- this is almost certainly NOT what a caller "
            "wants for a real vulnerability scan (see this module's own docstring), but changing "
            "the no-argument default would be a silent behavior change for any existing caller "
            "relying on it, so it's preserved as-is; pass an explicit repo_root instead."
        ),
    )
    args = parser.parse_args()

    if args.repo_root is None:
        base_dirs = [os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))]
    else:
        base_dirs = _resolve_repo_roots(args.repo_root)

    print(
        f"[*] Scanning {', '.join(base_dirs)} for LLM UI stripping vulnerabilities..."
    )

    scanned_count = 0
    vulnerable_files = 0
    for base_dir in base_dirs:
        dir_scanned, dir_vulnerable = _scan_dir(base_dir)
        scanned_count += dir_scanned
        vulnerable_files += dir_vulnerable

    print(f"\n[*] Scan complete. Checked {scanned_count} Python files.")
    if vulnerable_files > 0:
        print(
            f"[!] Found {vulnerable_files} file(s) requiring hex-escape immunization (\\x3c / \\x3e)."
        )
        sys.exit(1)
    else:
        print("[*] No vulnerabilities found. Python strings are immunized.")
        sys.exit(0)


if __name__ == "__main__":
    main()
