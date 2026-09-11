#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit test for run_linters.py's step-27 test-suite discovery glob.

run_linters.py's main() is one large, non-decomposed, side-effecting
function -- it hardcodes dir_path from its own real location, runs
~27 real linter/subprocess steps against the real repo tree, and has
no way to be pointed at a fixture. Exercising the whole orchestrator
per test run is neither isolated nor fast, so this test extracts and
execs just the two-statement discovery snippet (the exact source lines,
not a re-implementation of them) against a controlled fixture dir_path,
to lock in the specific regression this step exists to prevent: a
future edit narrowing the glob back to `test_check_*.py` (which is
exactly how the "nothing runs these tests" bug happened the first
time), or removing a runner script from the exclusion set so pytest
tries to collect test.py/test_mcp_server.py and executes their
module-level Odoo-launching code instead of running unit tests.
"""

import glob
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_linters  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_linters.py")


def _extract_discovery_snippet():
    with open(_SCRIPT, encoding="utf-8") as f:
        source = f.read()
    match = re.search(
        r"([ \t]*_RUNNER_SCRIPTS_NOT_SUITES = \{.*?\n[ \t]*\)\n)",
        source,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(
            "Could not locate the test-suite discovery snippet in run_linters.py -- "
            "its source shape changed; update this test's extraction regex."
        )
    return textwrap.dedent(match.group(1))


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class DiscoverySnippetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.snippet = _extract_discovery_snippet()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_snippet(self):
        namespace = {"glob": glob, "os": os, "dir_path": self.tmp}
        exec(self.snippet, namespace)  # noqa: S102 -- real source, not user input
        return namespace["tool_test_files"]

    def test_a_real_test_suite_file_is_collected(self):
        _write(os.path.join(self.tmp, "tools", "test_check_dependency_cycles.py"))
        self.assertEqual(
            self._run_snippet(),
            [os.path.join(self.tmp, "tools", "test_check_dependency_cycles.py")],
        )

    def test_a_suite_not_named_after_a_check_script_is_still_collected(self):
        # The exact regression this step's own commit message documents:
        # a `test_check_*` glob would silently skip a suite like this one.
        _write(os.path.join(self.tmp, "tools", "test_run_linters.py"))
        self.assertEqual(
            self._run_snippet(),
            [os.path.join(self.tmp, "tools", "test_run_linters.py")],
        )

    def test_test_py_the_odoo_runner_script_is_excluded(self):
        _write(os.path.join(self.tmp, "tools", "test.py"))
        self.assertEqual(self._run_snippet(), [])

    def test_test_mcp_server_py_is_excluded(self):
        _write(os.path.join(self.tmp, "tools", "test_mcp_server.py"))
        self.assertEqual(self._run_snippet(), [])

    def test_a_mix_of_runner_scripts_and_real_suites_keeps_only_the_suites_sorted(self):
        _write(os.path.join(self.tmp, "tools", "test.py"))
        _write(os.path.join(self.tmp, "tools", "test_mcp_server.py"))
        _write(os.path.join(self.tmp, "tools", "test_zzz_last.py"))
        _write(os.path.join(self.tmp, "tools", "test_aaa_first.py"))
        result = self._run_snippet()
        self.assertEqual(
            result,
            [
                os.path.join(self.tmp, "tools", "test_aaa_first.py"),
                os.path.join(self.tmp, "tools", "test_zzz_last.py"),
            ],
        )

    def test_no_matching_files_yields_an_empty_list(self):
        os.makedirs(os.path.join(self.tmp, "tools"))
        self.assertEqual(self._run_snippet(), [])


class ResolveRepoRootTests(unittest.TestCase):
    # Regression test for the same real bug found and fixed across 9 other checker scripts the
    # same night (see docs/proposals/LINTER_POLICY_REVISIT.md): run_linters.py's own `dir_path`
    # resolves to .../hams_shared, not a real repo root, whenever it's invoked from hams_shared
    # directly -- a third, intentionally valid invocation mode per AGENTS.md. Confirmed directly:
    # before this fix, check_burn_list.py scanned only 3 files via that exact invocation, versus
    # 557 once `targets`/module-discovery/the sibling-repo resolution all switched from `dir_path`
    # to `_resolve_repo_root(dir_path)`.
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            run_linters._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(run_linters._resolve_repo_root(fake_repo), fake_repo)


def _make_manifest_dir(base, name):
    mod_dir = os.path.join(base, name)
    os.makedirs(mod_dir, exist_ok=True)
    _write(os.path.join(mod_dir, "__manifest__.py"), "{}")
    return mod_dir


class ResolveModulePathTests(unittest.TestCase):
    # Real bug found 2026-09-10 reviewing run_linters.py: `targets` (the list handed to
    # flake8/check_burn_list/verify_anchors/etc when modules are explicitly named) used to be
    # recomputed from scratch as `os.path.join(repo_root, mod)` for every named module,
    # completely ignoring the `community_dir` (sibling-repo) fallback the neighboring
    # pre-flight-check loop already performs one path resolution earlier. A module that exists
    # only in the sibling repo got a `targets` entry pointing at a path that doesn't exist under
    # `repo_root` at all -- `os.walk()` on a nonexistent directory silently yields zero files, so
    # every targets-scoped checker reported a clean pass for that module without ever having
    # scanned it. `_resolve_module_path` is the single resolution `main()` now shares between the
    # pre-flight loop and the `targets` computation, closing that divergence.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo_root = os.path.join(self.tmp, "hams_com")
        self.community_dir = os.path.join(self.tmp, "hams_open")
        os.makedirs(self.repo_root)
        os.makedirs(self.community_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_module_in_repo_root_resolves_there(self):
        mod_dir = _make_manifest_dir(self.repo_root, "local_mod")
        self.assertEqual(
            run_linters._resolve_module_path("local_mod", self.repo_root, self.community_dir),
            mod_dir,
        )

    def test_a_module_only_in_the_sibling_repo_falls_back_there(self):
        # The exact case the real bug got wrong: `zero_sudo` (or any module) that exists only
        # under `community_dir`, not `repo_root`.
        mod_dir = _make_manifest_dir(self.community_dir, "zero_sudo")
        self.assertEqual(
            run_linters._resolve_module_path("zero_sudo", self.repo_root, self.community_dir),
            mod_dir,
        )

    def test_a_module_in_neither_repo_resolves_to_none(self):
        self.assertIsNone(
            run_linters._resolve_module_path("nonexistent_mod", self.repo_root, self.community_dir)
        )

    def test_no_community_dir_and_module_missing_resolves_to_none(self):
        self.assertIsNone(
            run_linters._resolve_module_path("nonexistent_mod", self.repo_root, None)
        )


class RunPerTargetCheckerTests(unittest.TestCase):
    # Real bug found 2026-09-10: run_linters.py invoked several hams_shared/tools/check_*.py
    # scripts as ONE subprocess call with every scoped target appended to the same argv list
    # (`[script] + targets`). check_manifest_dependencies.py, check_test_tags.py,
    # check_absolute_paths.py, check_rabbitmq_pool.py, check_shebang.py, and
    # check_init_imports.py all read only `sys.argv[1]`, silently ignoring every target after the
    # first; check_burn_list.py's `nargs="?"` positional argument makes argparse reject 2+
    # positional values outright. Confirmed empirically against the real, unmodified
    # check_shebang.py (git-stashed back to this file's pre-fix state first): a batched call
    # `[sys.executable, "check_shebang.py", modA, modB]` where modB (the SECOND target) contains
    # a real, unambiguous violation (a `#!` on line 2, not line 1) returned exit code 0 -- the
    # violation was silently never seen. `_run_per_target_checker` is the fix: run the checker
    # once per target and aggregate. This test exercises it against the real check_shebang.py
    # script (not a fake/mock), with the violation deliberately placed in the SECOND target, so
    # it fails if the "only the first target is ever actually checked" bug is reintroduced.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_violation_in_the_second_of_two_targets_is_still_caught(self):
        mod_a = os.path.join(self.tmp, "modA")
        mod_b = os.path.join(self.tmp, "modB")
        os.makedirs(mod_a)
        os.makedirs(mod_b)
        _write(os.path.join(mod_a, "clean.py"), "print(1)\n")
        # A real, unambiguous check_shebang.py violation: a shebang line that isn't line 1.
        _write(os.path.join(mod_b, "bad.py"), "x = 1\n#!/usr/bin/env python3\n")

        tools_dir = os.path.dirname(os.path.abspath(run_linters.__file__))
        dir_path = os.path.dirname(tools_dir)
        any_failed = run_linters._run_per_target_checker(
            sys.executable, dir_path, "check_shebang.py", [mod_a, mod_b]
        )
        self.assertTrue(
            any_failed,
            "check_shebang.py's real violation in the SECOND target was not detected -- "
            "the per-target loop silently only checked the first target again.",
        )

    def test_a_clean_second_target_does_not_report_a_false_failure(self):
        mod_a = os.path.join(self.tmp, "modA")
        mod_b = os.path.join(self.tmp, "modB")
        os.makedirs(mod_a)
        os.makedirs(mod_b)
        _write(os.path.join(mod_a, "clean.py"), "print(1)\n")
        _write(os.path.join(mod_b, "also_clean.py"), "print(2)\n")

        tools_dir = os.path.dirname(os.path.abspath(run_linters.__file__))
        dir_path = os.path.dirname(tools_dir)
        any_failed = run_linters._run_per_target_checker(
            sys.executable, dir_path, "check_shebang.py", [mod_a, mod_b]
        )
        self.assertFalse(any_failed)

    def test_a_single_element_targets_list_makes_exactly_one_call_with_unchanged_argv(self):
        # The docstring on `_run_per_target_checker` asserts, but this file's own tests never
        # actually pinned down, that the common unscoped case (`targets == [repo_root]`, the
        # shape every call site uses before any multi-module scoping is requested) "produces
        # exactly one subprocess call with exactly the same argv as before" the refactor that
        # introduced this helper. This is the single most-exercised invocation shape of all --
        # a regression here (e.g. an accidental double-invocation, or an argv reordering) would
        # hit every unscoped lint run silently. Mocks subprocess.run directly rather than
        # exercising a real checker script, since the point here is call *shape*, not a real
        # checker's own pass/fail behavior (already covered by the two tests above).
        fake_result = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            run_linters.subprocess, "run", return_value=fake_result
        ) as mock_run:
            any_failed = run_linters._run_per_target_checker(
                "/usr/bin/python3", "/repo", "check_shebang.py", ["/repo"]
            )
        self.assertFalse(any_failed)
        mock_run.assert_called_once_with(
            ["/usr/bin/python3", os.path.join("/repo", "tools", "check_shebang.py"), "/repo"],
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
