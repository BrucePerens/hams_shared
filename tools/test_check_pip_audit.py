#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_pip_audit.py.

main()'s actual pip-audit invocation makes a real network call against
PyPI's advisory database -- never exercised for real here. subprocess.run
is mocked throughout main()'s tests so no test in this file makes any
network call, the same boundary-mocking approach used for
check_bot_compliance.py's DNS/HTTP calls in this same sweep.
find_requirements_files() has no subprocess involvement at all and is
tested directly against real temp-directory fixtures.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import check_pip_audit as chk  # noqa: E402

_CACHE_ROOT = tempfile.mkdtemp(prefix="pipaudit-cache-test-")


def setUpModule():
    # Every test gets its own cache directory: no real cache under ~/.cache is read or written.
    os.environ["HAMS_PIP_AUDIT_CACHE_DIR"] = _CACHE_ROOT


def tearDownModule():
    os.environ.pop("HAMS_PIP_AUDIT_CACHE_DIR", None)
    shutil.rmtree(_CACHE_ROOT, ignore_errors=True)


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class FindRequirementsFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ignores_site_packages_regardless_of_the_venvs_own_directory_name(self):
        """A venv's own outer directory can be named anything ("whisper_venv", not just "venv" or
        ".venv") -- matches check_float_isfinite.py's own real-bug regression test. A real
        site-packages tree can genuinely carry a vendored package's own requirements.txt."""
        _write(
            os.path.join(
                self.tmp, "whisper_venv", "lib", "python3.13", "site-packages", "numpy", "requirements.txt"
            )
        )
        _write(os.path.join(self.tmp, "requirements.txt"))
        self.assertEqual(
            chk.find_requirements_files(self.tmp),
            [os.path.join(self.tmp, "requirements.txt")],
        )

    def test_a_root_level_requirements_txt_is_found(self):
        _write(os.path.join(self.tmp, "requirements.txt"))
        self.assertEqual(
            chk.find_requirements_files(self.tmp),
            [os.path.join(self.tmp, "requirements.txt")],
        )

    def test_a_variant_name_like_requirements_dev_txt_is_found(self):
        _write(os.path.join(self.tmp, "requirements-dev.txt"))
        found = chk.find_requirements_files(self.tmp)
        self.assertEqual(len(found), 1)

    def test_a_different_extension_is_not_found(self):
        _write(os.path.join(self.tmp, "requirements.in"))
        self.assertEqual(chk.find_requirements_files(self.tmp), [])

    def test_a_file_not_starting_with_requirements_is_not_found(self):
        _write(os.path.join(self.tmp, "dev_requirements.txt"))
        self.assertEqual(chk.find_requirements_files(self.tmp), [])

    def test_an_ignored_directory_is_never_walked(self):
        _write(os.path.join(self.tmp, "node_modules", "pkg", "requirements.txt"))
        self.assertEqual(chk.find_requirements_files(self.tmp), [])

    def test_a_claude_worktree_directory_is_never_walked(self):
        # Agent sessions run in isolated git worktrees under `.claude/worktrees/<session>/`
        # INSIDE the repo root, and a worktree is a full checkout (verified with a real
        # `git worktree add`) -- `requirements.txt` included, not an empty directory.
        _write(
            os.path.join(self.tmp, ".claude", "worktrees", "sess1", "requirements.txt")
        )
        self.assertEqual(chk.find_requirements_files(self.tmp), [])

    def test_multiple_files_across_directories_are_all_found_and_sorted(self):
        _write(os.path.join(self.tmp, "daemons", "foo", "requirements.txt"))
        _write(os.path.join(self.tmp, "ingest", "requirements.txt"))
        found = chk.find_requirements_files(self.tmp)
        self.assertEqual(len(found), 2)
        self.assertEqual(found, sorted(found))


class MainTests(unittest.TestCase):
    """subprocess.run is mocked throughout -- no real pip-audit invocation
    (and therefore no real network call) is ever made in these tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self):
        return ["check_pip_audit.py", self.tmp]

    @patch.dict(os.environ, {"HAMS_CHECK_ALLOW_EMPTY": "1"})  # a step that may legitimately see zero files (check_target.py)
    def test_no_requirements_files_exits_0_without_calling_subprocess_at_all(self):
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run"
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)
            mock_run.assert_not_called()

    @patch.dict(os.environ)
    def test_pip_audit_no_requirements_with_nothing_to_scan_is_exit_2_by_hand(self):
        os.environ.pop("HAMS_CHECK_ALLOW_EMPTY", None)
        with patch.object(sys, "argv", self._argv()), patch("check_pip_audit.subprocess.run") as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 2)
            mock_run.assert_not_called()


    def test_pip_audit_not_installed_exits_1_with_a_clear_message(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        version_check = MagicMock(returncode=1)
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", return_value=version_check
        ) as mock_run, patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            # Only the --version check ran; the -r audit invocation never
            # should have been attempted once the install check failed.
            self.assertEqual(mock_run.call_count, 1)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("not installed", printed)

    def test_a_clean_audit_result_exits_0(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        version_check = MagicMock(returncode=0)
        audit_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", side_effect=[version_check, audit_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)

    def test_a_finding_in_the_audit_exits_1(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        version_check = MagicMock(returncode=0)
        audit_result = MagicMock(returncode=1, stdout="VULNERABILITY FOUND", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", side_effect=[version_check, audit_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_multiple_requirements_files_are_all_audited_even_after_a_finding(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        _write(os.path.join(self.tmp, "requirements-dev.txt"), "pytest==8.0\n")
        version_check = MagicMock(returncode=0)
        finding = MagicMock(returncode=1, stdout="VULN", stderr="")
        clean = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", side_effect=[version_check, finding, clean]
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            # 1 version check + 2 per-file audits, not stopped early after
            # the first file's finding.
            self.assertEqual(mock_run.call_count, 3)


class TimeoutTests(unittest.TestCase):
    """Regression tests for the `timeout=` on both subprocess.run() calls in main():
    this module's own docstring states pip-audit "Requires
    network access" -- a stalled network call (or a hung pip-audit subprocess for any other
    reason) would block the whole run_linters.py run indefinitely. Confirms each call site
    passes a real timeout, and that a subprocess.TimeoutExpired is caught cleanly (a readable
    failure message + exit 1), never an uncaught traceback."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self):
        return ["check_pip_audit.py", self.tmp]

    def test_the_version_probe_passes_a_real_timeout_kwarg(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        version_check = MagicMock(returncode=0)
        audit_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", side_effect=[version_check, audit_result]
        ) as mock_run:
            with self.assertRaises(SystemExit):
                chk.main()
            version_call_kwargs = mock_run.call_args_list[0].kwargs
            self.assertEqual(
                version_call_kwargs.get("timeout"), chk.VERSION_CHECK_TIMEOUT_SECONDS
            )

    def test_the_per_file_audit_passes_a_real_timeout_kwarg(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        version_check = MagicMock(returncode=0)
        audit_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", side_effect=[version_check, audit_result]
        ) as mock_run:
            with self.assertRaises(SystemExit):
                chk.main()
            audit_call_kwargs = mock_run.call_args_list[1].kwargs
            self.assertEqual(audit_call_kwargs.get("timeout"), chk.AUDIT_TIMEOUT_SECONDS)

    def test_a_hung_version_probe_fails_cleanly_instead_of_raising(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pip_audit --version", timeout=30),
        ), patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("did not respond", printed)

    def test_a_hung_per_file_audit_fails_that_file_cleanly_and_continues(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        _write(os.path.join(self.tmp, "requirements-dev.txt"), "pytest==8.0\n")
        version_check = MagicMock(returncode=0, stdout="pip-audit 2.7")
        calls = []

        def fake(cmd, **kwargs):
            if "--version" in cmd:
                return version_check
            calls.append(os.path.basename(cmd[cmd.index("-r") + 1]))
            if calls[-1] == "requirements.txt":
                raise subprocess.TimeoutExpired(cmd="pip_audit -r ...", timeout=300)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(sys, "argv", self._argv()), patch("check_pip_audit.subprocess.run", side_effect=fake), \
                patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            # Both files were attempted -- the hang on the first one didn't abort the loop -- and the hung one was tried twice.
            self.assertEqual(sorted(calls), ["requirements-dev.txt", "requirements.txt", "requirements.txt"])
            self.assertEqual(ctx.exception.code, 1)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("did not complete within", printed)
            self.assertIn("twice", printed)


class CacheAndRetryTests(unittest.TestCase):
    """A passing audit of an unchanged file is reused for a few hours, a stalled call is retried once, and only a pass is cached.

    Tests [@ANCHOR: check_pip_audit:cache_hours]
    Tests [@ANCHOR: check_pip_audit:cache_key]
    Tests [@ANCHOR: check_pip_audit:included_requirement_files]
    Tests [@ANCHOR: check_pip_audit:cached_pass_age_hours]
    Tests [@ANCHOR: check_pip_audit:store_pass]
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.cache, ignore_errors=True)
        self.env = {chk.CACHE_DIR_ENV: self.cache}
        self.req = os.path.join(self.tmp, "requirements.txt")
        _write(self.req, "requests==2.0\n")

    def test_hours_default_override_zero_and_bad_values(self):
        self.assertEqual(chk.cache_hours({}), 6.0)
        self.assertEqual(chk.cache_hours({chk.CACHE_HOURS_ENV: "2.5"}), 2.5)
        self.assertEqual(chk.cache_hours({chk.CACHE_HOURS_ENV: "0"}), 0.0)  # 0 turns the cache off
        for bad in ("x", "nan", "inf", "-1"):
            self.assertEqual(chk.cache_hours({chk.CACHE_HOURS_ENV: bad}), 6.0, bad)

    def test_the_key_changes_with_the_content_the_path_and_the_version(self):
        key = chk.cache_key(self.req, "pip-audit 2.7")
        self.assertEqual(len(key), 64)
        self.assertEqual(chk.cache_key(self.req, "pip-audit 2.7\n"), key)
        self.assertNotEqual(chk.cache_key(self.req, "pip-audit 2.8"), key)
        other = os.path.join(self.tmp, "sub", "requirements.txt")
        _write(other, "requests==2.0\n")
        self.assertNotEqual(chk.cache_key(other, "pip-audit 2.7"), key)
        _write(self.req, "requests==2.1\n")
        self.assertNotEqual(chk.cache_key(self.req, "pip-audit 2.7"), key)
        with self.assertRaises(OSError):
            chk.cache_key(os.path.join(self.tmp, "missing.txt"), "v")

    def test_an_edit_to_an_included_file_changes_the_key(self):
        constraints = os.path.join(self.tmp, "constraints.txt")
        base = os.path.join(self.tmp, "base", "base.txt")
        _write(constraints, "urllib3==1.0\n")
        _write(base, "idna==2.0\n")
        _write(self.req, "requests==2.0\n-r base/base.txt  # a comment\n--constraint=constraints.txt\n")
        key = chk.cache_key(self.req, "v")
        self.assertEqual(chk.cache_key(self.req, "v"), key)
        _write(constraints, "urllib3==1.1\n")
        changed = chk.cache_key(self.req, "v")
        self.assertNotEqual(changed, key)
        _write(base, "idna==2.1\n")
        self.assertNotEqual(chk.cache_key(self.req, "v"), changed)

    def test_included_files_are_found_through_every_spelling_nesting_and_cycles(self):
        a, b, c, d = (os.path.join(self.tmp, name) for name in ("a.txt", "b.txt", "c.txt", "d.txt"))
        _write(self.req, "-ra.txt\n--requirement b.txt\n-c  c.txt\n--requirements-not-an-include\nfoo==1\n")
        _write(a, "-r d.txt\n")
        _write(b, "-r requirements.txt\n")  # a cycle back to the including file
        _write(c, "x==1\n")
        _write(d, "-r a.txt\n")  # a cycle between two included files
        found = chk.included_requirement_files(self.req)
        self.assertEqual(found, [os.path.realpath(p) for p in (a, d, b, c)])

    def test_an_unreadable_url_or_too_deeply_nested_include_leaves_no_key(self):
        _write(self.req, "-r missing.txt\n")
        with self.assertRaises(OSError):
            chk.cache_key(self.req, "v")
        _write(self.req, "-r https://example.test/r.txt\n")
        with self.assertRaises(OSError):
            chk.cache_key(self.req, "v")
        for number in range(chk.MAX_INCLUDE_DEPTH + 3):
            _write(os.path.join(self.tmp, "n%d.txt" % number), "-r n%d.txt\n" % (number + 1))
        _write(os.path.join(self.tmp, "n%d.txt" % (chk.MAX_INCLUDE_DEPTH + 3)), "x==1\n")
        _write(self.req, "-r n0.txt\n")
        with self.assertRaises(OSError):
            chk.cache_key(self.req, "v")

    def test_a_stored_pass_is_found_until_it_expires_and_never_from_the_future(self):
        self.assertTrue(chk.store_pass("k", now=1000.0, environ=self.env))
        self.assertAlmostEqual(chk.cached_pass_age_hours("k", now=1000.0 + 3600, environ=self.env), 1.0)
        self.assertIsNone(chk.cached_pass_age_hours("k", now=1000.0 + 7 * 3600, environ=self.env))
        self.assertIsNone(chk.cached_pass_age_hours("k", now=500.0, environ=self.env))
        self.assertIsNone(chk.cached_pass_age_hours("unknown", now=1001.0, environ=self.env))
        self.assertIsNone(chk.cached_pass_age_hours("k", now=1001.0, environ=dict(self.env, **{chk.CACHE_HOURS_ENV: "0"})))
        self.assertFalse(chk.store_pass("k2", environ=dict(self.env, **{chk.CACHE_HOURS_ENV: "0"})))
        self.assertFalse(chk.store_pass("k3", environ={chk.CACHE_DIR_ENV: "/proc/no/such/dir"}))

    def _run_main(self, audit):
        calls = []

        def fake(cmd, **kwargs):
            if "--version" in cmd:
                return MagicMock(returncode=0, stdout="pip-audit 2.7\n")
            calls.append(cmd)
            return audit(len(calls))

        with patch.dict(os.environ, self.env), patch.object(sys, "argv", ["check_pip_audit.py", self.tmp]), \
                patch("check_pip_audit.subprocess.run", side_effect=fake), patch("builtins.print") as shown:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
        return ctx.exception.code, calls, " ".join(str(c) for c in shown.call_args_list)

    def test_a_pass_is_reused_and_a_changed_file_is_audited_again(self):
        clean = lambda n: MagicMock(returncode=0, stdout="", stderr="")  # noqa: E731
        self.assertEqual(self._run_main(clean)[:2][0], 0)
        code, calls, printed = self._run_main(clean)
        self.assertEqual((code, len(calls)), (0, 0))
        self.assertIn("reusing that result", printed)
        _write(self.req, "requests==2.1\n")
        self.assertEqual(len(self._run_main(clean)[1]), 1)

    def test_a_finding_or_a_double_stall_is_never_cached_and_one_stall_is_retried(self):
        finding = lambda n: MagicMock(returncode=1, stdout="VULN", stderr="")  # noqa: E731
        self.assertEqual(self._run_main(finding)[0], 1)
        self.assertEqual(self._run_main(finding)[0], 1)

        def stall(n):
            raise subprocess.TimeoutExpired(cmd="pip_audit", timeout=300)

        code, calls, printed = self._run_main(stall)
        self.assertEqual((code, len(calls)), (1, 2))
        self.assertIn("twice", printed)

        def stall_once(n):
            if n == 1:
                raise subprocess.TimeoutExpired(cmd="pip_audit", timeout=300)
            return MagicMock(returncode=0, stdout="", stderr="")

        code, calls, _ = self._run_main(stall_once)
        self.assertEqual((code, len(calls)), (0, 2))


class ResolveRepoRootTests(unittest.TestCase):
    # Regression test for a real bug found the same night check_model_extension_collisions.py's
    # own identical bug was found and fixed: run_linters.py's own dir_path resolves to
    # .../hams_shared, not a real repo root -- confirmed directly, this checker (this codebase's
    # Python supply-chain vulnerability gate) was silently finding 0 requirements.txt files via
    # run_linters.py's actual invocation, versus 2 at a real repo root.
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
    # Regression test for the second half of the same repo-root redirect problem:
    # _resolve_repo_root's redirect only ever lands on ONE repo (hams_shared's literal parent),
    # but real requirements.txt files exist in both hams_open's own root and hams_com/daemons/ --
    # confirmed directly, run_linters.py's own real invocation was catching hams_open's file but
    # silently missing both hams_com ones.
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
