#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

import ast
import os
import sys


def _own_scope_nodes(node):
    """Yields every descendant of `node` that executes as part of `node`'s
    OWN control flow -- deliberately does NOT descend into a nested
    FunctionDef/AsyncFunctionDef/Lambda/ClassDef's own body, since code
    inside one of those only runs when that nested scope is later called/
    instantiated, not merely by being defined inside `node`. Mirrors
    check_function_test_anchors.py's own `_direct_functions` convention
    ("deliberately does NOT descend into a FunctionDef's own body to find
    closures nested [inside]"), applied here to call-detection instead of
    anchor-citation scanning.

    Without this, `ast.walk(node)` (which recurses into everything) counts
    a call inside a nested helper function's body as if it ran on `node`'s
    own path -- so a mutating `self.env.cr.execute(...)` in `node` followed
    only by an unused, never-invoked nested `def _helper(): notify_model_
    invalidation(...)` was silently treated as "invalidation present",
    missing the exact "raw SQL mutation with no real invalidation" case
    this checker exists to catch."""
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        yield child
        yield from _own_scope_nodes(child)


def check_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=filepath)

    errors = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            has_execute = False
            has_invalidation = False
            mutating_query = False

            for child in _own_scope_nodes(node):
                if isinstance(child, ast.Call):
                    # Check for self.env.cr.execute(...)
                    func_name = ""
                    if isinstance(child.func, ast.Attribute):
                        func_name = child.func.attr
                        if func_name == "execute":
                            # It's an execute call. Check if it's on env.cr
                            val = child.func.value
                            if isinstance(val, ast.Attribute) and val.attr == "cr":
                                has_execute = True
                                # Try to determine if query is mutating
                                if child.args and isinstance(
                                    child.args[0], ast.Constant
                                ):
                                    query = str(child.args[0].value).strip().upper()
                                    # CREATE [OR REPLACE] FUNCTION/PROCEDURE is
                                    # schema DDL that *defines* a stored
                                    # procedure's body -- it does not itself
                                    # mutate any table row when executed, only
                                    # when the defined procedure is later
                                    # CALLED/SELECTed elsewhere (a separate
                                    # cr.execute(), checked on its own merits).
                                    # Without this exclusion, a query like
                                    # "CREATE OR REPLACE FUNCTION
                                    # foo_upsert_batch(...) ... $$ ... $$"
                                    # trips the "UPSERT" substring heuristic
                                    # below purely because that word appears
                                    # in the function's own name inside the
                                    # DDL text, not because anything is being
                                    # mutated right now. Caught by two
                                    # real false positives in
                                    # ham_callbook/models/ham_au_register.py
                                    # and ham_callbook_procedures.py, both
                                    # pure `init()` DDL with no data mutation
                                    # of their own -- their actual callers
                                    # already correctly call
                                    # notify_model_invalidation() right after
                                    # invoking the procedure.
                                    is_ddl_definition = query.startswith(
                                        ("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION",
                                         "CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE")
                                    )
                                    if not is_ddl_definition:
                                        if any(
                                            query.startswith(kw)
                                            for kw in ["INSERT", "UPDATE", "DELETE"]
                                        ):
                                            mutating_query = True
                                        # Very heuristic for stored procedures
                                        if "UPSERT" in query or "INCREMENT" in query:
                                            mutating_query = True

                    # Check for notify_model_invalidation(...) or invalidate_model_cache(...)
                    if isinstance(child.func, ast.Name):
                        if child.func.id in [
                            "notify_model_invalidation",
                            "invalidate_model_cache",
                        ]:
                            has_invalidation = True

            if has_execute and mutating_query and not has_invalidation:
                errors.append(
                    f"{filepath}:{node.lineno} - Function '{node.name}' executes mutating raw SQL but is missing notify_model_invalidation()."
                )

    return errors


def main():
    search_dirs = sys.argv[1:]
    if not search_dirs:
        print("Usage: python3 check_cache_invalidation.py <dir1> <dir2>")
        sys.exit(1)

    all_errors = []
    for directory in search_dirs:
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith(".py") and "models" in root:
                    filepath = os.path.join(root, file)
                    try:
                        all_errors.extend(check_file(filepath))
                    except (SyntaxError, OSError):
                        pass  # Ignore syntax errors in unsupported files

    if all_errors:
        print("CRITICAL: Found raw SQL mutations without cache invalidations:")
        for error in all_errors:
            print(error)
        sys.exit(1)
    else:
        print("Cache invalidation linter passed successfully.")
        sys.exit(0)


if __name__ == "__main__":
    main()
