#!/usr/bin/env python3
import os
import sys
import ast


def get_imported_names(init_path):
    imported = set()
    try:
        with open(init_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=init_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.level == 1
                ):  # from . import my_model or from .my_model import my_func
                    if node.module:
                        imported.add(node.module.split(".")[0])
                    else:
                        for alias in node.names:
                            imported.add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
    except Exception as e:
        print(f"Error parsing {init_path}: {e}")
    return imported


def main():
    if len(sys.argv) < 2:
        print("Usage: check_init_imports.py <repo_root>")
        sys.exit(1)

    repo_root = sys.argv[1]
    ignore_files = {"__init__.py", "__manifest__.py", "__openerp__.py", "setup.py"}
    warnings = 0

    for root, dirs, files in os.walk(repo_root):
        if "radae" in dirs:
            dirs.remove("radae")
        dirs[:] = [d for d in dirs if d not in ('target', 'venv', '.venv', 'node_modules', 'daemons', 'test_env', '.git')]
        if "__init__.py" in files:
            init_path = os.path.join(root, "__init__.py")
            imported = get_imported_names(init_path)

            for file in files:
                if file.endswith(".py") and file not in ignore_files:
                    mod_name = file[:-3]
                    if mod_name not in imported:
                        print(
                            f"  ⚠️  WARNING: File '{file}' in '{root}' is never imported in its __init__.py"
                        )
                        warnings += 1

    if warnings > 0:
        print(f"Total Warnings (Init Imports): {warnings}")
    sys.exit(0)  # Warnings usually do not fail the build


if __name__ == "__main__":
    main()
