#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fail on Odoo module tests that Odoo never runs.

Odoo runs only the test classes that are subclasses of its own BaseCase (TransactionCase, HttpCase, ...) in a
`tests/` module that `tests/__init__.py` imports. Two kinds of test sit in a module's `tests/` and run nowhere,
and nothing fails when they break (hamcall_verify's address matcher test was one: it failed on main unnoticed):

  1. a `test_*.py` that `tests/__init__.py` (directly or through a sub-package) does not import;
  2. a class with `test*` methods that is a plain `unittest.TestCase` (or no TestCase at all): Odoo's tag
     selector drops every test that has no `test_tags`, and no lint step runs a module's tests/ either.
     Make it subclass `odoo.tests.common.BaseCase` with `@tagged("post_install", "-at_install")`.

Usage: check_unrun_odoo_tests.py <repository_root> [more_roots...]
"""

import ast
import os
import sys

import check_target
from tree_files import tree_walk

_IGNORE_DIRS = ("node_modules", "venv", "env", ".venv", "__pycache__", "target", "site-packages", ".claude", "tmp")


def _parse(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _reached(tests_dir):
    """Absolute paths of the .py files reached by `from . import x` / `from .x import y` from tests/__init__.py."""
    seen = set()

    def visit(init_path, package_dir):
        if init_path in seen or not os.path.isfile(init_path):
            return
        seen.add(init_path)
        for node in ast.walk(_parse(init_path)):
            if not (isinstance(node, ast.ImportFrom) and node.level >= 1):
                continue
            names = [alias.name for alias in node.names] if not node.module else [node.module.split(".")[0]]
            for name in names:
                if os.path.isfile(os.path.join(package_dir, name + ".py")):
                    seen.add(os.path.join(package_dir, name + ".py"))
                else:
                    visit(os.path.join(package_dir, name, "__init__.py"), os.path.join(package_dir, name))

    visit(os.path.join(tests_dir, "__init__.py"), tests_dir)
    return seen


def _unittest_names(tree):
    """Local names bound to unittest's TestCase, so a bare `TestCase` is told from Odoo's classes."""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "unittest":
            names |= {alias.asname or alias.name for alias in node.names if alias.name == "TestCase"}
    return names


def plain_test_classes(tree):
    """[(line, name)] of the classes in `tree` with test* methods whose bases are unittest.TestCase or nothing."""
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    unittest_names = _unittest_names(tree)
    used_as_base = {ast.unparse(base) for node in classes.values() for base in node.bases}

    def is_plain(node, depth=0):
        bases = [ast.unparse(base) for base in node.bases]
        if not bases:
            return True
        resolved = []
        for base in bases:
            if base == "unittest.TestCase" or base in unittest_names:
                resolved.append(True)
            elif base in classes and depth < 10:
                resolved.append(is_plain(classes[base], depth + 1))
            else:
                resolved.append(False)  # an Odoo case, or a class from another file: assume Odoo runs it
        if any(base == "unittest.TestCase" or base in unittest_names for base in bases):
            return not any(
                not value and not (base == "unittest.TestCase" or base in unittest_names)
                and base.rsplit(".", 1)[-1].endswith("Case")
                for base, value in zip(bases, resolved)
            )
        return all(resolved)

    found = []
    for name, node in classes.items():
        has_tests = any(isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test") for m in node.body)
        if has_tests and not name.startswith("_") and name not in used_as_base and is_plain(node):
            found.append((node.lineno, name))
    return sorted(found)


def check_module_tests(tests_dir):
    """Findings (strings) for one module's tests/ directory."""
    reached = _reached(tests_dir)
    findings = []
    for current, _dirs, files in sorted(os.walk(tests_dir)):
        if "__pycache__" in current:
            continue
        for name in sorted(files):
            path = os.path.join(current, name)
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            if path not in reached:
                findings.append(f"{path}: not imported by tests/__init__.py, so Odoo never runs it")
            try:
                tree = _parse(path)
            except SyntaxError as error:
                findings.append(f"{path}: syntax error {error}")
                continue
            for line, cls in plain_test_classes(tree):
                findings.append(
                    f"{path}:{line} class {cls} is a plain unittest.TestCase: Odoo skips it; subclass "
                    "odoo.tests.common.BaseCase and add @tagged('post_install', '-at_install')"
                )
    return findings


def main():
    if len(sys.argv) < 2:
        print("Usage: check_unrun_odoo_tests.py <repository_root> [more_roots...]")
        sys.exit(1)
    findings = []
    modules = 0
    for arg in sys.argv[1:]:
        root = check_target.require_directory(arg, "check_unrun_odoo_tests")
        for current, _dirs, files in tree_walk(root, "__manifest__.py", _IGNORE_DIRS, untracked=True):
            if "__manifest__.py" not in files:
                continue
            modules += 1
            tests_dir = os.path.join(current, "tests")
            if os.path.isdir(tests_dir):
                findings.extend(check_module_tests(tests_dir))
    check_target.require_scanned(modules, ", ".join(sys.argv[1:]), "check_unrun_odoo_tests", "Odoo modules (__manifest__.py)")
    for finding in findings:
        print(f"🚨 UNRUN TEST: {finding}")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
