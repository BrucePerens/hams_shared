#!/usr/bin/env python3
import ast
import os
import sys

def check_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=filepath)

    errors = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            has_execute = False
            has_invalidation = False
            mutating_query = False

            for child in ast.walk(node):
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
                                if child.args and isinstance(child.args[0], ast.Constant):
                                    query = str(child.args[0].value).strip().upper()
                                    if any(query.startswith(kw) for kw in ["INSERT", "UPDATE", "DELETE"]):
                                        mutating_query = True
                                    # Very heuristic for stored procedures
                                    if "UPSERT" in query or "INCREMENT" in query:
                                        mutating_query = True

                    # Check for notify_model_invalidation(...) or invalidate_model_cache(...)
                    if isinstance(child.func, ast.Name):
                        if child.func.id in ["notify_model_invalidation", "invalidate_model_cache"]:
                            has_invalidation = True

            if has_execute and mutating_query and not has_invalidation:
                errors.append(f"{filepath}:{node.lineno} - Function '{node.name}' executes mutating raw SQL but is missing notify_model_invalidation().")

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
                    except Exception:
                        pass # Ignore syntax errors in unsupported files

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
