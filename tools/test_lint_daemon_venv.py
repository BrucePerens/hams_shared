#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_daemon_venv (the cached isolated venv of a daemon's lint step) and the run_linters helpers that
route hams_simulated_bots' pytest step through it.

Tests [@ANCHOR: lint_daemon_venv:venv_dir]
Tests [@ANCHOR: lint_daemon_venv:ensure_venv]
Tests [@ANCHOR: lint_daemon_venv:main]
Tests [@ANCHOR: run_linters:ISOLATED_VENV_DAEMONS]
Tests [@ANCHOR: run_linters:isolated_venv_marker_for]
Tests [@ANCHOR: run_linters:pytest_argv_prefix]
"""

import os
import subprocess
import tempfile
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


class RunLintersRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "daemons")
        for sub in ("hams_simulated_bots/tests", "hams_simulated_bots/fine_tuning/tests", "other/tests"):
            os.makedirs(os.path.join(self.root, sub))
        with open(os.path.join(self.root, "hams_simulated_bots", "requirements.txt"), "w", encoding="utf-8") as handle:
            handle.write("av\n")
        with open(os.path.join(self.root, "other", "requirements.txt"), "w", encoding="utf-8") as handle:
            handle.write("requests\n")

    def test_only_listed_daemons_own_directories_get_the_isolated_venv(self):
        self.assertEqual(run_linters.ISOLATED_VENV_DAEMONS, ("hams_simulated_bots",))
        marker = run_linters.ISOLATED_VENV_PREFIX + "hams_simulated_bots"
        self.assertEqual(run_linters.isolated_venv_marker_for(os.path.join(self.root, "hams_simulated_bots", "tests"), self.root), marker)
        self.assertEqual(run_linters.isolated_venv_marker_for(os.path.join(self.root, "hams_simulated_bots"), self.root), marker)
        self.assertIsNone(run_linters.isolated_venv_marker_for(os.path.join(self.root, "hams_simulated_bots", "fine_tuning", "tests"), self.root))
        self.assertIsNone(run_linters.isolated_venv_marker_for(os.path.join(self.root, "other", "tests"), self.root))

    def test_a_daemon_without_requirements_txt_is_not_matched(self):
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
        self.assertEqual(run_linters.pytest_argv_prefix("/x/.venv/bin/python3", "/usr/bin/python3", "/repo"), ["/x/.venv/bin/python3", "-m", "pytest", "-q"])


if __name__ == "__main__":
    unittest.main()
