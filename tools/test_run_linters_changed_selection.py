#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Which linter steps `run_linters.py --changed` selects for which kind of changed file.

A passing `--changed` run writes the pass stamp that devbox_tools/prmerge.sh requires, so the
selection has to be complete: every checker that can fail on a changed file must run. On
2026-10-29 a change that touched only daemons/ (Rust and Python) selected none of the per-module
checkers, a verify_anchors scoped to the changed module reported documentation that lived outside
it as missing, and the stamp was written all the same.

These tests run the real script (`--changed --list`, which applies the real selection and prints
the step names without running them) in a throwaway workspace laid out like production:

    <ws>/hams_open       git repo, `tools` symlinked to hams_shared/tools, two modules
    <ws>/hams_open/hams_shared   git repo holding a copy of this tools directory
    <ws>/hams_com        git repo with one module

Each test edits files in the work tree (an uncommitted change counts as changed) and asserts the
selected set.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))  # hams_shared/tools, also when run through a repository's `tools` symlink
SHARED = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import run_linters  # noqa: E402

# Steps whose checkers read only the file types they name; a change to a file of another type
# may leave them out. Every other step must run for a Python change under daemons/.
STEPS_THAT_CANNOT_READ_PYTHON = {
    "check_pip_audit",
    "check_cargo_deny",
    "check_cargo_clippy",
    "check_cargo_fmt",
    "check_rust_function_test_anchors",
    "check_async_lock_scope",
    "check_xml_comment_double_hyphen",
    "check_xml_attribute_position_comment",
    "check_owl_templates",
    "check_scss_math_functions",
    "check_window_fetch_reassignment",
    "check_js_syntax",
    "check_js_test_hook_gating",
    "check_js_function_test_anchors",
    "check_minified_js_nested_templates",
    "check_minified_js_syntax_errors",
    "check_hoot_runner_coverage",
    "check_external_library_locality",
    "check_access_csv_group_order",
    "check_doc_images",
    "check_binary_manifest_consistency",
    "eslint",
    # Test suites for code in other directories: their inputs are tools/, scripts/ and ingest/.
    "pytest tools/",
    "pytest scripts/",
    "pytest ingest/",
}


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _commit_all(repo):
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "user.name=t", "-c", "user.email=t@example.invalid",
        "commit", "-q", "-m", "base",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def _manifest(depends):
    return repr({"name": "m", "version": "1.0", "depends": depends, "license": "AGPL-3"})


class SelectionFixture(unittest.TestCase):
    """One workspace for the class; each test resets the work trees to the committed base."""

    @classmethod
    def setUpClass(cls):
        cls.ws = tempfile.mkdtemp(prefix="lint-selection-")
        cls.open_repo = os.path.join(cls.ws, "hams_open")
        cls.com_repo = os.path.join(cls.ws, "hams_com")
        cls.shared = os.path.join(cls.open_repo, "hams_shared")

        os.makedirs(cls.shared)
        shutil.copytree(
            HERE,
            os.path.join(cls.shared, "tools"),
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        for name in ("eslint.config.js", "tsconfig.json", "package.json"):
            shutil.copy(os.path.join(SHARED, name), os.path.join(cls.shared, name))
        _write(os.path.join(cls.shared, "docs", "guide.md"), "guide\n")
        _write(os.path.join(cls.shared, "agents", "skills", "s", "SKILL.md"), "skill\n")
        _git(cls.shared, "init", "-q", "-b", "main")
        _commit_all(cls.shared)

        _git(cls.open_repo, "init", "-q", "-b", "main")
        _write(os.path.join(cls.open_repo, ".gitignore"), "hams_shared/\n")
        os.symlink(os.path.join("hams_shared", "tools"), os.path.join(cls.open_repo, "tools"))
        _write(os.path.join(cls.open_repo, "zero_sudo", "__manifest__.py"), _manifest([]))
        _write(os.path.join(cls.open_repo, "zero_sudo", "__init__.py"))
        _write(
            os.path.join(cls.open_repo, "zero_sudo", "models", "model.py"),
            "X = 1\n\n\n# [@ANCHOR: persona_login]\n# Verified by [@ANCHOR: test_persona_login]\n"
            "def persona_login():\n    return X\n",
        )
        _write(
            os.path.join(cls.open_repo, "zero_sudo", "tests", "test_model.py"),
            "# [@ANCHOR: test_persona_login]\n# Tests [@ANCHOR: persona_login]\n"
            "def test_persona_login():\n    from zero_sudo.models.model import persona_login\n    assert persona_login() == 1\n",
        )
        _write(
            os.path.join(cls.open_repo, "zero_sudo", "docs", "stories", "login.md"),
            "# Login\n\n[@ANCHOR: persona_login]\n",
        )
        _write(
            os.path.join(cls.open_repo, "hams_helpdesk", "models", "ticket.py"),
            "# Triggers [@ANCHOR: zero_sudo:persona_login]\nTICKET = 1\n",
        )
        _write(os.path.join(cls.open_repo, "zero_sudo", "static", "src", "js", "a.js"), "export const a = 1;\n")
        _write(os.path.join(cls.open_repo, "hams_helpdesk", "__manifest__.py"), _manifest(["zero_sudo"]))
        _write(os.path.join(cls.open_repo, "hams_helpdesk", "__init__.py"))
        _write(os.path.join(cls.open_repo, "daemons", "bench", "Cargo.toml"), "[package]\nname = 'bench'\n")
        _write(os.path.join(cls.open_repo, "daemons", "bench", "Cargo.lock"), "# lock\n")
        _write(os.path.join(cls.open_repo, "daemons", "bench", "src", "main.rs"), "fn main() {}\n")
        _write(os.path.join(cls.open_repo, "daemons", "bench", "tools", "helper.py"), "VALUE = 1\n")
        _write(os.path.join(cls.open_repo, "daemons", "bench", "README.md"), "bench\n")
        _write(os.path.join(cls.open_repo, "daemons", "bench", "logo.png"), "png\n")
        _write(os.path.join(cls.open_repo, "scripts_top", "run.sh"), "#!/bin/sh\n")
        _commit_all(cls.open_repo)

        os.makedirs(cls.com_repo)
        _git(cls.com_repo, "init", "-q", "-b", "main")
        _write(os.path.join(cls.com_repo, "ham_x", "__manifest__.py"), _manifest(["zero_sudo"]))
        _write(os.path.join(cls.com_repo, "ham_x", "__init__.py"))
        _commit_all(cls.com_repo)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.ws, ignore_errors=True)

    def setUp(self):
        for repo in (self.open_repo, self.com_repo, self.shared):
            _git(repo, "reset", "-q", "--hard", "refs/remotes/origin/main")
            _git(repo, "clean", "-qfd")

    def run_script(self, *args):
        env = dict(os.environ)
        env["HAMS_ALLOW_STALE_SHARED"] = "1"
        return subprocess.run(
            [sys.executable, os.path.join(self.open_repo, "tools", "run_linters.py"), *args],
            cwd=self.open_repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def selected(self, *changes):
        """Apply `changes` (path relative to the workspace, new text) and return the step names
        `--changed --list` selects."""
        for relative, text in changes:
            path = os.path.join(self.ws, relative)
            _write(path, text)
        result = self.run_script("--changed", "--list")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [line for line in result.stdout.splitlines() if line.strip()]

    def all_steps(self):
        result = self.run_script("--list")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [line for line in result.stdout.splitlines() if line.strip()]


def _base(name):
    return name.split("[")[0]


class DaemonChangeSelectionTests(SelectionFixture):
    def test_a_rust_change_under_daemons_runs_every_checker_that_reads_rust(self):
        picked = {_base(n) for n in self.selected(("hams_open/daemons/bench/src/main.rs", "fn main() { }\n"))}
        for expected in (
            "check_cargo_fmt",
            "check_cargo_clippy",
            "check_cargo_deny",
            "check_rust_function_test_anchors",
            "check_async_lock_scope",
            "check_fake_network_listener",
            "verify_anchors",
            "check_claims_freshness",
            "check_binary_manifest_consistency",
        ):
            self.assertIn(expected, picked, f"{expected} did not run for a daemons/ Rust change")

    def test_a_python_change_under_daemons_runs_every_checker_that_reads_python(self):
        full = self.all_steps()
        chosen = self.selected(("hams_open/daemons/bench/tools/helper.py", "VALUE = 2\n"))
        for must in (
            "flake8",
            "check_burn_list",
            "verify_anchors",
            "check_manifest_dependencies",
            "check_test_tags",
            "check_absolute_paths",
            "check_rabbitmq_pool",
            "check_shebang",
            "check_init_imports",
            "check_untyped_utility_files",
            "check_function_test_anchors",
            "check_int_float_overflow[hams_open]",
            "check_float_isfinite[hams_open]",
            "check_fake_network_listener[hams_open]",
            "check_summation_bias",
            "check_dependency_cycles",
            "check_model_extension_collisions",
        ):
            self.assertIn(must, chosen, f"{must} did not run for a daemons/ Python change")
        left_out = {_base(n) for n in full if n not in chosen}
        unexpected = {
            name
            for name in left_out
            if name not in STEPS_THAT_CANNOT_READ_PYTHON and not name.startswith("pre_flight_check")
        }
        self.assertEqual(
            unexpected,
            set(),
            "steps skipped for a daemons/ Python change that are not on the list of checkers "
            "that cannot read Python",
        )

    def test_the_pass_does_not_need_the_cargo_checkers_for_a_python_only_change(self):
        chosen = {_base(n) for n in self.selected(("hams_open/daemons/bench/tools/helper.py", "VALUE = 2\n"))}
        self.assertTrue({"check_cargo_clippy", "check_cargo_fmt", "check_cargo_deny"}.isdisjoint(chosen))

    def test_a_cargo_lock_change_runs_the_dependency_audit(self):
        chosen = {_base(n) for n in self.selected(("hams_open/daemons/bench/Cargo.lock", "# lock 2\n"))}
        self.assertIn("check_cargo_deny", chosen)

    def test_a_readme_change_under_daemons_runs_the_documentation_checkers(self):
        chosen = {_base(n) for n in self.selected(("hams_open/daemons/bench/README.md", "bench 2\n"))}
        for expected in ("verify_anchors", "check_claims_freshness", "check_doc_images"):
            self.assertIn(expected, chosen)

    def test_an_image_only_change_skips_the_source_code_checkers(self):
        chosen = {_base(n) for n in self.selected(("hams_open/daemons/bench/logo.png", "png 2\n"))}
        for left_out in ("flake8", "check_burn_list", "check_cargo_clippy", "verify_anchors", "check_js_syntax"):
            self.assertNotIn(left_out, chosen)

    def test_a_script_outside_every_module_runs_the_whole_repository_checkers(self):
        chosen = {_base(n) for n in self.selected(("hams_open/scripts_top/run.sh", "#!/bin/sh\necho 2\n"))}
        for expected in ("check_shebang", "check_absolute_paths", "check_manifest_dependencies"):
            self.assertIn(expected, chosen)


class ModuleChangeSelectionTests(SelectionFixture):
    def burn_list_targets(self, *changes):
        """The scan roots `check_burn_list` is started with for these changes: whole repository, or the changed modules."""
        for relative, text in changes:
            _write(os.path.join(self.ws, relative), text)
        result = self.run_script("--changed", "--list")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return sorted(line for line in result.stdout.splitlines() if line.startswith("check_burn_list"))

    def test_a_rust_change_beside_a_module_change_leaves_the_checkers_on_the_changed_modules(self):
        only_module = self.burn_list_targets(("hams_open/zero_sudo/static/src/js/a.js", "export const a = 2;\n"))
        with_rust = self.burn_list_targets(("hams_open/daemons/bench/src/main.rs", "fn main() { }\n"))
        self.assertEqual(only_module, ["check_burn_list[hams_helpdesk]", "check_burn_list[zero_sudo]"])
        self.assertEqual(with_rust, only_module, "a Rust file cannot change what the burn list reports")

    def test_a_story_and_a_shell_script_beside_a_module_change_do_not_widen_the_burn_list(self):
        names = self.burn_list_targets(
            ("hams_open/zero_sudo/static/src/js/a.js", "export const a = 2;\n"),
            ("hams_open/daemons/bench/README.md", "bench 3\n"),
            ("hams_open/scripts_top/run.sh", "#!/bin/sh\necho 3\n"),
        )
        self.assertEqual(names, ["check_burn_list[hams_helpdesk]", "check_burn_list[zero_sudo]"])

    def test_a_shell_script_outside_every_module_still_widens_the_checkers_that_read_it(self):
        result = self.run_script("--changed", "--list")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _write(os.path.join(self.ws, "hams_open/scripts_top/run.sh"), "#!/bin/sh\necho 4\n")
        _write(os.path.join(self.ws, "hams_open/zero_sudo/static/src/js/a.js"), "export const a = 4;\n")
        result = self.run_script("--changed", "--list")
        names = [n for n in result.stdout.splitlines() if n.startswith("check_shebang")]
        self.assertEqual(names, ["check_shebang"], "the shebang checker reads shell scripts: whole repository")

    def test_a_python_change_beside_a_module_change_still_widens_the_checkers_to_the_whole_repository(self):
        names = self.burn_list_targets(
            ("hams_open/zero_sudo/static/src/js/a.js", "export const a = 2;\n"),
            ("hams_open/daemons/bench/tools/helper.py", "VALUE = 2\n"),
        )
        self.assertEqual(names, ["check_burn_list"])

    def test_a_python_change_in_a_module_runs_that_module_and_its_dependents(self):
        chosen = self.selected(("hams_open/zero_sudo/models/model.py", "X = 2\n"))
        self.assertIn("pre_flight_check[zero_sudo]", chosen)
        self.assertIn("pre_flight_check[hams_helpdesk]", chosen)
        # hams_com modules are pre-flighted by a run started from hams_com, as in a full run.
        self.assertNotIn("pre_flight_check[ham_x]", chosen)
        self.assertIn("verify_anchors", chosen)

    def test_a_javascript_change_in_a_module_runs_the_javascript_checkers(self):
        chosen = {
            _base(n)
            for n in self.selected(("hams_open/zero_sudo/static/src/js/a.js", "export const a = 2;\n"))
        }
        for expected in ("eslint", "check_js_syntax", "check_js_function_test_anchors", "check_burn_list"):
            self.assertIn(expected, chosen)

    def test_verify_anchors_finds_documentation_that_lives_outside_the_changed_module(self):
        # 2026-10-29, #957: scoped to hams_helpdesk, verify_anchors reported
        # `zero_sudo:persona_login` as missing because the code, test and story that define it are
        # in zero_sudo. The step must scan the whole repository.
        _write(os.path.join(self.open_repo, "hams_helpdesk", "models", "ticket.py"),
               "# Triggers [@ANCHOR: zero_sudo:persona_login]\nTICKET = 2\n")
        result = self.run_script("--changed", "--only", "verify_anchors", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("persona_login", result.stdout)

    def test_verify_anchors_still_reports_a_reference_to_an_anchor_nobody_defines(self):
        _write(os.path.join(self.open_repo, "hams_helpdesk", "models", "ticket.py"),
               "# Triggers [@ANCHOR: zero_sudo:no_such_anchor]\nTICKET = 2\n")
        result = self.run_script("--changed", "--only", "verify_anchors", "--no-cache")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no_such_anchor", result.stdout)

    def test_verify_anchors_finds_a_test_that_lives_in_the_sibling_repository(self):
        # Started through the repository's `tools` symlink, verify_anchors used to look for the
        # sibling beside the repository instead of beside hams_shared and never found hams_com.
        _write(os.path.join(self.open_repo, "hams_helpdesk", "models", "only_com_tests.py"),
               "# [@ANCHOR: hams_helpdesk:com_tested]\n# Verified by [@ANCHOR: ham_x:test_com_tested]\n"
               "def com_tested():\n    return 1\n")
        _write(os.path.join(self.open_repo, "hams_helpdesk", "docs", "stories", "c.md"),
               "# C\n\n[@ANCHOR: com_tested]\n")
        _write(os.path.join(self.com_repo, "ham_x", "tests", "test_c.py"),
               "# [@ANCHOR: test_com_tested]\n# Tests [@ANCHOR: hams_helpdesk:com_tested]\n"
               "def test_com_tested():\n    from hams_helpdesk.models.only_com_tests import com_tested\n    assert com_tested() == 1\n")
        result = self.run_script("--changed", "--only", "verify_anchors", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # and without the sibling's test, the same anchor is reported untested
        os.remove(os.path.join(self.com_repo, "ham_x", "tests", "test_c.py"))
        result = self.run_script("--changed", "--only", "verify_anchors", "--no-cache")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("com_tested", result.stdout)


    def test_verify_anchors_in_a_worktree_elsewhere_uses_the_main_checkouts_sibling(self):
        _write(os.path.join(self.open_repo, "hams_helpdesk", "models", "only_com_tests.py"),
               "# [@ANCHOR: hams_helpdesk:com_tested]\n# Verified by [@ANCHOR: test_com_tested]\n"
               "def com_tested():\n    return 1\n")
        _write(os.path.join(self.open_repo, "hams_helpdesk", "docs", "stories", "c.md"),
               "# C\n\n[@ANCHOR: com_tested]\n")
        _write(os.path.join(self.com_repo, "ham_x", "tests", "test_c.py"),
               "# [@ANCHOR: test_com_tested]\n# Tests [@ANCHOR: hams_helpdesk:com_tested]\n"
               "def test_com_tested():\n    from hams_helpdesk.models.only_com_tests import com_tested\n"
               "    assert com_tested() == 1\n")
        for repo in (self.open_repo, self.com_repo):
            _git(repo, "add", "-A")
            _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
        elsewhere = tempfile.mkdtemp(prefix="lint-wt-elsewhere-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        worktree = os.path.join(elsewhere, "branch_checkout")
        _git(self.open_repo, "worktree", "add", "-q", "-b", "side", worktree)
        self.addCleanup(_git, self.open_repo, "worktree", "remove", "--force", worktree)
        os.symlink(os.path.join(self.shared), os.path.join(worktree, "hams_shared"))
        result = subprocess.run(
            [sys.executable, os.path.join(self.shared, "tools", "verify_anchors.py"), worktree],
            cwd=worktree, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class HamsSharedChangeSelectionTests(SelectionFixture):
    def test_a_guide_under_hams_shared_docs_runs_the_documentation_checkers(self):
        chosen = {_base(n) for n in self.selected(("hams_open/hams_shared/docs/guide.md", "guide 2\n"))}
        for expected in ("verify_anchors", "check_claims_freshness", "check_doc_images", "check_skill_integrity"):
            self.assertIn(expected, chosen)

    def test_a_skill_under_hams_shared_agents_runs_the_skill_integrity_check(self):
        chosen = {_base(n) for n in self.selected(("hams_open/hams_shared/agents/skills/s/SKILL.md", "skill 2\n"))}
        self.assertIn("check_skill_integrity", chosen)

    def test_a_change_to_the_checkers_themselves_runs_every_step(self):
        full = self.all_steps()
        chosen = self.selected(("hams_open/hams_shared/tools/check_shebang.py", "# changed\n"))
        self.assertEqual(sorted(chosen), sorted(full))


class BaseHandlingTests(SelectionFixture):
    def test_an_unreadable_base_is_an_error_and_not_an_empty_change_set(self):
        result = self.run_script("--changed", "refs/remotes/origin/does-not-exist", "--list")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cannot compare", result.stderr)


class EslintToolchainTests(SelectionFixture):
    def test_missing_toolchain_fails_the_step_with_a_clear_message_when_javascript_is_in_scope(self):
        # The fixture's hams_shared has no node_modules and is its own main checkout.
        result = self.run_script("--only", "eslint")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ESLint toolchain not found", result.stdout)
        self.assertIn("npm install", result.stdout)

    def test_missing_toolchain_does_not_fail_a_change_with_no_javascript(self):
        chosen = self.selected(("hams_open/daemons/bench/tools/helper.py", "VALUE = 3\n"))
        self.assertNotIn("eslint", chosen)

    def test_missing_toolchain_is_reported_for_a_javascript_change(self):
        chosen = self.selected(("hams_open/zero_sudo/static/src/js/a.js", "export const a = 3;\n"))
        self.assertIn("eslint", chosen)


class EslintFromAnyLayoutTests(unittest.TestCase):
    """ESLint's type-aware rules read a TypeScript project; its include list used to be relative to
    hams_shared, so a checkout that was not beside the main hams_shared checkout (every worktree)
    got one "file not found in the provided project" error per JavaScript file."""

    def setUp(self):
        self.toolchain = run_linters.find_eslint_toolchain(SHARED)
        self.assertIsNotNone(
            self.toolchain,
            "ESLint is not installed (npm install in the main hams_shared checkout); "
            "JavaScript cannot be linted on this machine",
        )
        self.ws = tempfile.mkdtemp(prefix="eslint-layout-")
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.repo = os.path.join(self.ws, "elsewhere", "hams_open_worktree")
        self.sibling = os.path.join(self.ws, "elsewhere", "hams_com_checkout")
        os.makedirs(self.sibling)
        _write(os.path.join(self.repo, "mod_a", "static", "src", "js", "ok.js"), "export const ok = 1;\n")

    def lint(self):
        project = run_linters.write_eslint_project(SHARED, [self.repo, self.sibling])
        env = dict(os.environ)
        env["HAMS_ESLINT_TSCONFIG"] = project
        env["NODE_PATH"] = self.toolchain["node_modules"]
        return subprocess.run(
            [
                self.toolchain["eslint_bin"],
                "--config", os.path.join(SHARED, "eslint.config.js"),
                "--no-error-on-unmatched-pattern",
                self.repo,
            ],
            cwd=os.path.join(self.ws, "elsewhere"),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def test_a_clean_file_in_an_unrelated_layout_passes(self):
        result = self.lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("parserOptions.project", result.stdout)

    def test_a_real_violation_in_an_unrelated_layout_is_reported_as_the_violation(self):
        _write(os.path.join(self.repo, "mod_a", "static", "src", "js", "bad.js"), "export const x = undefinedName;\n")
        result = self.lint()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no-undef", result.stdout)
        self.assertNotIn("parserOptions.project", result.stdout)

    def test_the_generated_project_lists_both_repositories(self):
        project = run_linters.write_eslint_project(SHARED, [self.repo, self.sibling])
        with open(project, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["extends"], os.path.join(SHARED, "tsconfig.json"))
        self.assertIn(os.path.join(os.path.realpath(self.repo), "*", "static", "**", "*.js"), data["include"])
        self.assertIn(os.path.join(os.path.realpath(self.repo), "daemons", "**", "*.js"), data["include"])
        self.assertIn(os.path.join(os.path.realpath(self.repo), "daemons", "**", "*.mjs"), data["include"])
        absent = os.path.join(self.ws, "elsewhere", "not_there")
        again = run_linters.write_eslint_project(SHARED, [self.repo, absent])
        with open(again, encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)["include"]), 3, "a missing directory is not listed (three globs for the one real repository)")


class JavascriptAnchorScanFailureTests(unittest.TestCase):
    def test_a_scan_that_cannot_run_is_a_failure_and_not_zero_functions(self):
        import check_js_function_test_anchors as scanner

        original = scanner._JS_SCAN_SCRIPT
        scanner._JS_SCAN_SCRIPT = os.path.join(tempfile.gettempdir(), "no-such-scan-script.cjs")
        try:
            with self.assertRaises(SystemExit) as raised:
                scanner._run_js_scan(["/x/a.js"])
        finally:
            scanner._JS_SCAN_SCRIPT = original
        self.assertIn("JS function scan failed", str(raised.exception))

    def test_the_real_scan_finds_functions_from_a_worktree_without_node_modules(self):
        import check_js_function_test_anchors as scanner

        directory = tempfile.mkdtemp(prefix="js-scan-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "a.js")
        _write(path, "export function named() { return 1; }\n")
        found = scanner._run_js_scan([path])
        self.assertEqual([entry[0] for entry in found[path]], ["named"])


class MainWorktreeTests(unittest.TestCase):
    def test_the_main_worktree_of_a_plain_repository_is_its_own_directory(self):
        import lint_exec

        directory = tempfile.mkdtemp(prefix="mainwt-")
        self.addCleanup(shutil.rmtree, directory, True)
        _git(directory, "init", "-q", "-b", "main")
        self.assertEqual(os.path.realpath(lint_exec.main_worktree_of(directory)), os.path.realpath(directory))

    def test_the_main_worktree_of_a_linked_worktree_is_the_main_directory(self):
        import lint_exec

        directory = tempfile.mkdtemp(prefix="mainwt-")
        self.addCleanup(shutil.rmtree, directory, True)
        main = os.path.join(directory, "main")
        os.makedirs(main)
        _git(main, "init", "-q", "-b", "main")
        _write(os.path.join(main, "f"), "x\n")
        _git(main, "add", "-A")
        _git(main, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
        linked = os.path.join(directory, "linked")
        _git(main, "worktree", "add", "-q", "-b", "side", linked)
        self.assertEqual(os.path.realpath(lint_exec.main_worktree_of(linked)), os.path.realpath(main))


if __name__ == "__main__":
    unittest.main()
