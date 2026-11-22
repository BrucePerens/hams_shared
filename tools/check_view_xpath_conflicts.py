#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Inheriting-view xpath conflict checker
-------------------------------------------------------------------------------------------
Round 138 failed its deploy because two views (ham_base and content_security_policy, both
`portal_docs_entry_no_inline_onclick`) inherited account_payment.portal_docs_entry and applied
the same `//button[@onclick]` attributes edit. The first removed the onclick, the second could
no longer find its target and the whole upgrade stopped.

For every active inheriting view (a <template inherit_id> or an ir.ui.view record with an
inherit_id) of both repos this reports, per parent view:

  (a) two views of different modules/ids editing the same xpath expression with
      position attributes/replace/inside where one invalidates the other: any pair with a
      replace, or an attributes pair that edits the same attribute name (or whose expression
      selects by an attribute the other edits). Two `inside` edits do not conflict.
  (b) a view whose expression selects by an attribute (`[@onclick]`) that another view of the
      same parent removes (an empty `<attribute name="onclick"/>`).
  (c) the same local xml id in different modules for the same parent and expression, both
      attributes/replace edits (a copy-pasted view).

Only `<xpath>` edits and the `<tag name=.. position=..>` shorthand are read. Usage:
check_view_xpath_conflicts.py <repo_root> [<repo_root>...]
"""

import os
import re
import sys

from lxml import etree

import check_target
from tree_files import tree_files

SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".claude", "vendor", "target", "radae"}
EDITING = {"attributes", "replace", "inside"}
_ATTR_SELECTOR = re.compile(r"@([\w:.-]+)")


def module_of(path, root):
    """Name of the Odoo module holding `path` (the nearest directory with a __manifest__.py)."""
    folder = os.path.dirname(path)
    while folder and folder.startswith(root) and folder != root:
        if os.path.exists(os.path.join(folder, "__manifest__.py")):
            return os.path.basename(folder)
        folder = os.path.dirname(folder)
    return None


def _qualified(ref, module):
    return ref if "." in ref else f"{module}.{ref}"


def _edit_of(node):
    """(expression, position, attribute edits) of an inheriting node, or None when it is not an edit."""
    position = node.get("position")
    if position is None and node.tag != "xpath":
        return None
    position = position or "inside"
    if node.tag == "xpath":
        expr = (node.get("expr") or "").strip()
    else:
        expr = f"//{node.tag}" + "".join(f"[@{k}='{v}']" for k, v in sorted(node.attrib.items()) if k != "position")
    attrs = {}
    if position == "attributes":
        for child in node.iter("attribute"):
            attrs[child.get("name")] = not (child.text or "").strip() and child.get("add") is None and child.get("remove") is None
    return expr, position, attrs


def _views(tree, module):
    """Yield (local id, parent, [edits]) for every active inheriting view of one parsed file."""
    for node in tree.getroot().iter("template", "record"):
        if node.tag == "template":
            parent, active, arch = node.get("inherit_id"), node.get("active"), node
        else:
            if node.get("model") != "ir.ui.view":
                continue
            parent = active = None
            arch = None
            for field in node.findall("field"):
                if field.get("name") == "inherit_id":
                    parent = field.get("ref")
                elif field.get("name") == "active":
                    active = (field.get("eval") or field.text or "").strip()
                elif field.get("name") == "arch":
                    arch = field
        if not parent or arch is None or (active or "").lower() in ("false", "0"):
            continue
        edits = [e for e in (_edit_of(n) for n in arch.iter() if n is not arch) if e and e[0]]
        yield node.get("id"), _qualified(parent, module), edits


def collect(roots, problems=None):
    """{parent: [(module, id, relpath, expr, position, attrs)]} for all roots. A file that cannot be parsed or read is described in
    the list `problems` when the caller passes one: its views were not checked."""
    by_parent = {}
    for root in roots:
        for path in tree_files(root, ".xml", SKIP_DIRS, skip_hidden=True, untracked=True):
            module = module_of(path, root)
            if module is None:
                continue
            try:
                tree = etree.parse(path)
            except (etree.XMLSyntaxError, OSError) as error:
                if problems is not None:
                    problems.append(f"{os.path.relpath(path, root)}: cannot be parsed or read ({type(error).__name__}): its view edits were not checked")
                continue
            rel = os.path.relpath(path, root)
            for view_id, parent, edits in _views(tree, module):
                for expr, position, attrs in edits:
                    by_parent.setdefault(parent, []).append((module, view_id, rel, expr, position, attrs))
    return by_parent


def _conflict(first, second):
    _m1, _i1, _r1, expr1, pos1, attrs1 = first
    _m2, _i2, _r2, expr2, pos2, attrs2 = second
    if expr1 != expr2 or pos1 not in EDITING or pos2 not in EDITING:
        return False
    if "replace" in (pos1, pos2):
        return True
    if pos1 == pos2 == "attributes":
        selected = set(_ATTR_SELECTOR.findall(expr1))
        return bool(set(attrs1) & set(attrs2) or selected & (set(attrs1) | set(attrs2)))
    return False


def check_conflicts(by_parent):
    violations = []
    for parent, edits in sorted(by_parent.items()):
        for index, first in enumerate(edits):
            for second in edits[index + 1:]:
                if (first[0], first[1]) == (second[0], second[1]):
                    continue
                where = f"{first[2]} ({first[0]}.{first[1]}) and {second[2]} ({second[0]}.{second[1]})"
                if first[1] == second[1] and first[0] != second[0] and first[3] == second[3] and first[4] in ("attributes", "replace") and second[4] in ("attributes", "replace"):
                    violations.append(f"(c) parent {parent}: same xml id `{first[1]}` in two modules for xpath `{first[3]}`: {where}")
                if _conflict(first, second):
                    violations.append(f"(a) parent {parent}: both edit `{first[3]}` ({first[4]}/{second[4]}), the first applied invalidates the second: {where}")
        for view in edits:
            for other in edits:
                if (view[0], view[1]) == (other[0], other[1]) or other[4] != "attributes":
                    continue
                removed = {name for name, empty in other[5].items() if empty}
                for name in set(_ATTR_SELECTOR.findall(view[3])) & removed:
                    violations.append(
                        f"(b) parent {parent}: {view[2]} ({view[0]}.{view[1]}) selects `{view[3]}` but {other[2]} ({other[0]}.{other[1]}) removes `{name}`"
                    )
    return violations


def check_view_xpath_conflicts(roots):
    problems = []
    by_parent = collect(roots, problems)
    return problems + check_conflicts(by_parent)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_view_xpath_conflicts.py <repo_root> [<repo_root>...]")
        sys.exit(2)
    scan_roots = [os.path.abspath(p) for p in sys.argv[1:]]
    for scan_root in scan_roots:
        check_target.require_directory(scan_root, "check_view_xpath_conflicts")
        check_target.require_scanned(
            len(tree_files(scan_root, ".xml", SKIP_DIRS, skip_hidden=True, untracked=True)), scan_root, "check_view_xpath_conflicts", "*.xml files"
        )
    found = check_view_xpath_conflicts(scan_roots)
    if found:
        print("❌ Inheriting views whose xpaths conflict (the second one cannot find its target on upgrade):")
        for line in found:
            print(f"  - {line}")
        sys.exit(1)
    print("✅ No conflicting inheriting-view xpaths found.")
    sys.exit(0)
