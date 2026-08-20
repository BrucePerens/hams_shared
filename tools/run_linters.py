#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

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
    allowed_symlinks = {
        "hams_shared",
        "tools",
        "docs",
        "AGENTS.md",
        "agents",
        ".agents",
    }
    symlinks_found = [
        f
        for f in os.listdir(dir_path)
        if os.path.islink(os.path.join(dir_path, f)) and f not in allowed_symlinks
    ]
    if symlinks_found:
        print(
            "================================================================================"
        )
        print("🚨 CRITICAL ARCHITECTURE WARNING: NO SYMLINKING 🚨")
        print("Symbolic links detected in the repository root:")
        for s in symlinks_found:
            print(f" - {s}")
        print(
            "This is an ANTI-PATTERN. You are strictly forbidden from symlinking modules"
        )
        print(
            "(e.g., zero_sudo, distributed_redis_cache) from hams_open into hams_com."
        )
        print("You MUST configure and rely on the Odoo --addons-path correctly.")
        print(
            "================================================================================"
        )
        sys.exit(1)

    # 2. Child Directory Mandate
    child_community = os.path.join(dir_path, "hams_open")
    if os.path.isdir(child_community):
        print(
            "================================================================================"
        )
        print("🚨 CRITICAL REPOSITORY STRUCTURE WARNING 🚨")
        print(
            f"hams_open was found as a CHILD of the current repository: {child_community}"
        )
        print("This is an ANTI-PATTERN. hams_open MUST be a SIBLING directory instead.")
        print(
            f"Please move it to: {os.path.abspath(os.path.join(dir_path, '..', 'hams_open'))}"
        )
        print(
            "================================================================================"
        )
        sys.exit(1)

    # 3. Resolve Sibling Dependency
    community_dir = None
    if not os.path.exists(os.path.join(dir_path, "zero_sudo", "__manifest__.py")):
        sibling_community = os.path.abspath(os.path.join(dir_path, "..", "hams_open"))
        if os.path.isdir(sibling_community):
            community_dir = sibling_community

    addons_paths = ["/usr/lib/python3/dist-packages/odoo/addons", dir_path, os.path.abspath(os.path.join(dir_path, "..", "hams_com"))]
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
            if os.path.isdir(mod_path) and os.path.isfile(
                os.path.join(mod_path, "__manifest__.py")
            ):
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
            "-m",
            mod_path,
            "--addons-path",
            addons_path_str,
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
    
    targets = [os.path.join(dir_path, m) for m in mod_array] if target_modules_str else [dir_path]

    try:
        res = subprocess.run(
            [
                flake8_cmd,
                *targets,
                "--exclude=venv,env,.venv,__pycache__,node_modules,target,daemons",
                "--select=E9,F,E402",
                "--per-file-ignores=__init__.py:F401",
            ],
            capture_output=True,
            text=True,
        )

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
    res = subprocess.run(
        [python_exec, os.path.join(dir_path, "tools", "check_burn_list.py")] + targets,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 9. verify_anchors
    res = subprocess.run(
        [python_exec, os.path.join(dir_path, "tools", "verify_anchors.py")] + targets,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 10. check_manifest_dependencies
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_manifest_dependencies.py"),
        ] + targets,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 11. check_js_syntax
    res = subprocess.run(
        [python_exec, os.path.join(dir_path, "tools", "check_js_syntax.py")] + targets,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 12. check_test_tags
    res = subprocess.run(
        [python_exec, os.path.join(dir_path, "tools", "check_test_tags.py")] + targets,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 13. check_absolute_paths
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_absolute_paths.py"),
        ] + targets,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 14. check_rabbitmq_pool
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_rabbitmq_pool.py"),
        ] + targets,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 15. check_shebang
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_shebang.py"),
        ] + targets,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 16. check_summation_bias
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_summation_bias.py"),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 17. check_skill_integrity
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_skill_integrity.py"),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 18. check_init_imports
    res = subprocess.run(
        [python_exec, os.path.join(dir_path, "tools", "check_init_imports.py")]
        + targets,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 19. check_dependency_cycles
    # Always scans the full repo (not the possibly-scoped `targets`) --
    # a cycle is a property of the whole manifest graph, not any single
    # module, and staying scoped could hide a cycle introduced by a
    # module outside the current target list.
    res = subprocess.run(
        [python_exec, os.path.join(dir_path, "tools", "check_dependency_cycles.py"), dir_path],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 20. check_self_writeable_field_tests
    # Same reasoning as step 19: this is a property of the whole repo's
    # anchor graph (a SELF_WRITEABLE_FIELDS override in one module can be
    # tested from that module's own tests/ dir only), not the possibly-
    # scoped `targets`.
    res = subprocess.run(
        [python_exec, os.path.join(dir_path, "tools", "check_self_writeable_field_tests.py"), dir_path],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 21. ESLint (JS-side analogue of flake8, config shared from hams_shared/)
    # Flat config restricts scanning to the config's own directory tree, so
    # this must run with cwd set to the common parent of hams_com and
    # hams_open -- dir_path's parent, per this repo's established sibling
    # layout -- rather than dir_path itself.
    eslint_bin = os.path.join(dir_path, "hams_shared", "node_modules", ".bin", "eslint")
    eslint_config = os.path.join(dir_path, "hams_shared", "eslint.config.js")
    workspace_root = os.path.abspath(os.path.join(dir_path, ".."))
    if os.path.isfile(eslint_bin):
        res = subprocess.run(
            [
                eslint_bin,
                "--config",
                eslint_config,
                "--no-error-on-unmatched-pattern",
            ] + targets,
            capture_output=True,
            text=True,
            cwd=workspace_root,
        )
        if res.returncode != 0:
            print("❌ ESLint Violations:")
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")
            linters_failed = True
    else:
        print(f"❌ ESLint executable not found at {eslint_bin} (run `npm install` in hams_shared/).")
        linters_failed = True

    # 22. check_model_extension_collisions
    # Same reasoning as step 19: a same-_name collision or a cross-module
    # _auto=False extension can be introduced by a module outside the
    # possibly-scoped `targets`, so this always scans the full repo.
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_model_extension_collisions.py"),
            dir_path,
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 23. check_untyped_utility_files (ODOO_AWARE_TYPE_CHECKING.md Phase 1)
    # Same reasoning as step 19/20/22: a plain-utility-file call-signature
    # bug can be introduced anywhere in daemons/ or ingest/, not just the
    # possibly-scoped `targets`, so this always scans the full repo's own
    # curated file set rather than `targets`.
    res = subprocess.run(
        [python_exec, os.path.join(dir_path, "tools", "check_untyped_utility_files.py"), dir_path],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True

    # 24. check_pip_audit (CODE_REVIEW_PROCESS.md's Python supply-chain scan,
    # the direct parallel to cargo-deny/cargo-audit on the Rust side).
    # Same reasoning as step 19/20/22/23: a vulnerable dependency can be
    # introduced by any requirements.txt in the repo, not just `targets`.
    res = subprocess.run(
        [python_exec, os.path.join(dir_path, "tools", "check_pip_audit.py"), dir_path],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True

    # 25. check_minified_js_nested_templates (rjsmin compatibility -- see
    # hams_com commit f1f00511 for the real bug this class of check was
    # written to catch). Same reasoning as step 19/20/22/23/24: a bundled
    # JS asset that gets re-minified by Odoo can be declared by any
    # module's manifest, not just the possibly-scoped `targets`, and the
    # vendored/sibling-repo asset it references may live outside
    # `dir_path` entirely, so this always scans the full repo and also
    # searches the sibling repo root for cross-repo asset references.
    sibling_dir = community_dir or os.path.abspath(os.path.join(dir_path, "..", "hams_com"))
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_minified_js_nested_templates.py"),
            dir_path,
            sibling_dir,
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if res.stdout:
            print(res.stdout, end="")
        if res.stderr:
            print(res.stderr, end="")
        linters_failed = True
    elif res.stdout and res.stdout.strip():
        print(res.stdout, end="")

    # 26. check_external_library_locality -- same reasoning as step 25: a
    # module vendoring its own copy of a library instead of using
    # "external" can be introduced anywhere in the repo, not just the
    # possibly-scoped `targets`, so this always scans the full repo.
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_external_library_locality.py"),
            dir_path,
        ],
        capture_output=True,
        text=True,
    )
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
