#!/usr/bin/env python3
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

    def test_ignores_site_packages_regardless_of_the_venvs_own_directory_name(self):
        """A venv's own outer directory can be named anything ("whisper_venv", not just "venv" or
        ".venv") -- matches check_float_isfinite.py's own real-bug regression test."""
        vendored = os.path.join(self.tmp, "whisper_venv", "lib", "python3.13", "site-packages", "pkg")
        _write(os.path.join(vendored, "Cargo.toml"), "[package]\nname = \"pkg\"\n")
        crate = os.path.join(self.tmp, "daemons", "some_daemon")
        _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"some_daemon\"\n")
        self.assertEqual(chk.find_cargo_crates(self.tmp), [crate])

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

    def test_a_concurrent_sessions_claude_worktree_is_never_walked(self):
        """Real, live bug found reviewing this checker: IGNORE_DIR_NAMES has no ".claude" entry
        and no generic dot-directory exclusion, unlike several sibling checkers in this same tree
        (check_dead_code.py, check_minified_js_nested_templates.py, etc., which all prune `not
        d.startswith(".")`). This project's own standing convention runs concurrent bug-hunt
        dispatches in isolated git worktrees under `.claude/worktrees/<session>/` INSIDE the repo
        root -- confirmed live while reviewing this exact file: running find_cargo_crates against
        the real hams_com checkout during this session found the same daemon crates listed
        THREE times over, once for the real repo and once each for two other, real, concurrently
        running sessions' own worktrees. Beyond wasted work (cargo clippy is slow), a concurrent
        session's in-progress, uncommitted Rust code failing to build or having real clippy
        findings -- completely normal for work in progress -- would be reported as a finding
        against the wrong repo entirely, a spurious CI failure with no relationship to the actual
        code under review."""
        real_crate = os.path.join(self.tmp, "daemons", "real_daemon")
        _write(os.path.join(real_crate, "Cargo.toml"), "[package]\nname = \"real_daemon\"\n")
        worktree_crate = os.path.join(
            self.tmp, ".claude", "worktrees", "agent-other-session", "daemons", "real_daemon"
        )
        _write(os.path.join(worktree_crate, "Cargo.toml"), "[package]\nname = \"real_daemon\"\n")
        self.assertEqual(chk.find_cargo_crates(self.tmp), [real_crate])

    def test_the_riscv_bench_crate_that_needs_a_generated_speech_file_is_excluded(self):
        # Its main.rs include_bytes!s a gitignored file that prep.sh generates, so it cannot build on a clean checkout.
        bench = os.path.join(self.tmp, "daemons", "ham_digital_modes", "tools", "codec2_riscv32_bench")
        _write(os.path.join(bench, "Cargo.toml"), "[package]\nname = \"codec2_riscv32_bench\"\n")
        real = os.path.join(self.tmp, "daemons", "ham_digital_modes")
        _write(os.path.join(real, "Cargo.toml"), "[package]\nname = \"ham_digital_modes\"\n")
        self.assertEqual(chk.find_cargo_crates(self.tmp), [real])

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
        # A real clippy warning always follows "warning: ..." with a " --> " source
        # pointer line -- that's what distinguishes it from a build script's own
        # "cargo:warning=..." relay, which never has one.
        self._make_crate()
        version_check = MagicMock(returncode=0)
        clippy_result = MagicMock(
            returncode=0, stdout="", stderr="warning: unused variable\n --> src/lib.rs:1:1"
        )
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
        finding = MagicMock(
            returncode=0, stdout="", stderr="warning: unused variable\n --> src/lib.rs:1:1"
        )
        clean = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run",
            side_effect=[version_check, finding, clean],
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(mock_run.call_count, 3)


class PurgeStaleBuildScriptOutputTests(unittest.TestCase):
    """A build script's cached output, shared by every worktree that shares one target dir, must
    not be reused by a checkout it was not generated in (bench_mcp's build.rs embeds absolute
    include_str! paths: the second worktree failed to compile once the first was removed)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.here = os.path.join(self.tmp, "wt_here")
        self.other = os.path.join(self.tmp, "wt_removed")
        self.crate = os.path.join(self.here, "daemons", "bench_mcp")
        self.target = os.path.join(self.tmp, "target")
        os.makedirs(self.crate)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_dir(self, name, generated_in, profile=("debug",), crate="daemons/bench_mcp"):
        run_dir = os.path.join(self.target, *profile, "build", name)
        _write(os.path.join(run_dir, "output"), "cargo:rerun-if-changed=manual\n")
        _write(
            os.path.join(run_dir, "out", "manual_files.rs"),
            f'pub const FILES: &[(&str, &str)] = &[("a.md", include_str!("{generated_in}/{crate}/manual/a.md"))];\n',
        )
        fingerprint = os.path.join(self.target, *profile, ".fingerprint", name)
        _write(os.path.join(fingerprint, "run-build-script-build-script-build"), "x")
        return run_dir, fingerprint

    def _purge(self):
        return chk.purge_stale_build_script_output(self.target, self.here, self.crate)

    def test_output_generated_in_a_removed_worktree_is_deleted_with_its_fingerprint(self):
        run_dir, fingerprint = self._run_dir("bench_mcp-0123456789abcdef", self.other)
        self.assertEqual(self._purge(), [run_dir])
        self.assertFalse(os.path.exists(run_dir))
        self.assertFalse(os.path.exists(fingerprint))

    def test_output_generated_in_another_worktree_that_still_exists_is_deleted_too(self):
        # Silently building from the other worktree's files is the same bug without the failure.
        os.makedirs(os.path.join(self.other, "daemons", "bench_mcp", "manual"))
        run_dir, _ = self._run_dir("bench_mcp-0123456789abcdef", self.other)
        self.assertEqual(self._purge(), [run_dir])

    def test_output_generated_in_this_checkout_is_kept(self):
        run_dir, fingerprint = self._run_dir("bench_mcp-0123456789abcdef", self.here)
        self.assertEqual(self._purge(), [])
        self.assertTrue(os.path.isdir(run_dir))
        self.assertTrue(os.path.isdir(fingerprint))

    def test_a_path_that_only_shares_a_prefix_with_this_checkout_is_another_checkout(self):
        run_dir, _ = self._run_dir("bench_mcp-0123456789abcdef", self.here + "_2")
        self.assertEqual(self._purge(), [run_dir])

    def test_output_of_a_different_crate_is_kept(self):
        run_dir, _ = self._run_dir("other-0123456789abcdef", self.other, crate="daemons/other")
        self.assertEqual(self._purge(), [])
        self.assertTrue(os.path.isdir(run_dir))

    def test_a_target_triple_directory_is_searched_too(self):
        run_dir, _ = self._run_dir("bench_mcp-0123456789abcdef", self.other, profile=("x86_64-unknown-linux-gnu", "debug"))
        self.assertEqual(self._purge(), [run_dir])

    def test_one_memo_serves_every_crate_and_reads_each_run_dir_once(self):
        other_crate = os.path.join(self.here, "daemons", "other")
        os.makedirs(other_crate)
        run_dir, _ = self._run_dir("bench_mcp-0123456789abcdef", self.here)
        memo = {}
        reads = []
        original = chk._cached_text

        def counting(path):
            reads.append(path)
            return original(path)

        with patch.object(chk, "_cached_text", counting):
            self.assertEqual(chk.purge_stale_build_script_output(self.target, self.here, self.crate, memo), [])
            self.assertEqual(chk.purge_stale_build_script_output(self.target, self.here, other_crate, memo), [])
        self.assertEqual(reads, [run_dir])  # the second crate reused the first crate's read

    def test_a_rewritten_output_is_read_again_and_a_stale_one_is_purged(self):
        run_dir, _ = self._run_dir("bench_mcp-0123456789abcdef", self.here)
        memo = {}
        self.assertEqual(chk.purge_stale_build_script_output(self.target, self.here, self.crate, memo), [])
        # another checkout's build rewrites the script's output (a later mtime): the memo must not hide it
        _write(os.path.join(run_dir, "out", "manual_files.rs"), f'include_str!("{self.other}/daemons/bench_mcp/manual/a.md")\n')
        later = os.stat(os.path.join(run_dir, "output")).st_mtime_ns + 5_000_000_000
        os.utime(os.path.join(run_dir, "output"), ns=(later, later))
        self.assertEqual(chk.purge_stale_build_script_output(self.target, self.here, self.crate, memo), [run_dir])

    def test_binary_output_and_a_missing_target_directory_are_ignored(self):
        run_dir = os.path.join(self.target, "debug", "build", "ring-0123456789abcdef")
        _write(os.path.join(run_dir, "output"), "")
        os.makedirs(os.path.join(run_dir, "out"))
        with open(os.path.join(run_dir, "out", "blob.a"), "wb") as f:
            f.write(b"\0" + f"{self.other}/daemons/bench_mcp/x".encode())
        self.assertEqual(self._purge(), [])
        self.assertEqual(
            chk.purge_stale_build_script_output(os.path.join(self.tmp, "nowhere"), self.here, self.crate), []
        )

    def test_main_purges_before_it_runs_clippy_and_says_so(self):
        _write(os.path.join(self.crate, "Cargo.toml"), "[package]\nname = \"bench_mcp\"\n")
        run_dir, _ = self._run_dir("bench_mcp-0123456789abcdef", self.other)
        seen = {}

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["cargo", "clippy"] and "--version" not in cmd:
                seen["run_dir_existed_at_clippy"] = os.path.exists(run_dir)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(sys, "argv", ["check_cargo_clippy.py", self.here]), patch.dict(
            os.environ, {"CARGO_TARGET_DIR": self.target}
        ), patch("check_cargo_clippy.subprocess.run", side_effect=fake_run), patch(
            "builtins.print"
        ) as mock_print:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
        self.assertEqual(ctx.exception.code, 0)
        self.assertFalse(seen["run_dir_existed_at_clippy"])
        self.assertIn("another checkout", " ".join(str(c) for c in mock_print.call_args_list))


class KeepOutputThatOnlyNamesAnotherCheckoutTests(unittest.TestCase):
    """Alternating between two checkouts that share one target directory cost 210-280 s per switch because the run
    directories of `opusic-sys` and `hams_local_relay` (C builds) name the other checkout in files that only the script
    itself reads. Those, and `rerun-if-changed` lines, are kept when the same sources exist unchanged in this checkout;
    anything the compiler consumes (a `cargo:` directive, a generated .rs) is still purged."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.here = os.path.join(self.tmp, "wt_here")
        self.other = os.path.join(self.tmp, "wt_other")
        self.target = os.path.join(self.tmp, "target")
        self.relpath = "daemons/relay"
        self.crate = os.path.join(self.here, self.relpath)
        for root in (self.here, self.other):
            _write(os.path.join(root, self.relpath, "vendor", "opus", "CMakeLists.txt"), "project(opus)\n")
            _write(os.path.join(root, self.relpath, "vendor", "opus", "src", "a.c"), "int a;\n")
        self.run_dir = os.path.join(self.target, "debug", "build", "relay-0123456789abcdef")
        self.stamp = 2_000_000_000  # seconds; every source below is older
        for root in (self.here, self.other):
            self._age(root, self.stamp - 1000)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _age(self, root, seconds):
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                os.utime(os.path.join(dirpath, n), (seconds, seconds))

    def _run(self, output="", scratch=None, generated=None):
        _write(os.path.join(self.run_dir, "output"), output)
        _write(os.path.join(self.run_dir, "invoked.timestamp"), "")
        if scratch is not None:
            _write(os.path.join(self.run_dir, "out", "build", "CMakeCache.txt"), scratch)
        if generated is not None:
            _write(os.path.join(self.run_dir, "out", "gen.rs"), generated)
        for name in ("output", "invoked.timestamp"):
            os.utime(os.path.join(self.run_dir, name), (self.stamp, self.stamp))
        _write(os.path.join(self.target, "debug", ".fingerprint", "relay-0123456789abcdef", "f"), "x")

    def _purge(self):
        kept = []
        removed = chk.purge_stale_build_script_output(self.target, self.here, self.crate, None, kept)
        return removed, kept

    def _rerun_line(self):
        return f"cargo:rerun-if-changed={self.other}/{self.relpath}/vendor/opus\n"

    def _scratch(self):
        return f"CMAKE_HOME_DIRECTORY:INTERNAL={self.other}/{self.relpath}/vendor/opus\n"

    def test_rerun_if_changed_naming_the_other_checkout_is_kept_when_the_files_are_the_same(self):
        self._run(self._rerun_line())
        self.assertEqual(self._purge(), ([], [self.run_dir]))
        self.assertTrue(os.path.isdir(self.run_dir))

    def test_a_private_build_tree_naming_the_other_checkout_is_kept_when_the_sources_are_the_same(self):
        self._run("", scratch=self._scratch())
        self.assertEqual(self._purge(), ([], [self.run_dir]))

    def test_a_file_that_differs_between_the_checkouts_purges_it(self):
        self._run(self._rerun_line(), scratch=self._scratch())
        _write(os.path.join(self.here, self.relpath, "vendor", "opus", "src", "a.c"), "int b;\n")
        self._age(self.here, self.stamp - 1000)
        self.assertEqual(self._purge(), ([self.run_dir], []))
        self.assertFalse(os.path.exists(self.run_dir))
        self.assertFalse(os.path.exists(os.path.join(self.target, "debug", ".fingerprint", "relay-0123456789abcdef")))

    def test_a_file_changed_here_after_the_script_ran_purges_it(self):
        # cargo reruns the script for this checkout: the other checkout's CMake cache would break that rerun
        self._run("", scratch=self._scratch())
        os.utime(os.path.join(self.here, self.relpath, "vendor", "opus", "src", "a.c"), (self.stamp + 5, self.stamp + 5))
        self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_a_file_changed_in_the_other_checkout_after_the_script_ran_purges_it(self):
        self._run(self._rerun_line())
        os.utime(os.path.join(self.other, self.relpath, "vendor", "opus", "src", "a.c"), (self.stamp + 5, self.stamp + 5))
        self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_an_extra_file_in_one_checkout_purges_it(self):
        self._run(self._rerun_line())
        _write(os.path.join(self.here, self.relpath, "vendor", "opus", "src", "new.c"), "int c;\n")
        self._age(self.here, self.stamp - 1000)
        self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_a_removed_other_checkout_purges_it(self):
        self._run(self._rerun_line(), scratch=self._scratch())
        shutil.rmtree(self.other)
        self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_a_cargo_directive_naming_the_other_checkout_is_always_purged(self):
        self._run(f"cargo:rustc-env=SRC={self.other}/{self.relpath}/vendor/opus\n")
        self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_a_generated_source_naming_the_other_checkout_is_always_purged(self):
        self._run("", generated=f'include_str!("{self.other}/{self.relpath}/vendor/opus/CMakeLists.txt")\n')
        self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_one_harmless_and_one_consumed_name_together_are_purged(self):
        self._run(self._rerun_line() + f"cargo:rustc-link-search={self.other}/{self.relpath}/vendor/opus\n")
        self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_a_watched_path_outside_the_other_checkout_is_purged(self):
        self._run(f"cargo:rerun-if-changed={self.other}/{self.relpath}/../../../elsewhere\n")
        self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_a_symlink_that_differs_purges_it(self):
        os.symlink("a", os.path.join(self.other, self.relpath, "vendor", "opus", "link"))
        os.symlink("b", os.path.join(self.here, self.relpath, "vendor", "opus", "link"))
        self._run(self._rerun_line())
        self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_a_tree_over_the_comparison_budget_is_purged(self):
        self._run(self._rerun_line())
        with patch.object(chk, "_MAX_COMPARED_BYTES", 3):
            self.assertEqual(self._purge(), ([self.run_dir], []))

    def test_a_failed_build_with_kept_output_removes_it_and_builds_once_more(self):
        _write(os.path.join(self.crate, "Cargo.toml"), "[package]\nname = \"relay\"\n")
        self._run(self._rerun_line(), scratch=self._scratch())
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(os.path.exists(self.run_dir))
            return MagicMock(returncode=101 if len(calls) == 1 else 0, stdout="", stderr="")

        with patch.dict(os.environ, {"CARGO_TARGET_DIR": self.target}), patch(
            "check_cargo_clippy.subprocess.run", side_effect=fake_run
        ), patch("builtins.print") as mock_print:
            failed = chk._clippy_all([(self.here, self.crate)])
        self.assertFalse(failed)
        self.assertEqual(calls, [True, False])
        self.assertIn("building again", " ".join(str(c) for c in mock_print.call_args_list))

    def test_a_failed_build_with_nothing_kept_is_not_repeated(self):
        _write(os.path.join(self.crate, "Cargo.toml"), "[package]\nname = \"relay\"\n")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=101, stdout="", stderr="")

        with patch.dict(os.environ, {"CARGO_TARGET_DIR": self.target}), patch(
            "check_cargo_clippy.subprocess.run", side_effect=fake_run
        ), patch("builtins.print"):
            self.assertTrue(chk._clippy_all([(self.here, self.crate)]))
        self.assertEqual(len(calls), 1)


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
