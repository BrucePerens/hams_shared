#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_shebang.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_shebang as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_shebang.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CheckShebangTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_shebang_on_line_one_is_never_a_violation(self):
        _write(os.path.join(self.tmp, "script.py"), "#!/usr/bin/env python3\nprint('hi')\n")
        self.assertEqual(chk.check_shebang(self.tmp), [])

    def test_a_bang_prefixed_line_later_in_a_py_file_is_flagged(self):
        _write(
            os.path.join(self.tmp, "script.py"),
            "#!/usr/bin/env python3\nprint('hi')\n#!oops\n",
        )
        violations = chk.check_shebang(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("script.py:3", violations[0])

    def test_a_sh_file_is_also_checked(self):
        _write(os.path.join(self.tmp, "script.sh"), "echo one\n#!not-a-real-shebang\n")
        violations = chk.check_shebang(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("script.sh:2", violations[0])

    def test_a_file_with_no_bang_lines_at_all_is_clean(self):
        _write(os.path.join(self.tmp, "script.py"), "print('hi')\n")
        self.assertEqual(chk.check_shebang(self.tmp), [])

    def test_a_disallowed_extension_is_never_scanned(self):
        _write(os.path.join(self.tmp, "notes.txt"), "line one\n#!looks like a shebang\n")
        self.assertEqual(chk.check_shebang(self.tmp), [])

    def test_a_file_with_no_extension_at_all_is_still_scanned(self):
        # Real, verified, non-obvious behavior: the skip condition is
        # `ext not in valid_exts and "." in file` -- a file with NO dot in
        # its name at all (e.g. a script named "run_script", common for
        # shell scripts without a .sh suffix) is never excluded by the
        # extension check, unlike a file with an explicitly disallowed
        # extension.
        _write(os.path.join(self.tmp, "run_script"), "echo hi\n#!fake\n")
        violations = chk.check_shebang(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("run_script:2", violations[0])

    def test_an_ignored_directory_is_never_walked(self):
        _write(os.path.join(self.tmp, "node_modules", "pkg", "cli.py"), "x\n#!bad\n")
        self.assertEqual(chk.check_shebang(self.tmp), [])

    def test_a_binary_file_with_invalid_utf8_is_skipped_without_crashing(self):
        p = os.path.join(self.tmp, "data.py")
        with open(p, "wb") as f:
            f.write(b"\xff\xfe\x00\x01 not valid utf-8")
        self.assertEqual(chk.check_shebang(self.tmp), [])

    def test_multiple_violations_across_files_are_all_reported(self):
        _write(os.path.join(self.tmp, "a.py"), "x\n#!bad\n")
        _write(os.path.join(self.tmp, "b.py"), "x\n#!also_bad\n")
        self.assertEqual(len(chk.check_shebang(self.tmp)), 2)


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
        _write(os.path.join(self.tmp, "script.py"), "#!/usr/bin/env python3\nprint(1)\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_a_violation_fails(self):
        _write(os.path.join(self.tmp, "script.py"), "x\n#!bad\n")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("Shebang Violations", out)


if __name__ == "__main__":
    unittest.main()
