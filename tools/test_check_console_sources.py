#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for check_console_sources.py with a fake `cargo` on PATH, so no Rust build is needed.

Tests [@ANCHOR: check_console_sources:main]
"""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from unittest import mock

import check_console_sources as ccs


class MainTests(unittest.TestCase):
    def _repo(self, tmp):
        crate = os.path.join(tmp, "repo", "daemons", "shack_console")
        os.makedirs(crate)
        with open(os.path.join(crate, "Cargo.toml"), "w", encoding="utf-8") as out:
            out.write('[package]\nname = "x"\n\n[build-dependencies]\nsha2 = "0.10"\n# note\nflate2 = "1"\n\n[dev-dependencies]\ntower = "0.5"\n')
        with open(os.path.join(crate, "Cargo.lock"), "w", encoding="utf-8") as out:
            out.write("# lock\n")
        return os.path.join(tmp, "repo")

    def _fake_cargo(self, tmp, code):
        path = os.path.join(tmp, "bin")
        os.makedirs(path)
        script = os.path.join(path, "cargo")
        with open(script, "w", encoding="utf-8") as out:
            out.write("#!/bin/sh\nexit %d\n" % code)
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        return path

    def _run(self, repo, tmp, code):
        env = {"PATH": self._fake_cargo(tmp, code) + os.pathsep + os.environ["PATH"], "CARGO_TARGET_DIR": os.path.join(tmp, "target")}
        with mock.patch.dict(os.environ, env):
            return ccs.main([repo])

    def test_no_crate_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
                ccs.main([tmp])
        self.assertEqual(raised.exception.code, 2)

    def test_missing_repo_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
                ccs.main([os.path.join(tmp, "absent")])
        self.assertEqual(raised.exception.code, 2)

    def test_clean_sources_exit_zero_and_project_has_build_deps_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(self._repo(tmp), tmp, 0), 0)
            with open(os.path.join(tmp, "target", "console_sources_project", "Cargo.toml"), encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn('sha2 = "0.10"', text)
            self.assertIn('flate2 = "1"', text)
            self.assertNotIn("tower", text)
            self.assertNotIn("# note", text)

    def test_problems_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(self._repo(tmp), tmp, 1), 1)


if __name__ == "__main__":
    unittest.main()
