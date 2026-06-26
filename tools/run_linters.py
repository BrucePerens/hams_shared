#!/usr/bin/env python3
"""
Odoo DevSecOps Linter Orchestrator
----------------------------------
Replaces the legacy bash script to enforce the structural integrity
of the repository, including child-directory detection and anti-symlink rules.
"""

import os
import sys
import subprocess

def main():
    dir_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 1. Anti-Symlink Mandate
    symlinks_found = [
        f for f in os.listdir(dir_path)
        if os.path.islink(os.path.join(dir_path, f))
    ]
    if symlinks_found:
        print("================================================================================")
        print("🚨 CRITICAL ARCHITECTURE WARNING: NO SYMLINKING 🚨")
        print("Symbolic links detected in the repository root:")
        for s in symlinks_found:
            print(f" - {s}")
        print("This is an ANTI-PATTERN. You are strictly forbidden from symlinking modules")
        print("(e.g., zero_sudo, distributed_redis_cache) from hams_community into hams_com.")
        print("You MUST configure and rely on the Odoo --addons-path correctly.")
        print("================================================================================")
        sys.exit(1)

    # 2. Child Directory Mandate
    child_community = os.path.join(dir_path, "hams_community")
    if os.path.isdir(child_community):
        print("================================================================================")
        print("🚨 CRITICAL REPOSITORY STRUCTURE WARNING 🚨")
        print(f"hams_community was found as a CHILD of the current repository: {child_community}")
        print("This is an ANTI-PATTERN. hams_community MUST be a SIBLING directory instead.")
        print(f"Please move it to: {os.path.abspath(os.path.join(dir_path, '..', 'hams_community'))}")
        print("================================================================================")
        sys.exit(1)

    # 3. Resolve Sibling Dependency
    community_dir = None
    if not os.path.exists(os.path.join(dir_path, "zero_sudo", "__manifest__.py")):
        sibling_community = os.path.abspath(os.path.join(dir_path, "..", "hams_community"))
        if os.path.isdir(sibling_community):
            community_dir = sibling_community

    addons_paths = ["/usr/lib/python3/dist-packages/odoo/addons", dir_path]
    if community_dir:
        addons_paths.append(community_dir)
    addons_path_str = ",".join(addons_paths)

    python_exec = "/usr/bin/python3"
    linters_failed = False

    # 5. Modules Discovery
    target_modules_str = sys.argv[1] if len(sys.argv) > 1 else ""
    mod_array = []
    if not target_modules_str:
        for item in os.listdir(dir_path):
            mod_path = os.path.join(dir_path, item)
            if os.path.isdir(mod_path) and os.path.isfile(os.path.join(mod_path, "__manifest__.py")):
                mod_array.append(item)
    else:
        mod_array = [m.strip() for m in target_modules_str.split(",") if m.strip()]

    # 6. Pre-flight Checks
    for mod in mod_array:
        mod_path = os.path.join(dir_path, mod)
        if not os.path.isfile(os.path.join(mod_path, "__manifest__.py")):
            if community_dir:
                comm_mod_path = os.path.join(community_dir, mod)
                if os.path.isfile(os.path.join(comm_mod_path, "__manifest__.py")):
                    mod_path = comm_mod_path
                else:
                    continue
            else:
                continue

        pre_flight_cmd = [
            python_exec,
            os.path.join(dir_path, "tools", "pre_flight_check.py"),
            "-m", mod_path,
            "--addons-path", addons_path_str
        ]
        res = subprocess.run(pre_flight_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")
            linters_failed = True

    # 7. Flake8
    flake8_cmd = "/usr/bin/flake8"

    try:
        res = subprocess.run([
            flake8_cmd,
            dir_path,
            "--exclude=venv,env,.venv,__pycache__,node_modules",
            "--select=E9,F,E402",
            "--per-file-ignores=__init__.py:F401"
        ], capture_output=True, text=True)

        if res.returncode != 0:
            print("❌ Flake8 Violations:")
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")
            linters_failed = True
    except FileNotFoundError:
        print("❌ Flake8 executable not found.")
        linters_failed = True

    # 8. check_burn_list
    res = subprocess.run([
        python_exec,
        os.path.join(dir_path, "tools", "check_burn_list.py"),
        dir_path
    ], capture_output=True, text=True)
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 9. verify_anchors
    res = subprocess.run([
        python_exec,
        os.path.join(dir_path, "tools", "verify_anchors.py"),
        dir_path
    ], capture_output=True, text=True)
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 10. check_manifest_dependencies
    res = subprocess.run([
        python_exec,
        os.path.join(dir_path, "tools", "check_manifest_dependencies.py"),
        dir_path
    ], capture_output=True, text=True)
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 11. check_js_syntax
    res = subprocess.run([
        python_exec,
        os.path.join(dir_path, "tools", "check_js_syntax.py"),
        dir_path
    ], capture_output=True, text=True)
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 12. check_test_tags
    res = subprocess.run([
        python_exec,
        os.path.join(dir_path, "tools", "check_test_tags.py"),
        dir_path
    ], capture_output=True, text=True)
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    if linters_failed:
        print("\n🛑 Halting due to linter violations. Please review the output above.")
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()
