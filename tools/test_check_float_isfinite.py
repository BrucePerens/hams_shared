#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_float_isfinite.py, exercised against real temp-directory .py fixtures.
"""

import os
import shutil
import sys
import tempfile
import textwrap
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_float_isfinite as chk  # noqa: E402


def _write_py(tmp, content):
    # dedent() is essential, not cosmetic -- see check_int_float_overflow.py's own sibling test
    # file for the exact bug this avoids (an un-dedented fixture is a real IndentationError,
    # silently swallowed by check_file()'s own `except SyntaxError: return []`).
    path = os.path.join(tmp, "main.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(content))
    return path


class CheckFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_float_with_only_except_valueerror_and_no_isfinite_is_flagged(self):
        # The real bug pattern this gate was born from (aprs_is_sync.parse_aprs()'s own shape,
        # before this specific instance was confirmed unreachable and given an escape hatch).
        path = _write_py(
            self.tmp,
            """
            def parse(val):
                try:
                    return float(val)
                except ValueError:
                    return 0.0
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)
        self.assertIn("math.isfinite", findings[0][1])

    def test_float_with_math_isfinite_check_in_the_same_function_is_silent(self):
        path = _write_py(
            self.tmp,
            """
            import math

            def parse(val):
                try:
                    result = float(val)
                except ValueError:
                    return 0.0
                if not math.isfinite(result):
                    return 0.0
                return result
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_float_with_isfinite_called_bare_via_from_import_is_silent(self):
        path = _write_py(
            self.tmp,
            """
            from math import isfinite

            def parse(val):
                try:
                    result = float(val)
                except ValueError:
                    return 0.0
                if not isfinite(result):
                    return 0.0
                return result
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_isfinite_check_in_a_different_function_does_not_silence_this_one(self):
        # The heuristic is function-scoped, not file-scoped -- a finiteness check
        # elsewhere in the file must not silence an unrelated, unguarded float() call.
        path = _write_py(
            self.tmp,
            """
            import math

            def parse_a(val):
                try:
                    return float(val)
                except ValueError:
                    return 0.0

            def parse_b(val):
                try:
                    result = float(val)
                except ValueError:
                    return 0.0
                if not math.isfinite(result):
                    return 0.0
                return result
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], 6)  # the float() call inside parse_a

    def test_a_bare_float_with_no_try_except_at_all_is_not_flagged(self):
        # Deliberately out of scope -- see this gate's own module docstring for why (a bare
        # float() with no try/except at all is a different, usually more severe shape that
        # would flood this narrowly-targeted gate with unrelated internal-computation sites).
        path = _write_py(
            self.tmp,
            """
            def compute(x):
                return float(x) * 2.0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_a_try_except_that_does_not_catch_valueerror_is_not_flagged(self):
        path = _write_py(
            self.tmp,
            """
            def parse(val):
                try:
                    return float(val)
                except KeyError:
                    return 0.0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_except_exception_counts_as_catching_valueerror(self):
        path = _write_py(
            self.tmp,
            """
            def parse(val):
                try:
                    return float(val)
                except Exception:
                    return 0.0
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)

    def test_bare_except_counts_as_catching_valueerror(self):
        path = _write_py(
            self.tmp,
            """
            def parse(val):
                try:
                    return float(val)
                except:
                    return 0.0
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)

    def test_same_line_ignore_comment_suppresses_the_finding(self):
        path = _write_py(
            self.tmp,
            """
            def parse(val):
                try:
                    return float(val)  # float-isfinite-ignore: digit-only regex, cannot spell nan/inf
                except ValueError:
                    return 0.0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_ignore_comment_on_a_different_line_does_not_suppress_the_finding(self):
        path = _write_py(
            self.tmp,
            """
            def parse(val):
                # float-isfinite-ignore: this comment is on the WRONG line
                try:
                    return float(val)
                except ValueError:
                    return 0.0
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)

    def test_a_float_call_outside_any_try_is_never_flagged_even_with_a_sibling_try(self):
        path = _write_py(
            self.tmp,
            """
            def parse(val):
                x = float(val)
                try:
                    return int(x)
                except ValueError:
                    return 0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_a_syntax_error_file_returns_no_findings_not_a_crash(self):
        path = _write_py(self.tmp, "def broken(:\n")
        self.assertEqual(chk.check_file(path), [])


class FindPythonFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ignores_pycache_and_node_modules(self):
        for d in ("__pycache__", "node_modules", "real_dir"):
            os.makedirs(os.path.join(self.tmp, d))
            with open(os.path.join(self.tmp, d, "x.py"), "w") as f:
                f.write("x = 1\n")
        found = chk.find_python_files(self.tmp)
        rels = [os.path.relpath(p, self.tmp) for p in found]
        self.assertEqual(rels, [os.path.join("real_dir", "x.py")])

    def test_finds_nested_py_files_sorted(self):
        os.makedirs(os.path.join(self.tmp, "b"))
        os.makedirs(os.path.join(self.tmp, "a"))
        with open(os.path.join(self.tmp, "b", "z.py"), "w") as f:
            f.write("x = 1\n")
        with open(os.path.join(self.tmp, "a", "y.py"), "w") as f:
            f.write("x = 1\n")
        found = chk.find_python_files(self.tmp)
        self.assertEqual(len(found), 2)
        self.assertEqual(found, sorted(found))


class FloatIsfinitePropertyTests(unittest.TestCase):
    """Hypothesis properties generalizing the fixed-example tests above."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @given(st.integers(min_value=1, max_value=6))
    @settings(max_examples=20)
    def test_arbitrary_blank_line_count_between_try_and_except_still_flags(self, n_blank):
        # The AST walk shouldn't care about incidental whitespace between the try body and
        # its except clause.
        blank = "\n" * n_blank
        path = _write_py(
            self.tmp,
            f"""
            def parse(val):
                try:
                    return float(val)
{blank}                except ValueError:
                    return 0.0
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)

    @given(st.integers(min_value=1, max_value=8))
    @settings(max_examples=20)
    def test_arbitrary_count_of_unguarded_float_functions_flags_each_once(self, n_funcs):
        body = "\n".join(
            textwrap.dedent(f"""
            def parse_{i}(val):
                try:
                    return float(val)
                except ValueError:
                    return 0.0
            """)
            for i in range(n_funcs)
        )
        path = os.path.join(self.tmp, "main.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        findings = chk.check_file(path)
        self.assertEqual(len(findings), n_funcs)


if __name__ == "__main__":
    unittest.main()
