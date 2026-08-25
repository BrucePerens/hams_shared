#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
XML Comment Double-Hyphen Checker
-------------------------------------------------------------------------------------------
The XML spec forbids a literal `--` anywhere inside a `<!-- ... -->` comment except as the
closing delimiter itself. lxml (and every other real XML parser) enforces this strictly:
`lxml.etree.XMLSyntaxError: Double hyphen within comment`. Odoo's module loader has no partial-
failure mode for this -- one malformed comment in one XML data file aborts that module's install
outright.

Hit this exact bug twice in one night (2026-08-24/25) writing explanatory comments in security
XML files, both times from using " -- " as an em-dash-style aside (a habit carried over from
this codebase's own Python comment style, where it's fine) inside an XML `<!-- -->` block. Both
were only caught by actually running the test suite and getting a real module-load crash --
exactly the kind of failure check_access_csv_group_order.py/check_module_subpackage_imports.py
already exist to catch statically instead of via a live crash discovered by whoever next
happens to run the full suite.

There is no legitimate use of `--` inside an XML comment (it's a hard syntax error, not a style
preference), so there is no escape-hatch comment for this check.

Usage: check_xml_comment_double_hyphen.py <repo_root>
"""

import os
import re
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "tools", "radae"}

_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


def _resolve_repo_root(given_path):
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _line_of_offset(text, offset):
    return text.count("\n", 0, offset) + 1


def check_xml_comment_double_hyphen(repo_root):
    violations = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in files:
            if not filename.endswith(".xml"):
                continue
            path = os.path.join(root, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError as e:
                print(f"Warning: UnicodeDecodeError reading {path}: {e}")
                continue

            for match in _COMMENT_RE.finditer(content):
                inner = match.group(1)
                if "--" in inner:
                    lineno = _line_of_offset(content, match.start() + inner.index("--"))
                    violations.append(
                        f"{os.path.relpath(path, repo_root)}:{lineno} "
                        f"Literal '--' inside an XML comment -- illegal per the XML spec "
                        f"(lxml raises 'Double hyphen within comment' and Odoo's module "
                        f"loader aborts the whole module install on it). Reword to avoid "
                        f"the double hyphen (e.g. use a comma or period instead of an "
                        f"em-dash-style ' -- ' aside)."
                    )
    return violations


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_xml_comment_double_hyphen.py <repo_root>")
        sys.exit(1)

    repo_root = _resolve_repo_root(sys.argv[1])
    violations = check_xml_comment_double_hyphen(repo_root)

    if violations:
        print("❌ XML comment double-hyphen violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)

    print("✅ No XML comment double-hyphen violations found.")
    sys.exit(0)
