#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
XML `position="attributes"` Comment Checker
-------------------------------------------------------------------------------------------
A DEFENSIVE, STYLE-ONLY convention -- NOT a confirmed Odoo bug workaround.

History: originally written as a suspected Odoo QWeb inheritance trap, reported live
2026-09-28/29 while fixing `theme_hams/views/layout_overrides.xml`'s `hams_theme_attribute`
view. The belief was that an XML `<!-- -->` comment placed as a direct sibling of `<attribute>`
elements inside an `<xpath ... position="attributes">` block silently made EVERY `<attribute>`
in that block render as absent. A later, direct reproduction against the real, installed Odoo
19 `apply_inheritance_specs()` (`odoo/tools/template_inheritance.py`, 2026-10-02) DISPROVED
that: its `position == 'attributes'` branch iterates `spec.getiterator('attribute')`, which
filters by tag and simply skips a comment sibling wherever it sits. Every shape tried (comment
between two `<attribute>` siblings, comment before a lone `<attribute>`, both with
`inherit_branding=True`, and a two-view stacked-inheritance case) produced an identical,
fully-correct arch with or without the comment. The original "confirmed twice" observation was
most likely a misattribution confounded by an unrelated theme-copy-step timing gap. Full
write-up: hams_com commit 551db4c1 (PR #512) and its `night_shift_history.md` 2026-10-02
entry closing the parent to-do.

The checker is kept anyway because the structural shape it flags -- a comment as a direct
child of an `<xpath position="attributes">` element, i.e. interspersed among inheritance-spec
siblings -- is unusual and easy to avoid: explanatory comments belong before the `<xpath>`
element, where every other explanatory comment in this project's view files already lives.
Do not cite this checker as evidence of an Odoo-core bug, and do not file an upstream report
on its strength.

The fix is always the same: move the comment to before the `<xpath>` element entirely (or
inside one specific `<attribute>` element's own text, which this checker does not flag --
only a comment interspersed as a direct SIBLING of `<attribute>` elements is flagged).

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
                            "An XML comment is a direct child of this "
                            "<xpath position=\"attributes\"> block, interspersed among its "
                            "<attribute> elements. Odoo 19 tolerates this (verified "
                            "2026-10-02), but this project's style convention keeps "
                            "explanatory comments out of inheritance-spec blocks. Move "
                            "the comment to before the <xpath> element entirely instead."
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
