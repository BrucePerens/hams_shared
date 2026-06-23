#!/usr/bin/env python3
"""
Odoo Test Tag Enforcer
----------------------
Parses the AST of all test files to ensure that test classes are properly
decorated with @tagged("post_install", "-at_install"). This prevents tests
that rely on fully committed database states (like zero_sudo service accounts)
from crashing during the at_install phase.
"""

import ast
import os
import sys
from pathlib import Path

def check_test_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Guardrail Preservation Mandate
    if "# burn-ignore-test-tags" in content:
        return True

    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError as e:
        print(f"❌ Syntax error in {filepath}: {e}")
        return False

    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # Odoo test classes standardly start with 'Test'
            if not node.name.startswith("Test"):
                continue

            has_tagged = False
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "tagged":
                    args = []
                    for arg in decorator.args:
                        # Python 3.8+ uses ast.Constant
                        if isinstance(arg, ast.Constant):
                            args.append(arg.value)
                        # Fallback for older python versions
                        elif getattr(ast, "Str", None) and isinstance(arg, ast.Str):
                            args.append(arg.s)

                    if "post_install" in args and "-at_install" in args:
                        has_tagged = True
                        break

            if not has_tagged:
                issues.append(f"{filepath}:{node.lineno} Class '{node.name}' is missing @tagged('post_install', '-at_install')")

    for issue in issues:
        print(f"🚨 TEST TAGGING VIOLATION: {issue}")

    return len(issues) == 0

def main():
    if len(sys.argv) < 2:
        print("Usage: check_test_tags.py <repository_root>")
        sys.exit(1)

    repo_root = sys.argv[1]
    all_passed = True

    for root, dirs, files in os.walk(repo_root):
        # Only process files inside a 'tests' directory
        if "tests" not in Path(root).parts:
            continue

        for file in files:
            if file.startswith("test_") and file.endswith(".py"):
                filepath = os.path.join(root, file)
                if not check_test_file(filepath):
                    all_passed = False

    if not all_passed:
        sys.exit(1)

if __name__ == "__main__":
    main()
