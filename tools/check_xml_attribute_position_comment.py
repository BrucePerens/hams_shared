#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
XML `position="attributes"` Comment Checker
-------------------------------------------------------------------------------------------
A real Odoo QWeb inheritance trap, found live 2026-09-28/29 fixing
`theme_hams/views/layout_overrides.xml`'s `hams_theme_attribute` view: placing an XML `<!--
-->` comment as a direct sibling BETWEEN `<attribute>` elements inside an
`<xpath ... position="attributes">` block does not raise, does not warn, and does not fail
the module upgrade -- but it silently makes EVERY `<attribute>` in that block (not just the
ones near the comment) produce nothing at all when the view renders. Confirmed twice against a
real Odoo test run: moving the explanatory comment in between the `<attribute>` children broke
a previously-passing real-HTTP assertion outright; moving it back out before the `<xpath>`
element (where every other explanatory comment in that file already lives) fixed it again, with
no other change. Full write-up:
night_shift_todo/low/qweb-inheritance-comment-inside-position-attributes-breaks-silently-e3c4eefd.md

This is exactly the "arch loaded fine, output is wrong anyway" failure mode that produces no
module-load error and no test failure unless the specific attribute happens to be covered by a
real rendering assertion -- the same silent-failure shape check_xml_comment_double_hyphen.py
exists to catch statically instead of via a live crash discovered by whoever next happens to run
the affected test. Unlike that checker's hard XML syntax error, this is not illegal XML (lxml
parses it fine) -- it is a real Odoo-core footgun this checker catches by structure, not by
syntax validity.

The fix is always the same: move the comment to before the `<xpath>` element entirely (or
inside one specific `<attribute>` element's own text, which this checker does not flag --
only a comment interspersed as a direct SIBLING of `<attribute>` elements is the trap).

Usage: check_xml_attribute_position_comment.py <repo_root>
"""

import os
import sys

from lxml import etree

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "radae"}
_HAMS_SHARED_TOOLS_SUFFIX = os.path.join("hams_shared", "tools")


def _resolve_repo_root(given_path):
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """Scan both hams_open and hams_com, same reasoning as
    check_xml_comment_double_hyphen.py's own identical helper -- real Odoo XML view files
    exist in both, and run_linters.py's own dir_path is only ever one of them."""
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


def _is_hams_shared_tools(path):
    normalized = os.path.normpath(path)
    return normalized == _HAMS_SHARED_TOOLS_SUFFIX or normalized.endswith(
        os.sep + _HAMS_SHARED_TOOLS_SUFFIX
    )


def check_xml_attribute_position_comment(repo_root):
    violations = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [
            d
            for d in dirs
            if d not in SKIP_DIRS
            and not d.startswith(".")
            and not (d == "tools" and _is_hams_shared_tools(os.path.join(root, d)))
        ]
        for filename in files:
            if not filename.endswith(".xml"):
                continue
            path = os.path.join(root, filename)
            try:
                tree = etree.parse(path)
            except etree.XMLSyntaxError:
                # A malformed file is check_xml_comment_double_hyphen.py's (or lxml's own
                # module-load error's) job to report, not this checker's -- skip quietly
                # rather than double-reporting the same broken file two different ways.
                continue
            except OSError as e:
                print(f"Warning: could not read {path}: {e}")
                continue

            for xpath_el in tree.iter("xpath"):
                if xpath_el.get("position") != "attributes":
                    continue
                for child in xpath_el:
                    if isinstance(child, etree._Comment):  # burn-ignore-introspection
                        violations.append(
                            f"{os.path.relpath(path, repo_root)}:{xpath_el.sourceline} "
                            f"An XML comment is a direct child of this "
                            f"<xpath position=\"attributes\"> block, interspersed among its "
                            f"<attribute> elements. This parses fine but is a known Odoo-core "
                            f"silent-failure trap: it makes EVERY <attribute> in this block "
                            f"render as absent, with no error and no warning anywhere. Move "
                            f"the comment to before the <xpath> element entirely instead."
                        )
                        break
    return violations


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_xml_attribute_position_comment.py <repo_root>")
        sys.exit(1)

    violations = []
    for repo_root in _resolve_repo_roots(sys.argv[1]):
        violations.extend(check_xml_attribute_position_comment(repo_root))

    if violations:
        print("❌ XML <xpath position=\"attributes\"> comment-placement violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)

    print("✅ No XML attribute-position comment-placement violations found.")
    sys.exit(0)
