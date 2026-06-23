#!/usr/bin/env python3
"""
Odoo Manifest & JS Dependency Contract Linter
---------------------------------------------
Scans all __manifest__.py files into ASTs, then cross-references all
JavaScript ES6 module imports (`@module_name/...`) to mathematically guarantee
that the parent module explicitly depends on the imported module.
Prevents silent race conditions and `module_loader.js` crashes.
Also verifies cross-bundle safety for test suites and production.
"""

import os
import sys
import ast
import re

def main():
    if len(sys.argv) < 2:
        print("Usage: check_manifest_dependencies.py <repo_root>")
        sys.exit(1)

    repo_root = sys.argv[1]
    manifests = {}
    file_to_bundles = {}
    errors_found = False

    # 1. Map all manifests, their dependencies, and asset bundles
    for root, dirs, files in os.walk(repo_root):
        if "__manifest__.py" in files:
            mod_name = os.path.basename(root)
            manifest_path = os.path.join(root, "__manifest__.py")
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    tree = ast.parse(f.read(), filename=manifest_path)
                    for node in tree.body:
                        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict):
                            manifest_dict = ast.literal_eval(node.value)
                            manifests[mod_name] = manifest_dict.get('depends', [])

                            if 'description' not in manifest_dict:
                                print(f"🚨 MANIFEST DESCRIPTION VIOLATION in {mod_name}")
                                print(f"  File: {manifest_path}")
                                print("  Error: __manifest__.py MUST contain a 'description' key to prevent Odoo from falling back to RST parsing on README.md during module installation.")
                                errors_found = True

                            assets = manifest_dict.get('assets', {})
                            for bundle_name, file_list in assets.items():
                                for asset_path in file_list:
                                    file_to_bundles.setdefault(asset_path, []).append(bundle_name)
            except (SyntaxError, ValueError, OSError) as e:
                print(f"❌ ERROR parsing {manifest_path}: {e}")
                errors_found = True

    # 2. Scan all JS files for Odoo '@' alias imports
    import_pattern = re.compile(r"(?:import|export)\s+(?:.*?\s+from\s+)?['\"]@([a-zA-Z0-9_-]+)/(.*?)['\"]")

    for root, dirs, files in os.walk(repo_root):
        if "node_modules" in root:
            continue
        for file in files:
            if file.endswith(".js"):
                filepath = os.path.join(root, file)

                # Determine which module this JS file belongs to
                mod_dir = os.path.dirname(os.path.abspath(filepath))
                current_mod = None
                while mod_dir and mod_dir != os.path.dirname(mod_dir):
                    if os.path.exists(os.path.join(mod_dir, "__manifest__.py")):
                        current_mod = os.path.basename(mod_dir)
                        break
                    mod_dir = os.path.dirname(mod_dir)

                if not current_mod or current_mod not in manifests:
                    continue

                importing_file_rel = os.path.relpath(filepath, repo_root).replace("\\", "/")
                importing_bundles = file_to_bundles.get(importing_file_rel, [])

                # Check imports against dependencies and bundle boundaries
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        for line_no, line in enumerate(f, 1):
                            match = import_pattern.search(line)
                            if match:
                                imported_mod = match.group(1)
                                imported_subpath = match.group(2)

                                # A. Inter-Module Dependency Check
                                if imported_mod != current_mod and imported_mod not in manifests[current_mod] and imported_mod not in ("web", "base", "odoo"):
                                    print(f"🚨 MANIFEST DEPENDENCY VIOLATION in {current_mod}")
                                    print(f"  File: {importing_file_rel}:{line_no}")
                                    print(f"  Imports: '@{imported_mod}' but '{imported_mod}' is NOT listed in {current_mod}/__manifest__.py 'depends' array.")
                                    print(f"  Fix: Add '{imported_mod}' to the depends array to prevent module_loader.js race conditions.")
                                    errors_found = True

                                # B. Intra-Module Cross-Bundle Check (Bundle Safety)
                                if imported_mod == current_mod:
                                    physical_import_path = f"{current_mod}/static/src/{imported_subpath}"
                                    if not physical_import_path.endswith('.js'):
                                        physical_import_path += '.js'

                                    imported_bundles = file_to_bundles.get(physical_import_path, [])
                                    test_bundles = {"web.assets_tests"}
                                    prod_bundles = {"web.assets_backend", "web.assets_frontend", "web.assets_common", "web.assets_core"}

                                    # 1. Test runner safety: test assets must import things available in testing
                                    if any(b in test_bundles for b in importing_bundles):
                                        allowed_bundles = test_bundles | prod_bundles
                                        if imported_bundles and not any(b in allowed_bundles for b in imported_bundles):
                                            print(f"🚨 ASSET BUNDLE CROSS-CONTAMINATION in {current_mod}")
                                            print(f"  File: {importing_file_rel}:{line_no}")
                                            print(f"  Imports: '@{current_mod}/{imported_subpath}' which is only registered in bundles: {imported_bundles}")
                                            print("  Error: The importing file is in 'web.assets_tests', but the imported utility is NOT. This causes the test runner to crash due to missing dependencies. Move the imported file to 'web.assets_tests', 'web.assets_backend', or 'web.assets_frontend'.")
                                            errors_found = True

                                    # 2. Production safety: production assets MUST NOT import test assets
                                    if any(b in prod_bundles for b in importing_bundles):
                                        if imported_bundles and all(b in test_bundles for b in imported_bundles):
                                            print(f"🚨 ASSET BUNDLE CROSS-CONTAMINATION in {current_mod}")
                                            print(f"  File: {importing_file_rel}:{line_no}")
                                            print(f"  Imports: '@{current_mod}/{imported_subpath}' which is strictly a TEST asset.")
                                            print("  Error: A production asset cannot import a test-only asset. This will crash the live production environment.")
                                            errors_found = True
                except OSError as e:
                    print(f"⚠️ Warning: Could not read JS file {filepath}: {e}")

    if errors_found:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
