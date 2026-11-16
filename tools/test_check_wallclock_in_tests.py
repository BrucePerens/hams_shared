#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_wallclock_in_tests.py (fixtures are source strings and temp directories; no real timing)."""

import contextlib
import io
import os
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock

import check_wallclock_in_tests as chk  # noqa: E402


def _py(src):
    return chk.check_python(textwrap.dedent(src))


class PythonTests(unittest.TestCase):
    def test_upper_bound_on_monotonic_difference_is_flagged(self):
        self.assertEqual(_py("""
            import time
            def test_x(self):
                started = time.monotonic()
                run()
                self.assertLess(time.monotonic() - started, 2)
            """), [6])

    def test_difference_through_variables_is_flagged(self):
        self.assertEqual(_py("""
            import time
            def test_x():
                t0 = time.perf_counter()
                run()
                dt = time.perf_counter() - t0
                ms = dt * 1000
                assert ms < 50
            """), [8])

    def test_datetime_difference_is_flagged(self):
        self.assertEqual(len(_py("""
            from datetime import datetime
            def test_x(self):
                a = datetime.now()
                run()
                self.assertLess((datetime.now() - a).total_seconds(), 1)
            """)), 1)

    def test_equality_and_approx_of_elapsed_is_flagged(self):
        self.assertEqual(len(_py("""
            import time
            def test_x():
                s = time.time()
                run()
                assert time.time() - s == pytest.approx(0.2, abs=0.1)
            """)), 1)

    def test_process_time_is_allowed(self):
        self.assertEqual(_py("""
            import time
            def test_x(self):
                s = time.process_time()
                run()
                self.assertLess(time.process_time() - s, 2)
            """), [])

    def test_lower_bound_after_a_real_sleep_is_allowed(self):
        self.assertEqual(_py("""
            import time
            def test_x(self):
                s = time.monotonic()
                time.sleep(0.5)
                self.assertGreaterEqual(time.monotonic() - s, 0.4)
                assert time.monotonic() - s >= 0.4
            """), [])

    def test_shifted_timestamp_is_test_data_not_elapsed(self):
        self.assertEqual(_py("""
            from datetime import datetime, timedelta
            def test_x(self):
                since = datetime.now() - timedelta(days=1)
                self.assertEqual(rec.date, since)
            """), [])

    def test_attribute_of_a_variable_holding_a_record_is_not_a_reading(self):
        self.assertEqual(_py("""
            import time
            def test_x(self):
                wide = self.make(at=time.time())
                self.assertTrue(wide.stop - wide.start <= 7200)
            """), [])

    def test_virtual_clock_is_allowed(self):
        self.assertEqual(_py("""
            def test_x():
                start = clock.now()
                run()
                assert clock.now() - start == 1.5
            """), [])

    def test_elapsed_in_another_function_does_not_leak(self):
        self.assertEqual(_py("""
            import time
            def helper():
                elapsed = time.time() - time.time()
            def test_x():
                elapsed = 3
                assert elapsed < 5
            """), [])


    def test_deadline_inside_a_polling_loop_is_a_timeout_guard(self):
        self.assertEqual(_py("""
            import time
            def test_x():
                start = time.monotonic()
                while not done():
                    assert time.monotonic() - start < 5, "timed out"
            """), [])


class JsTests(unittest.TestCase):
    def test_date_now_delta_is_flagged(self):
        self.assertEqual(chk.check_js("const t0 = Date.now();\nawait run();\nexpect(Date.now() - t0).toBeLessThan(2000);\n"), [3])

    def test_delta_through_variable_is_flagged(self):
        self.assertEqual(len(chk.check_js(
            "const t0 = performance.now();\nconst dt = performance.now() - t0;\nexpect(dt < 50).toBe(true);\n")), 1)

    def test_lower_bound_and_plain_timestamps_are_allowed(self):
        self.assertEqual(chk.check_js("const t0 = Date.now();\nexpect(Date.now() - t0 >= 29).toBe(true);\n"), [])
        self.assertEqual(chk.check_js("expect(entry.at).toBe(NOW - 86400e3);\n"), [])
        self.assertEqual(chk.check_js("expect(device.expiresAt > Date.now() - 1).toBe(true);\n"), [])


class RustTests(unittest.TestCase):
    def test_elapsed_upper_bound_is_flagged(self):
        self.assertEqual(chk.check_rust(
            "#[test]\nfn t() {\n    let started = Instant::now();\n    run();\n"
            "    assert!(started.elapsed() < Duration::from_secs(5));\n}\n"), [5])

    def test_multiline_assert_and_variable_are_flagged(self):
        self.assertEqual(chk.check_rust(
            "#[test]\nfn t() {\n    let t = Instant::now();\n    run();\n    let took = t.elapsed();\n"
            "    assert!(\n        took < Duration::from_millis(50),\n        \"slow\"\n    );\n}\n"), [6])

    def test_lower_bound_is_allowed(self):
        self.assertEqual(chk.check_rust("#[test]\nfn t() { assert!(t.elapsed() >= Duration::from_millis(90)); }\n"), [])

    def test_paused_tokio_clock_is_virtual_time(self):
        self.assertEqual(chk.check_rust(
            "#[tokio::test(start_paused = true)]\nasync fn t() { assert!(t.elapsed() < Duration::from_secs(5)); }\n"), [])

    def test_deadline_inside_a_polling_loop_is_a_timeout_guard(self):
        self.assertEqual(chk.check_rust(
            "#[test]\nfn t() {\n    let start = Instant::now();\n    while !done() {\n        if x {\n"
            "            assert!(start.elapsed() < Duration::from_secs(5), \"timed out\");\n        }\n    }\n}\n"), [])

    def test_non_test_helper_name_does_not_match(self):
        self.assertEqual(chk.check_rust("#[test]\nfn t() { assert!(x.len() < 5); }\n"), [])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.root, "tests"))
        with open(os.path.join(self.root, "tests", "test_a.py"), "w", encoding="utf-8") as f:
            f.write("import time\ndef test_a():\n    s = time.monotonic()\n    assert time.monotonic() - s < 1\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, baseline, allowlist=()):
        out = io.StringIO()
        with mock.patch.object(chk, "read_baseline", return_value=baseline), \
                mock.patch.object(chk, "read_allowlist", return_value=set(allowlist)), \
                mock.patch("sys.argv", ["x", self.root]), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
        return ctx.exception.code, out.getvalue()

    def test_new_finding_fails(self):
        code, out = self._run({})
        self.assertEqual(code, 1)
        self.assertIn("repo/tests/test_a.py:4", out)

    def test_baselined_finding_only_warns(self):
        code, out = self._run({"repo/tests/test_a.py": 1})
        self.assertEqual(code, 0)
        self.assertIn("⚠️", out)

    def test_more_findings_than_baseline_fails(self):
        with open(os.path.join(self.root, "tests", "test_a.py"), "a", encoding="utf-8") as f:
            f.write("def test_b():\n    s = time.monotonic()\n    assert time.monotonic() - s < 1\n")
        code, _ = self._run({"repo/tests/test_a.py": 1})
        self.assertEqual(code, 1)

    def test_allowlisted_file_passes(self):
        code, _ = self._run({}, allowlist={"repo/tests/test_a.py"})
        self.assertEqual(code, 0)

    def test_real_allowlist_file_parses(self):
        self.assertIsInstance(chk.read_allowlist(), set)
        self.assertIsInstance(chk.read_baseline(), dict)


if __name__ == "__main__":
    unittest.main()
