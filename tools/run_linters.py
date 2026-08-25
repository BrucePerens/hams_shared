#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Odoo DevSecOps Linter Orchestrator
----------------------------------
Replaces the legacy bash script to enforce the structural integrity
of the repository, including child-directory detection and anti-symlink rules.
"""

import glob
import os
import sys
import subprocess


def _resolve_repo_root(given_path):
    """`dir_path` (below) resolves to the hams_shared directory itself when this script is
    invoked from hams_shared directly -- AGENTS.md documents that as a third, intentionally valid
    invocation root (alongside hams_com and hams_open), but hams_shared has no Odoo addon modules
    of its own, so anything that needs a real repo root to scan (module discovery, `targets`, the
    sibling-repo/addons-path resolution used by pre_flight_check.py) must not use `dir_path`
    as-is in that case. Confirmed directly, not assumed: check_burn_list.py scanned only 3 files
    via this exact invocation before this fix, versus 78+ real files against a real repo root --
    every `targets`-based step (7-18, 21) was silently scanning almost nothing. Same fix as the 9
    individual checker scripts this same bug was found and fixed in earlier tonight (see
    docs/proposals/LINTER_POLICY_REVISIT.md) -- detect the hams_shared case by name and redirect
    to its real parent repo. Deliberately NOT used for steps 27-29 (the tools/scripts/daemons
    test-suite runners) or the anti-symlink/child-directory structural checks just below: those
    need `dir_path` to genuinely BE hams_shared (or resolve through a symlink to it) to find their
    own tooling correctly, and redirecting it there would scan the wrong daemons/ tree entirely
    (hams_open has its own separate, real daemons/ directory, unrelated to hams_shared's)."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def main():
    dir_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_root = _resolve_repo_root(dir_path)

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
    if not os.path.exists(os.path.join(repo_root, "zero_sudo", "__manifest__.py")):
        sibling_community = os.path.abspath(os.path.join(repo_root, "..", "hams_open"))
        if os.path.isdir(sibling_community):
            community_dir = sibling_community

    addons_paths = ["/usr/lib/python3/dist-packages/odoo/addons", repo_root, os.path.abspath(os.path.join(repo_root, "..", "hams_com"))]
    if community_dir:
        addons_paths.append(community_dir)
    addons_path_str = ",".join(addons_paths)

    python_exec = "/usr/bin/python3"
    linters_failed = False

    # 5. Modules Discovery
    target_modules_str = sys.argv[1] if len(sys.argv) > 1 else ""
    mod_array = []
    if not target_modules_str:
        for item in os.listdir(repo_root):
            mod_path = os.path.join(repo_root, item)
            if os.path.isdir(mod_path) and os.path.isfile(
                os.path.join(mod_path, "__manifest__.py")
            ):
                mod_array.append(item)
    else:
        mod_array = [m.strip() for m in target_modules_str.split(",") if m.strip()]

    # 6. Pre-flight Checks
    for mod in mod_array:
        mod_path = os.path.join(repo_root, mod)
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
    
    targets = [os.path.join(repo_root, m) for m in mod_array] if target_modules_str else [repo_root]

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
    # hams_open (the workspace root).
    # eslint and eslint.config.js live under the REAL hams_shared directory, which is not
    # necessarily `dir_path` -- `dir_path` is whatever repo run_linters.py was invoked from
    # (hams_com, hams_open, or hams_shared itself, all three valid per AGENTS.md), and only ONE
    # of those three literally IS hams_shared. An earlier version of this comment/code assumed
    # dir_path always equals hams_shared, which broke ESLint entirely (hard, unconditional
    # linters_failed=True) for the other two -- the far more common -- invocation modes:
    # confirmed directly, `dir_path/node_modules/.bin/eslint` does not exist when dir_path is a
    # real repo root, only when dir_path is hams_shared itself. Fixed by resolving the REAL
    # hams_shared directory via realpath() (which follows the hams_open/tools and hams_com/tools
    # symlinks, unlike a dirname chain off `dir_path`), same technique as the fix to the same bug
    # class in test_odoo_mypy_plugin.py the same night -- correct regardless of which of the
    # three valid roots this script was invoked from.
    _real_tools_dir = os.path.dirname(os.path.realpath(__file__))
    hams_shared_dir = os.path.dirname(_real_tools_dir)
    eslint_bin = os.path.join(hams_shared_dir, "node_modules", ".bin", "eslint")
    eslint_config = os.path.join(hams_shared_dir, "eslint.config.js")
    workspace_root = os.path.dirname(os.path.dirname(hams_shared_dir))
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
    sibling_dir = community_dir or os.path.abspath(os.path.join(repo_root, "..", "hams_com"))
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

    # 27. hams_shared/tools/ unit test suites -- the test_check_*.py files
    # verifying the checker scripts' OWN logic (test_check_burn_list.py,
    # test_check_dependency_cycles.py, etc.), not the checker scripts
    # themselves (steps 8/10/19/20/22/23/25/26 above already run those
    # against real repo content). Without this step, nothing ever ran
    # these suites at all -- confirmed directly, not assumed, before
    # adding it. A bare `test_*.py` glob with an explicit exclusion set,
    # not a narrower `test_check_*.py` glob: this directory's suite names
    # follow the checker script they cover (test_infrastructure.py,
    # test_run_linters.py, etc.), not all of them start with "check_", so
    # a `test_check_*` glob would silently skip most future suites -- the
    # exact failure this step exists to prevent, recurring by naming
    # accident. tools/test.py, tools/test_cf.py, and tools/test_mcp_server.py
    # all also match a bare `test_*.py` glob, but are Odoo test-runner/
    # MCP-server launcher scripts, not suites -- collecting them would
    # execute their own module-level Odoo-launching code instead of
    # running unit tests, so they're the ones excluded by name. Always the
    # full glob, run once, not scoped to `targets`: a checker's own logic
    # bug isn't a property of any one target module.
    _RUNNER_SCRIPTS_NOT_SUITES = {"test.py", "test_cf.py", "test_mcp_server.py"}
    tool_test_files = sorted(
        f
        for f in glob.glob(os.path.join(dir_path, "tools", "test_*.py"))
        if os.path.basename(f) not in _RUNNER_SCRIPTS_NOT_SUITES
    )
    if tool_test_files:
        res = subprocess.run(
            [python_exec, "-m", "pytest", "-q"] + tool_test_files,
            capture_output=True,
            text=True,
            cwd=os.path.join(dir_path, "tools"),
        )
        if res.returncode != 0:
            print("❌ hams_shared/tools/ unit test failures:")
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")
            linters_failed = True
        elif res.stdout and res.stdout.strip():
            print(res.stdout, end="")

    # 28. hams_shared/scripts/ unit test suites -- same reasoning as step 27
    # above, for the one other directory of standalone dev-tooling scripts
    # this repo has. Found while adding test_run_headless_chrome.py: without
    # this step, nothing would ever run it (or any future scripts/test_*.py
    # suite) automatically either, the exact gap step 27 already closed for
    # tools/.
    scripts_test_files = sorted(glob.glob(os.path.join(dir_path, "scripts", "test_*.py")))
    if scripts_test_files:
        res = subprocess.run(
            [python_exec, "-m", "pytest", "-q"] + scripts_test_files,
            capture_output=True,
            text=True,
            cwd=os.path.join(dir_path, "scripts"),
        )
        if res.returncode != 0:
            print("❌ hams_shared/scripts/ unit test failures:")
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")
            linters_failed = True
        elif res.stdout and res.stdout.strip():
            print(res.stdout, end="")

    # 29. daemons/ standalone-daemon unit test suites -- same reasoning as
    # steps 27/28, for the one other class of directory this repo has full
    # of test_*.py files nothing ever runs automatically. Unlike tools/ and
    # scripts/ (one shared directory, one shared cwd), daemons/*/test_*.py
    # each need their OWN daemon directory as cwd (for a bare `import main`
    # or `import <daemon>_sync` to resolve), plus PYTHONPATH pointed at
    # daemons/ itself (for `import hams_config`, the shared RPC/download
    # helper every sync daemon imports) -- so this runs pytest once per
    # daemon directory rather than batching every file into one invocation
    # the way steps 27/28 do. Confirmed directly, not assumed, that nothing
    # else runs these: this repo's daemons/ is excluded by name from
    # check_burn_list.py's own directory walk (see
    # docs/proposals/LINTER_POLICY_REVISIT.md), and grepping run_linters.py
    # itself before this step found no other test_*.py discovery under
    # daemons/ at any level. ODOO_URL/DB_NAME are required, no-fallback
    # module-level config in hams_config.py and most daemons' own main.py
    # (this codebase's fail-fast policy) -- every test file already sets
    # dummy values via os.environ.setdefault() before importing, but they're
    # also set here so a test file that forgets to wouldn't silently import-
    # crash this whole step instead of failing its own assertions.
    daemon_test_files = sorted(
        glob.glob(os.path.join(dir_path, "daemons", "test_*.py"))
        + glob.glob(os.path.join(dir_path, "daemons", "*", "test_*.py"))
        + glob.glob(os.path.join(dir_path, "daemons", "*", "tools", "test_*.py"))
        + glob.glob(os.path.join(dir_path, "daemons", "*", "tests", "test_*.py"))
    )
    daemon_test_dirs = sorted({os.path.dirname(f) for f in daemon_test_files})
    daemons_root = os.path.join(dir_path, "daemons")
    daemon_env = dict(os.environ)
    daemon_env.setdefault("ODOO_URL", "http://test-odoo.invalid:8069")
    daemon_env.setdefault("DB_NAME", "hams_test")
    # pdns_sync and qrz_scraper's own main.py read these at import time with
    # no fallback -- the exact same dummy defaults provision_environment()
    # already sets via env_vars.setdefault() elsewhere in this file, so this
    # step is self-sufficient rather than depending on inheriting them from
    # a fully-provisioned parent environment.
    daemon_env.setdefault("PDNS_API_URL", "http://powerdns:8081/api/v1/servers/localhost/zones")
    daemon_env.setdefault("PDNS_API_KEY", "secret")
    daemon_env.setdefault("RMQ_PORT", "5672")
    daemon_env.setdefault("RMQ_USER", "guest")
    daemon_env.setdefault("RMQ_PASS", "guest")
    daemon_env["PYTHONPATH"] = daemons_root + os.pathsep + daemon_env.get("PYTHONPATH", "")

    def _venv_python_for(start_dir):
        # A daemon with its own requirements.txt (e.g. hams_simulated_bots,
        # whose deps -- aiortc, av, faster-whisper -- have no business being
        # installed system-wide) provisions a .venv in its own directory or
        # an ancestor between it and daemons_root. Prefer that interpreter
        # over the generic python_exec when one exists, so this step
        # actually exercises what that daemon runs against instead of
        # failing on an import nothing outside its own venv ever provides.
        current = start_dir
        while True:
            candidate = os.path.join(current, ".venv", "bin", "python3")
            if os.path.exists(candidate):
                return candidate
            if current == daemons_root or len(current) <= len(daemons_root):
                return python_exec
            current = os.path.dirname(current)

    for test_dir in daemon_test_dirs:
        files_here = sorted(f for f in daemon_test_files if os.path.dirname(f) == test_dir)
        # A test_dir one level below its daemon (tools/, tests/) imports its
        # daemon's own main module by bare name (e.g. hams_simulated_bots/
        # tests/test_main.py's `from main import ...`) -- that only
        # resolves with the daemon's own directory on the path too, not
        # just daemons_root.
        this_env = dict(daemon_env)
        this_env["PYTHONPATH"] = os.path.dirname(test_dir) + os.pathsep + this_env["PYTHONPATH"]
        res = subprocess.run(
            [_venv_python_for(test_dir), "-m", "pytest", "-q"] + files_here,
            capture_output=True,
            text=True,
            cwd=test_dir,
            env=this_env,
        )
        if res.returncode != 0:
            print(f"❌ {os.path.relpath(test_dir, dir_path)} unit test failures:")
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")
            linters_failed = True
        elif res.stdout and res.stdout.strip():
            print(res.stdout, end="")

    # 30. check_access_csv_group_order -- catches the exact bug class that
    # silently broke the ENTIRE test suite tonight (not just one module's
    # tests): ham_aprs/__manifest__.py listed security/ir.model.access.csv
    # before the XML file defining the group it references, and Odoo's
    # whole boot died at that module with nothing after it in the -i list
    # ever running. Same reasoning as step 19/20/22/23: a module's own
    # data-list ordering bug can be introduced anywhere, not just the
    # possibly-scoped `targets`, so this always scans the full repo.
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_access_csv_group_order.py"),
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

    # 31. check_module_subpackage_imports -- catches a second, real bug found tonight, the same
    # shape as step 30's: a new ham_propagation/models/ subpackage was added (a real, correct
    # _inherit extension of ham.sked) but ham_propagation/__init__.py only ever imported
    # `controllers`, so Odoo's module loader never imported the new file at all -- the new
    # method was genuinely correct Python sitting on disk, invisible to Odoo, and the real
    # failure mode (a view validation error naming the method as "not a valid action") gave no
    # hint the actual cause was a missing top-level import. Same reasoning as step 19/20/22/23/30:
    # always scans the full repo.
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_module_subpackage_imports.py"),
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

    # 32. check_registry_test_cr_usage -- catches a real, repeatedly-found bug: an
    # `is_test = vars(self.env.registry).get("test_cr") is not None` guard around
    # env.cr.commit() that is always False, since registry.test_cr isn't a real attribute
    # in this installed Odoo version. Found live in ham_relay_bridge (the guarded commit()
    # had literally never been exercised end-to-end by any test) and as the same latent,
    # not-yet-triggered bug in 10 places across user_websites. Same reasoning as
    # step 19/20/22/23/30/31: always scans the full repo.
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_registry_test_cr_usage.py"),
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

    # 33. check_xml_comment_double_hyphen -- catches a hard XML syntax error (a literal `--`
    # inside a <!-- --> comment) hit twice in one night writing explanatory security-rule
    # comments in an em-dash-style " -- " aside, both times only caught by a live module-load
    # crash during a real test run. Same reasoning as step 19/20/22/23/30/31/32: always scans
    # the full repo.
    res = subprocess.run(
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_xml_comment_double_hyphen.py"),
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
