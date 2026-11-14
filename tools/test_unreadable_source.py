#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A file a checker cannot read (a broken symlink, bytes that are not UTF-8) is reported by name and fails the check; the run goes
on to the other files and never ends in a traceback.
[@ANCHOR: source_text:read_source]"""

import os
import subprocess
import sys
import tempfile
import unittest

from source_text import read_source

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKERS = ("check_async_lock_scope", "check_boundary_hazards", "check_burn_list", "check_cache_invalidation", "check_fake_network_listener",
            "check_float_isfinite", "check_int_float_overflow", "check_manifest_dependencies", "check_model_extension_collisions",
            "check_registry_test_cr_usage", "check_secret_storage", "check_self_writeable_field_tests")


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


class ReadSource(unittest.TestCase):
    def test_a_good_file_a_broken_symlink_and_a_non_utf8_file(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "ok.py", b"x = 1\n")
            _write(root, "bad.py", b"x = '\xff'\n")
            os.symlink("nowhere", os.path.join(root, "broken.py"))
            self.assertEqual(read_source(os.path.join(root, "ok.py")), ("x = 1\n", None))
            text, problem = read_source(os.path.join(root, "bad.py"))
            self.assertIsNone(text)
            self.assertIn("bad.py", problem)
            text, problem = read_source(os.path.join(root, "broken.py"))
            self.assertIsNone(text)
            self.assertIn("broken symlink", problem)


class CheckersSurviveBadFiles(unittest.TestCase):
    def test_every_checker_names_the_bad_files_and_fails_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as root:
            for rel, data in (("mod/__manifest__.py", b"{}"), ("mod/models/ok.py", b"x = 1\n"), ("mod/models/bad.py", b"x = '\xff'\n"),
                              ("mod/static/src/js/bad.js", b"var x = '\xff';\n"), ("mod/src/bad.rs", b"fn a() {}\n\xff\n")):
                _write(root, rel, data)
            for rel in ("mod/models/broken.py", "mod/static/src/js/broken.js", "mod/src/broken.rs"):
                os.symlink("nowhere", os.path.join(root, rel))
            subprocess.run(["git", "-C", root, "init", "-q"], check=True)
            subprocess.run(["git", "-C", root, "add", "-A"], check=True)
            subprocess.run(["git", "-C", root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], check=True)
            for name in CHECKERS:
                with self.subTest(checker=name):
                    done = subprocess.run([sys.executable, os.path.join(HERE, name + ".py"), root], capture_output=True, text=True, timeout=120)
                    output = done.stdout + done.stderr
                    self.assertNotIn("Traceback", output)
                    self.assertNotEqual(done.returncode, 0, output)
                    self.assertIn("cannot read", output)
                    self.assertIn("broken", output)


if __name__ == "__main__":
    unittest.main()
