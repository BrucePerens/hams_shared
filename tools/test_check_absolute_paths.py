#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_absolute_paths.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_absolute_paths as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_absolute_paths.py")
_HOME = "/h" + "ome/bruce/workspace"


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CheckAbsolutePathsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_hardcoded_home_path_is_flagged_with_file_and_line(self):
        _write(os.path.join(self.tmp, "script.py"), f"path = '{_HOME}/thing'\n")
        violations = chk.check_absolute_paths(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("script.py:1", violations[0])

    def test_a_file_with_no_home_path_is_clean(self):
        _write(os.path.join(self.tmp, "script.py"), "path = '/tmp/thing'\n")
        self.assertEqual(chk.check_absolute_paths(self.tmp), [])

    def test_multiple_occurrences_across_lines_are_all_flagged(self):
        _write(
            os.path.join(self.tmp, "script.py"),
            f"a = '{_HOME}/one'\nb = 'clean'\nc = '{_HOME}/two'\n",
        )
        violations = chk.check_absolute_paths(self.tmp)
        self.assertEqual(len(violations), 2)
        self.assertIn("script.py:1", violations[0])
        self.assertIn("script.py:3", violations[1])

    def test_a_disallowed_extension_is_never_scanned(self):
        _write(os.path.join(self.tmp, "image.png"), f"{_HOME}/binary-looking-content")
        self.assertEqual(chk.check_absolute_paths(self.tmp), [])

    def test_a_makefile_with_no_extension_is_scanned(self):
        _write(os.path.join(self.tmp, "Makefile"), f"BUILD_DIR={_HOME}/build\n")
        violations = chk.check_absolute_paths(self.tmp)
        self.assertEqual(len(violations), 1)

    def test_an_ignored_directory_is_never_walked(self):
        _write(os.path.join(self.tmp, "node_modules", "pkg", "index.js"), f"'{_HOME}'")
        self.assertEqual(chk.check_absolute_paths(self.tmp), [])

    def test_mypy_cache_is_specifically_excluded(self):
        # Documented rationale: .mypy_cache's own .json files legitimately
        # embed real absolute paths as cached type-check data, not source.
        _write(os.path.join(self.tmp, ".mypy_cache", "3.13", "foo.json"), f'"{_HOME}"')
        self.assertEqual(chk.check_absolute_paths(self.tmp), [])

    def test_archive_directory_is_specifically_excluded(self):
        # Documented rationale: archive/ holds frozen historical pipeline
        # snapshots, not live source -- a real path baked into an old
        # artifact isn't a bug to fix, and rewriting it would make the
        # archive inaccurate.
        _write(os.path.join(self.tmp, "archive", "run_1", "meta.json"), f'"{_HOME}"')
        self.assertEqual(chk.check_absolute_paths(self.tmp), [])

    def test_claude_directory_is_specifically_excluded(self):
        # Documented rationale: .claude/ is this tool's own local
        # configuration (skills, agent definitions), not shipped
        # application source -- a skill legitimately documenting a real,
        # box-specific path is its actual content, not a mistake.
        _write(
            os.path.join(self.tmp, ".claude", "skills", "example", "SKILL.md"),
            f"Secret lives at `{_HOME}/.secrets/thing.ini`.\n",
        )
        self.assertEqual(chk.check_absolute_paths(self.tmp), [])

    def test_a_binary_file_with_invalid_utf8_is_skipped_without_crashing(self):
        p = os.path.join(self.tmp, "data.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"\xff\xfe\x00\x01 not valid utf-8")
        self.assertEqual(chk.check_absolute_paths(self.tmp), [])

    def test_relative_path_in_the_violation_is_relative_to_repo_dir_not_absolute(self):
        _write(os.path.join(self.tmp, "sub", "script.py"), f"'{_HOME}'\n")
        violations = chk.check_absolute_paths(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertTrue(violations[0].startswith(os.path.join("sub", "script.py")))
        self.assertNotIn(self.tmp, violations[0])


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_clean_repo_passes(self):
        _write(os.path.join(self.tmp, "script.py"), "path = '/tmp/thing'\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_a_violation_fails_with_the_expected_message(self):
        _write(os.path.join(self.tmp, "script.py"), f"path = '{_HOME}'\n")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("Absolute Paths Violations", out)


if __name__ == "__main__":
    unittest.main()
