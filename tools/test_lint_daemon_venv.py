#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_daemon_venv (the cached isolated venv of a daemon's lint step) and the run_linters helpers that
route hams_simulated_bots' pytest step through it.

Tests [@ANCHOR: lint_daemon_venv:venv_dir]
Tests [@ANCHOR: lint_daemon_venv:ensure_venv]
Tests [@ANCHOR: lint_daemon_venv:main]
Tests [@ANCHOR: lint_daemon_venv:prune_venvs]
Tests [@ANCHOR: run_linters:ISOLATED_VENV_DIRS]
Tests [@ANCHOR: run_linters:isolated_venv_marker_for]
Tests [@ANCHOR: run_linters:pytest_argv_prefix]
"""

import os
import subprocess
import tempfile
import time
import unittest

import lint_daemon_venv
import run_linters


class VenvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_CACHE_HOME": os.path.join(self.tmp.name, "cache")}
        self.req = os.path.join(self.tmp.name, "requirements.txt")
        self.write_requirements("# nothing to install\n")

    def write_requirements(self, text):
        with open(self.req, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_venv_dir_is_keyed_on_name_python_and_requirements_content(self):
        first = lint_daemon_venv.venv_dir("bots", self.req, self.env, (3, 13, 5))
        self.assertTrue(first.startswith(os.path.join(self.tmp.name, "cache", "hams-lint-venvs", "bots-py313-")))
        self.assertEqual(first, lint_daemon_venv.venv_dir("bots", self.req, self.env, (3, 13, 9)))
        self.assertNotEqual(first, lint_daemon_venv.venv_dir("bots", self.req, self.env, (3, 12, 1)))
        self.assertNotEqual(first, lint_daemon_venv.venv_dir("other", self.req, self.env, (3, 13, 5)))
        self.write_requirements("requests\n")
        self.assertNotEqual(first, lint_daemon_venv.venv_dir("bots", self.req, self.env, (3, 13, 5)))

    def test_ensure_venv_builds_once_then_reuses_and_the_venv_is_isolated(self):
        python = lint_daemon_venv.ensure_venv("bots", self.req, self.env)
        self.assertTrue(os.access(python, os.X_OK))
        out = subprocess.run([python, "-c", "import sys; print(sys.prefix != sys.base_prefix)"], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "True")
        calls = []
        self.assertEqual(lint_daemon_venv.ensure_venv("bots", self.req, self.env, run=lambda *a, **k: calls.append(a)), python)
        self.assertEqual(calls, [], "a ready venv is reused without running anything")
        self.assertFalse(os.path.exists(lint_daemon_venv.venv_dir("bots", self.req, self.env) + ".partial"))

    def test_a_failed_build_raises_with_the_output_and_leaves_no_ready_venv(self):
        def failing(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="out text\n", stderr="pip said no\n")

        with self.assertRaises(RuntimeError) as caught:
            lint_daemon_venv.ensure_venv("bots", self.req, self.env, run=failing)
        self.assertIn("pip said no", str(caught.exception))
        target = lint_daemon_venv.venv_dir("bots", self.req, self.env)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".partial"))

    def test_main_execs_the_venv_python_with_the_arguments_after_the_double_dash(self):
        seen = []
        status = lint_daemon_venv.main(
            ["--name", "bots", "--requirements", self.req, "--", "-m", "pytest", "-q", "a.py"],
            self.env,
            execv=lambda path, argv: seen.append((path, argv)),
        )
        self.assertEqual(status, 0)
        path, argv = seen[0]
        self.assertEqual(path, lint_daemon_venv.venv_dir("bots", self.req, self.env) + "/bin/python3")
        self.assertEqual(argv, [path, "-m", "pytest", "-q", "a.py"])

    def test_main_returns_2_and_prints_when_the_build_fails(self):
        missing = os.path.join(self.tmp.name, "absent.txt")
        status = lint_daemon_venv.main(["--name", "bots", "--requirements", missing, "--", "-c", "1"], self.env, execv=lambda *a: self.fail("exec"))
        self.assertEqual(status, 2)

    def make_cached_venv(self, name, digest, age_seconds, py="py313"):
        """A fake complete cached venv whose last use (marker mtime) was age_seconds ago."""
        path = os.path.join(self.tmp.name, "cache", "hams-lint-venvs", f"{name}-{py}-{digest * 16}")
        os.makedirs(path)
        marker = os.path.join(path, lint_daemon_venv.READY_MARKER)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("x\n")
        when = time.time() - age_seconds
        os.utime(marker, (when, when))
        return path

    def test_prune_keeps_the_newest_n_of_one_name_and_never_touches_another_name(self):
        day = 86400
        keep_new = self.make_cached_venv("bots", "a", 1 * day)
        keep_next = self.make_cached_venv("bots", "b", 2 * day)
        drop_old = self.make_cached_venv("bots", "c", 3 * day)
        drop_older = self.make_cached_venv("bots", "d", 9 * day, py="py312")
        other = self.make_cached_venv("bots_fine_tuning", "e", 30 * day)
        removed = lint_daemon_venv.prune_venvs("bots", keep=2, environ=self.env)
        self.assertEqual(sorted(removed), sorted([drop_old, drop_older]))
        for path in (keep_new, keep_next, other):
            self.assertTrue(os.path.isdir(path), path)
        for path in (drop_old, drop_older):
            self.assertFalse(os.path.exists(path))

    def test_prune_spares_a_recently_used_venv_a_locked_one_and_a_young_partial_but_removes_an_old_partial(self):
        day = 86400
        self.make_cached_venv("bots", "a", 1 * day)
        self.make_cached_venv("bots", "b", 2 * day)
        recent = self.make_cached_venv("bots", "c", 60)  # used a minute ago: newest, kept anyway
        locked = self.make_cached_venv("bots", "d", 5 * day)
        base = os.path.join(self.tmp.name, "cache", "hams-lint-venvs")
        old_partial = os.path.join(base, "bots-py313-" + "f" * 16 + ".partial")
        young_partial = os.path.join(base, "bots-py313-" + "9" * 16 + ".partial")
        os.makedirs(old_partial)
        os.makedirs(young_partial)
        long_ago = time.time() - 2 * day
        os.utime(old_partial, (long_ago, long_ago))
        with open(locked + ".lock", "w", encoding="utf-8") as lock:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)  # a build (or run) holds it
            removed = lint_daemon_venv.prune_venvs("bots", keep=1, environ=self.env)
        self.assertTrue(os.path.isdir(recent) and os.path.isdir(locked) and os.path.isdir(young_partial))
        self.assertFalse(os.path.exists(old_partial))
        self.assertIn(old_partial, removed)
        self.assertNotIn(locked, removed)
        # a venv within min_age of its last use survives even outside the newest n
        self.assertEqual(lint_daemon_venv.prune_venvs("bots", keep=0, environ=self.env, min_age=10 * day), [])

    def test_prune_with_no_cache_directory_removes_nothing(self):
        self.assertEqual(lint_daemon_venv.prune_venvs("bots", environ=self.env), [])

    def test_ensure_venv_marks_use_and_prunes_old_venvs_of_its_name_after_a_build(self):
        day = 86400
        old = [self.make_cached_venv("bots", d, (3 + i) * day) for i, d in enumerate("abc")]
        python = lint_daemon_venv.ensure_venv("bots", self.req, self.env)
        self.assertTrue(os.access(python, os.X_OK))
        surviving = [p for p in old if os.path.isdir(p)]
        self.assertEqual(len(surviving), lint_daemon_venv.KEEP_VENVS - 1)  # the new venv plus the newest old one
        marker = os.path.join(lint_daemon_venv.venv_dir("bots", self.req, self.env), lint_daemon_venv.READY_MARKER)
        past = time.time() - 5 * day
        os.utime(marker, (past, past))
        lint_daemon_venv.ensure_venv("bots", self.req, self.env)
        self.assertGreater(os.path.getmtime(marker), time.time() - 60)


class RunLintersRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "daemons")
        for sub in ("hams_simulated_bots/tests", "hams_simulated_bots/fine_tuning/tests", "other/tests"):
            os.makedirs(os.path.join(self.root, sub))
        with open(os.path.join(self.root, "hams_simulated_bots", "requirements.txt"), "w", encoding="utf-8") as handle:
            handle.write("av\n")
        with open(os.path.join(self.root, "hams_simulated_bots", "fine_tuning", "requirements.txt"), "w", encoding="utf-8") as handle:
            handle.write("transformers\n")
        with open(os.path.join(self.root, "other", "requirements.txt"), "w", encoding="utf-8") as handle:
            handle.write("requests\n")

    def test_the_nearest_listed_directory_with_a_requirements_file_wins(self):
        self.assertEqual(run_linters.ISOLATED_VENV_DIRS, ("hams_simulated_bots", "hams_simulated_bots/fine_tuning"))
        bots = run_linters.ISOLATED_VENV_PREFIX + "hams_simulated_bots"
        tuning = run_linters.ISOLATED_VENV_PREFIX + "hams_simulated_bots/fine_tuning"
        self.assertEqual(run_linters.isolated_venv_marker_for(os.path.join(self.root, "hams_simulated_bots", "tests"), self.root), bots)
        self.assertEqual(run_linters.isolated_venv_marker_for(os.path.join(self.root, "hams_simulated_bots"), self.root), bots)
        self.assertEqual(run_linters.isolated_venv_marker_for(os.path.join(self.root, "hams_simulated_bots", "fine_tuning", "tests"), self.root), tuning)
        self.assertIsNone(run_linters.isolated_venv_marker_for(os.path.join(self.root, "other", "tests"), self.root))
        self.assertIsNone(run_linters.isolated_venv_marker_for(self.root, self.root))

    def test_a_directory_without_requirements_txt_is_skipped_for_the_next_one_up(self):
        os.remove(os.path.join(self.root, "hams_simulated_bots", "fine_tuning", "requirements.txt"))
        bots = run_linters.ISOLATED_VENV_PREFIX + "hams_simulated_bots"
        self.assertEqual(run_linters.isolated_venv_marker_for(os.path.join(self.root, "hams_simulated_bots", "fine_tuning", "tests"), self.root), bots)
        os.remove(os.path.join(self.root, "hams_simulated_bots", "requirements.txt"))
        self.assertIsNone(run_linters.isolated_venv_marker_for(os.path.join(self.root, "hams_simulated_bots", "tests"), self.root))

    def test_pytest_argv_prefix_for_a_marker_and_for_a_plain_interpreter(self):
        marker = run_linters.ISOLATED_VENV_PREFIX + "hams_simulated_bots"
        argv = run_linters.pytest_argv_prefix(marker, "/usr/bin/python3", "/repo")
        self.assertEqual(
            argv,
            ["/usr/bin/python3", "/repo/tools/lint_daemon_venv.py", "--name", "hams_simulated_bots",
             "--requirements", "/repo/daemons/hams_simulated_bots/requirements.txt", "--", "-m", "pytest", "-q"],
        )
        nested = run_linters.pytest_argv_prefix(run_linters.ISOLATED_VENV_PREFIX + "hams_simulated_bots/fine_tuning", "/usr/bin/python3", "/repo")
        self.assertEqual(nested[2:6], ["--name", "hams_simulated_bots_fine_tuning", "--requirements", "/repo/daemons/hams_simulated_bots/fine_tuning/requirements.txt"])
        self.assertEqual(run_linters.pytest_argv_prefix("/x/.venv/bin/python3", "/usr/bin/python3", "/repo"), ["/x/.venv/bin/python3", "-m", "pytest", "-q"])


if __name__ == "__main__":
    unittest.main()
