#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_rabbitmq_pool.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_rabbitmq_pool as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_rabbitmq_pool.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CheckRabbitmqTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_blocking_connection_is_flagged(self):
        _write(os.path.join(self.tmp, "foo.py"), "conn = pika.BlockingConnection(params)\n")
        violations = chk.check_rabbitmq(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("foo.py:1", violations[0])

    def test_a_select_connection_is_flagged(self):
        _write(os.path.join(self.tmp, "foo.py"), "conn = pika.SelectConnection(params)\n")
        self.assertEqual(len(chk.check_rabbitmq(self.tmp)), 1)

    def test_the_bypass_comment_exempts_that_line(self):
        _write(
            os.path.join(self.tmp, "foo.py"),
            "conn = pika.BlockingConnection(params)  # burn-ignore-pika\n",
        )
        self.assertEqual(chk.check_rabbitmq(self.tmp), [])

    def test_the_pool_implementation_file_itself_is_exempted(self):
        _write(
            os.path.join(self.tmp, "hams_rabbitmq", "rabbitmq_pool.py"),
            "conn = pika.BlockingConnection(params)\n",
        )
        self.assertEqual(chk.check_rabbitmq(self.tmp), [])

    def test_a_non_py_file_is_never_scanned(self):
        _write(os.path.join(self.tmp, "notes.md"), "pika.BlockingConnection(params)\n")
        self.assertEqual(chk.check_rabbitmq(self.tmp), [])

    def test_the_daemons_directory_is_never_walked(self):
        _write(
            os.path.join(self.tmp, "daemons", "some_daemon", "main.py"),
            "conn = pika.BlockingConnection(params)\n",
        )
        self.assertEqual(chk.check_rabbitmq(self.tmp), [])

    def test_other_pika_usage_not_matching_the_two_class_names_is_not_flagged(self):
        _write(os.path.join(self.tmp, "foo.py"), "params = pika.URLParameters(url)\n")
        self.assertEqual(chk.check_rabbitmq(self.tmp), [])

    def test_a_binary_file_with_invalid_utf8_is_skipped_without_crashing(self):
        p = os.path.join(self.tmp, "data.py")
        with open(p, "wb") as f:
            f.write(b"\xff\xfe\x00\x01 not valid utf-8")
        self.assertEqual(chk.check_rabbitmq(self.tmp), [])

    def test_a_clean_file_using_the_pool_is_not_flagged(self):
        _write(os.path.join(self.tmp, "foo.py"), "conn = env['hams_rabbitmq.pool'].get()\n")
        self.assertEqual(chk.check_rabbitmq(self.tmp), [])


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
        _write(os.path.join(self.tmp, "foo.py"), "conn = env['hams_rabbitmq.pool'].get()\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_a_violation_fails(self):
        _write(os.path.join(self.tmp, "foo.py"), "conn = pika.BlockingConnection(params)\n")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("RabbitMQ Pool Violations", out)


if __name__ == "__main__":
    unittest.main()
