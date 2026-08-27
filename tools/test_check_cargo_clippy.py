#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_cargo_clippy.py.

main()'s actual `cargo clippy` invocation is a real, potentially slow subprocess call -- never
exercised for real here. subprocess.run is mocked throughout main()'s tests, the same
boundary-mocking approach test_check_cargo_deny.py/test_check_pip_audit.py use for their own
subprocess calls. find_cargo_crates() has no subprocess involvement at all and is tested
directly against real temp-directory fixtures.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_cargo_clippy as chk  # noqa: E402


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class FindCargoCratesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_package_crate_is_found(self):
        crate = os.path.join(self.tmp, "daemons", "some_daemon")
        _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"some_daemon\"\n")
        self.assertEqual(chk.find_cargo_crates(self.tmp), [crate])

    def test_a_cargo_toml_without_a_package_table_is_not_found(self):
        # A workspace-only Cargo.toml (no [package] table of its own) --
        # its member crates are found and checked individually instead.
        crate = os.path.join(self.tmp, "workspace_root")
        _write(os.path.join(crate, "Cargo.toml"), "[workspace]\nmembers = [\"a\"]\n")
        self.assertEqual(chk.find_cargo_crates(self.tmp), [])

    def test_an_ignored_directory_is_never_walked(self):
        crate = os.path.join(self.tmp, "node_modules", "pkg")
        _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"pkg\"\n")
        self.assertEqual(chk.find_cargo_crates(self.tmp), [])

    def test_multiple_crates_are_all_found_and_sorted(self):
        crate_a = os.path.join(self.tmp, "daemons", "a_daemon")
        crate_b = os.path.join(self.tmp, "daemons", "b_daemon")
        for crate in (crate_a, crate_b):
            _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"x\"\n")
        found = chk.find_cargo_crates(self.tmp)
        self.assertEqual(len(found), 2)
        self.assertEqual(found, sorted(found))


class MainTests(unittest.TestCase):
    """subprocess.run is mocked throughout -- no real `cargo clippy` invocation is ever made in
    these tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self):
        return ["check_cargo_clippy.py", self.tmp]

    def _make_crate(self):
        crate = os.path.join(self.tmp, "daemons", "some_daemon")
        _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"some_daemon\"\n")
        return crate

    def test_no_crates_exits_0_without_calling_subprocess_at_all(self):
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run"
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)
            mock_run.assert_not_called()

    def test_clippy_not_installed_exits_1_with_a_clear_message(self):
        self._make_crate()
        version_check = MagicMock(returncode=1)
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run", return_value=version_check
        ) as mock_run, patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(mock_run.call_count, 1)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("not installed", printed)

    def test_a_clean_pass_exits_0(self):
        self._make_crate()
        version_check = MagicMock(returncode=0)
        clippy_result = MagicMock(returncode=0, stdout="", stderr="    Finished `dev` profile")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run", side_effect=[version_check, clippy_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)

    def test_a_warning_in_stderr_exits_1_even_with_returncode_0(self):
        # clippy's own exit code stays 0 for plain warnings (no -D passed) --
        # this gate must catch that case by scanning stderr, not just returncode.
        self._make_crate()
        version_check = MagicMock(returncode=0)
        clippy_result = MagicMock(returncode=0, stdout="", stderr="warning: unused variable")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run", side_effect=[version_check, clippy_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_a_nonzero_returncode_exits_1_even_with_no_warning_text(self):
        # A real build error (not just a lint warning) -- must not be
        # silently swallowed just because "warning:" didn't appear.
        self._make_crate()
        version_check = MagicMock(returncode=0)
        clippy_result = MagicMock(returncode=1, stdout="", stderr="error[E0433]: failed to resolve")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run", side_effect=[version_check, clippy_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_multiple_crates_are_all_checked_even_after_a_finding(self):
        crate_a = os.path.join(self.tmp, "daemons", "a_daemon")
        crate_b = os.path.join(self.tmp, "daemons", "b_daemon")
        for crate in (crate_a, crate_b):
            _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"x\"\n")
        version_check = MagicMock(returncode=0)
        finding = MagicMock(returncode=0, stdout="", stderr="warning: unused variable")
        clean = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run",
            side_effect=[version_check, finding, clean],
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(mock_run.call_count, 3)


class ResolveRepoRootTests(unittest.TestCase):
    # This checker was added 2026-08-26, after the original dir_path-repo-root audit
    # (LINTER_POLICY_REVISIT.md) that fixed 9 other checkers -- it inherited the same
    # hams_shared-redirect pattern by copying it, but was never itself covered by that audit's
    # own regression tests until now.
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            chk._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(chk._resolve_repo_root(fake_repo), fake_repo)


class ResolveRepoRootsTests(unittest.TestCase):
    # Regression test for the real gap found 2026-08-27: the four real hams_com daemon crates
    # this gate's own docstring cites findings from (hams_local_relay, hams_relay_bridge,
    # hams_data_relay, hams_simulated_band) live under hams_com/daemons/, not hams_open --
    # confirmed directly, run_linters.py's own real invocation was silently never checking any
    # of them since being wired in.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_repo(self, name, module_names=()):
        repo = os.path.join(self.workspace, name)
        for module_name in module_names:
            _write(os.path.join(repo, module_name, "__manifest__.py"), "{}")
        return repo

    def test_hams_shared_input_appends_the_real_hams_com_sibling(self):
        self._make_repo("hams_open", module_names=["zero_sudo"])
        hams_com = self._make_repo("hams_com", module_names=["ham_base"])
        hams_shared = os.path.join(self.workspace, "hams_open", "hams_shared")
        os.makedirs(hams_shared)
        roots = chk._resolve_repo_roots(hams_shared)
        self.assertEqual(roots, [os.path.join(self.workspace, "hams_open"), hams_com])

    def test_a_real_repo_root_with_no_odoo_sibling_present_scans_alone(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        self.assertEqual(chk._resolve_repo_roots(repo), [repo])

    def test_a_sibling_directory_with_no_manifest_py_anywhere_is_not_treated_as_a_repo(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        os.makedirs(os.path.join(self.workspace, "hams_com", "not_a_module"))
        self.assertEqual(chk._resolve_repo_roots(repo), [repo])


if __name__ == "__main__":
    unittest.main()
