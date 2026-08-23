#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
SELF_WRITEABLE_FIELDS Write-Proof Verifier
-------------------------------------------
MASTER_10 (Identity & Access Control) mandates that any module extending
res.users' SELF_WRITEABLE_FIELDS property must be verified by a test that
actually performs the self-write and asserts it took effect -- not a
test that merely inspects the returned field list. The bug this policy
exists to prevent: several modules once overrode a method Odoo core
never calls at all (_get_writeable_fields instead of the real
SELF_WRITEABLE_FIELDS property), and their tests still "passed" because
they only checked that a field NAME appeared in the override's return
value, never that a non-admin user could actually write it. The dead
override and the passing-but-hollow test shipped together for years.

This script, for every `def SELF_WRITEABLE_FIELDS` property override:
  1. Requires a `# Verified by [@ANCHOR: <name>]` comment on or
     immediately above the property definition.
  2. Requires a matching `# Tests [@ANCHOR: <name>]` comment on some
     test method, in that same module's own tests/ directory.
  3. Deep-AST-verifies that test method's body actually contains the
     required shape: a `.write(` call, a `.with_user(` call (proving
     identity was switched to a specific, non-default user), and an
     assertion call textually after the write (assertEqual/assertTrue/
     assertFalse/assertIn) -- proving the test checked the write
     actually landed, not just that it didn't raise.
Anything short of that shape is reported as an error, the same style as
check_burn_list.py's existing bypass-testing Deep AST Verification.
"""

import ast
import os
import re
import sys

ANCHOR_VERIFIED_RE = re.compile(r"#\s*Verified by \[@ANCHOR:\s*([A-Za-z0-9_:]+)\]")
ANCHOR_TESTS_RE = re.compile(r"#\s*Tests \[@ANCHOR:\s*([A-Za-z0-9_:]+)\]")
# This codebase's anchor convention has two forms in active use:
#   simple:   source "# Verified by [@ANCHOR: X]"  <->  test "# Tests [@ANCHOR: X]"
#   elaborate: source "[@ANCHOR: A]" + "# Verified by [@ANCHOR: B]"
#              <-> test "# Tests [@ANCHOR: A]" + "[@ANCHOR: B]" (bare)
# In the elaborate form, the test side's link to the source's "Verified
# by" target (B) is a BARE anchor declaration, not a "# Tests" line -- so
# a bare `[@ANCHOR: X]` counts as a match too, provided it isn't itself
# the "Verified by" or "Tests" line already captured above.
ANCHOR_BARE_RE = re.compile(r"\[@ANCHOR:\s*([A-Za-z0-9_:]+)\]")
WRITE_ASSERTIONS = {
    "assertEqual",
    "assertTrue",
    "assertFalse",
    "assertIn",
    "assertNotEqual",
}


def _owning_module(path, repo_root):
    d = os.path.dirname(path)
    while d and d != repo_root and os.path.dirname(d) != d:
        if os.path.isfile(os.path.join(d, "__manifest__.py")):
            return d
        d = os.path.dirname(d)
    return None


def _find_self_writeable_overrides(repo_roots):
    """Yields (py_path, anchor_or_None, def_lineno) for every
    `def SELF_WRITEABLE_FIELDS` found outside a tests/ directory."""
    for repo_root in repo_roots:
        for root, dirs, files in os.walk(repo_root):
            if "radae" in dirs:
                dirs.remove("radae")
            dirs[:] = [
                d
                for d in dirs
                if d not in ("node_modules", "__pycache__", ".git", "tools", "daemons")
            ]
            if os.sep + "tests" + os.sep in root + os.sep or root.endswith("tests"):
                continue
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        content = fh.read()
                except OSError:
                    continue
                if "def SELF_WRITEABLE_FIELDS" not in content:
                    continue
                lines = content.splitlines()
                for i, line in enumerate(lines):
                    if "def SELF_WRITEABLE_FIELDS" not in line:
                        continue
                    anchor = None
                    # Look at the def line itself and the ~4 lines above/below
                    # (property definitions usually have the decorator, then
                    # def, then a "# Verified by" comment as the first body
                    # line, or the comment sits just above the decorator).
                    window = lines[max(0, i - 4): i + 5]
                    for w in window:
                        m = ANCHOR_VERIFIED_RE.search(w)
                        if m:
                            anchor = m.group(1)
                            break
                    yield path, anchor, i + 1


def _find_tests_anchor(module_dir, anchor):
    """Searches module_dir/tests for a `# Tests [@ANCHOR: anchor]` comment
    and returns (test_file_path, containing_function_ast_node, file_content)
    or (None, None, None) if not found."""
    tests_dir = os.path.join(module_dir, "tests")
    if not os.path.isdir(tests_dir):
        return None, None, None
    for root, dirs, files in os.walk(tests_dir):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue
            lines = content.splitlines()
            anchor_line = None
            for i, line in enumerate(lines, 1):
                m = ANCHOR_TESTS_RE.search(line) or ANCHOR_BARE_RE.search(line)
                if m and m.group(1) == anchor:
                    anchor_line = i
                    break
            if anchor_line is None:
                continue
            try:
                tree = ast.parse(content, filename=path)
            except SyntaxError:
                continue
            # Method-level anchor (most specific) wins if present.
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    start = getattr(node, "lineno", 0)
                    end = getattr(node, "end_lineno", float("inf"))
                    if start <= anchor_line <= end:
                        return path, node, content
            # Fall back to a class-level anchor (e.g. right after the
            # `class Foo(...):` line, not inside any one method) -- the
            # write-proof shape then needs to be found somewhere in the
            # class as a whole, since ast.walk() on the ClassDef covers
            # every method's body.
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    start = getattr(node, "lineno", 0)
                    end = getattr(node, "end_lineno", float("inf"))
                    if start <= anchor_line <= end:
                        return path, node, content
    return None, None, None


def _verify_write_proof_shape(func_node):
    """Returns a list of missing-shape error strings (empty if the shape
    is satisfied)."""
    found_with_user = False
    write_lines = []
    assertion_lines = []

    # Two passes rather than one: ast.walk() is breadth-first, not source
    # order, so a write nested one level deeper (e.g. inside a `try:`)
    # can be visited AFTER a sibling assertion that is textually later in
    # the file but shallower in the tree. Comparing line numbers requires
    # collecting them all first, not comparing mid-walk.
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            attr = getattr(node.func, "attr", "")
            if attr == "write":
                write_lines.append(getattr(node, "lineno", 0))
            if attr == "with_user":
                found_with_user = True
            if attr in WRITE_ASSERTIONS:
                assertion_lines.append(getattr(node, "lineno", 0))

    found_write = bool(write_lines)
    assertion_after_write = found_write and any(
        a >= min(write_lines) for a in assertion_lines
    )

    errors = []
    if not found_write:
        errors.append("does not call .write(...) at all")
    if not found_with_user:
        errors.append(
            "does not call .with_user(...) -- must switch to a specific, "
            "non-default user to prove the SELF write works for someone "
            "other than the record's creator/admin context"
        )
    if found_write and not assertion_after_write:
        errors.append(
            "has a .write(...) call but no assertEqual/assertTrue/assertFalse/"
            "assertIn/assertNotEqual AFTER it -- must prove the write actually "
            "took effect, not just that it didn't raise"
        )
    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: check_self_writeable_field_tests.py <repo_root>")
        sys.exit(1)

    repo_root = os.path.abspath(sys.argv[1])
    roots = [repo_root]
    sibling_name = "hams_open" if os.path.basename(repo_root) != "hams_open" else "hams_com"
    sibling = os.path.abspath(os.path.join(repo_root, "..", sibling_name))
    if os.path.isdir(sibling) and any(
        os.path.isfile(os.path.join(sibling, d, "__manifest__.py"))
        for d in os.listdir(sibling)
        if os.path.isdir(os.path.join(sibling, d))
    ):
        roots.append(sibling)

    errors = 0
    for path, anchor, lineno in _find_self_writeable_overrides(roots):
        if not anchor:
            print(
                f"❌ ERROR: {path}:{lineno}: SELF_WRITEABLE_FIELDS override has no "
                "'# Verified by [@ANCHOR: <name>]' comment. See MASTER_10 "
                "Identity & Access Control, section 2."
            )
            errors += 1
            continue

        # _owning_module(path, repo_root) needs a real ancestor repo_root
        # to walk up to; only os.path.dirname(path) itself is known here,
        # so walk up directly instead of calling it with a repo_root that
        # would make the loop's `d != repo_root` check fail immediately.
        module_dir = None
        d = os.path.dirname(path)
        while d and os.path.dirname(d) != d:
            if os.path.isfile(os.path.join(d, "__manifest__.py")):
                module_dir = d
                break
            d = os.path.dirname(d)

        if not module_dir:
            print(f"❌ ERROR: {path}:{lineno}: could not resolve owning module for anchor lookup.")
            errors += 1
            continue

        test_path, func_node, _content = _find_tests_anchor(module_dir, anchor)
        if not func_node:
            print(
                f"❌ ERROR: {path}:{lineno}: anchor '{anchor}' has no matching "
                f"'# Tests [@ANCHOR: {anchor}]' comment in {module_dir}/tests/."
            )
            errors += 1
            continue

        shape_errors = _verify_write_proof_shape(func_node)
        if shape_errors:
            print(
                f"❌ ERROR: {test_path}:{func_node.lineno}: test '{func_node.name}' "
                f"(anchor '{anchor}') does not prove the self-write actually works:"
            )
            for e in shape_errors:
                print(f"    - {e}")
            errors += len(shape_errors)

    if errors:
        print(f"Total Errors (SELF_WRITEABLE_FIELDS Write Proof): {errors}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
