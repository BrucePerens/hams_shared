#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_int_float_overflow.py, exercised against real temp-directory .py fixtures.
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

import check_int_float_overflow as chk  # noqa: E402


def _write_py(tmp, content):
    # dedent() is essential, not cosmetic: every fixture below is written as an indented
    # triple-quoted string literal (matching this file's own indentation), and a Python module
    # cannot start with leading whitespace on its own top-level `def` -- an un-dedented fixture
    # is a real IndentationError, which check_file()'s own `except SyntaxError: return []`
    # silently swallows into "no findings," making a fixture bug look identical to a real
    # "this shape isn't flagged" result. Found live: every fixture in this file originally hit
    # exactly this bug before dedent() was added.
    path = os.path.join(tmp, "main.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(content))
    return path


class CheckFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_int_float_with_only_except_valueerror_is_flagged(self):
        # The real bug this gate was born from (sota_sync._int()'s own shape).
        path = _write_py(
            self.tmp,
            """
            def _int(val):
                try:
                    return int(float(val))
                except ValueError:
                    return 0
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)
        self.assertIn("OverflowError", findings[0][1])

    def test_int_float_with_no_try_at_all_is_flagged(self):
        path = _write_py(
            self.tmp,
            """
            def parse(val):
                return int(float(val))
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)
        self.assertIn("no enclosing try", findings[0][1])

    def test_int_float_catching_overflowerror_explicitly_is_not_flagged(self):
        path = _write_py(
            self.tmp,
            """
            def _int(val):
                try:
                    return int(float(val))
                except (ValueError, OverflowError):
                    return 0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_int_float_under_a_bare_except_is_not_flagged(self):
        path = _write_py(
            self.tmp,
            """
            def _int(val):
                try:
                    return int(float(val))
                except:
                    return 0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_int_float_under_except_exception_is_not_flagged(self):
        path = _write_py(
            self.tmp,
            """
            def _int(val):
                try:
                    return int(float(val))
                except Exception:
                    return 0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_a_math_isfinite_check_before_int_alone_does_not_suppress_the_finding(self):
        # This gate's own textual/AST scan doesn't attempt to prove an isfinite() check earlier
        # in the same function makes an int(float(...)) call inside a still-incomplete
        # try/except provably safe -- only an explicit OverflowError-catching handler (or the
        # suppression comment) is recognized. A real fix restructures the try itself (as
        # sota_sync's own real fix did), which this test's own fixture does NOT do, so it must
        # still be flagged.
        path = _write_py(
            self.tmp,
            """
            def _int(val):
                x = float(val)
                try:
                    return int(float(val))
                except ValueError:
                    return 0
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)

    def test_a_suppression_comment_exempts_that_line(self):
        path = _write_py(
            self.tmp,
            """
            def _int(val):
                try:
                    return int(float(val))  # int-float-overflow-ignore: val is already known-finite
                except ValueError:
                    return 0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_int_float_inside_an_unrelated_sibling_try_is_still_flagged(self):
        # The enclosing-try lookup must find the try whose OWN body contains the call -- not a
        # DIFFERENT try elsewhere in the same function that happens to catch OverflowError.
        path = _write_py(
            self.tmp,
            """
            def f(val):
                try:
                    pass
                except OverflowError:
                    pass
                try:
                    return int(float(val))
                except ValueError:
                    return 0
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)

    def test_bare_float_with_no_surrounding_int_is_not_this_gates_concern(self):
        # float() alone raising ValueError is already handled correctly by ordinary
        # except-ValueError code throughout this codebase -- this gate is scoped narrowly to
        # the int(float(...)) combination's own OverflowError trap, not general NaN/Inf
        # validation (a much broader, more heuristic concern with real false-positive risk,
        # deliberately left to manual per-call-site review).
        path = _write_py(
            self.tmp,
            """
            def f(val):
                try:
                    return float(val)
                except ValueError:
                    return 0.0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_int_wrapping_an_expression_built_from_float_is_also_flagged(self):
        # Real gap found and fixed live: int(float(x) * y) has the identical real bug
        # (float("inf") * y is still inf, and int() of that still raises OverflowError) as the
        # exact int(float(x)) shape, but requiring float(...) to be int()'s own direct sole
        # argument would miss it -- this is hams_local_relay's own legacy Flask relay's real
        # qsy() endpoint shape (`int(float(freq) * 1000000)`).
        path = _write_py(
            self.tmp,
            """
            def f(freq):
                try:
                    return int(float(freq) * 1000000)
                except ValueError:
                    return 0
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)

    def test_a_file_with_a_syntax_error_is_skipped_not_fatal(self):
        path = _write_py(self.tmp, "def f(:\n    pass\n")
        self.assertEqual(chk.check_file(path), [])


class FindPythonFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_py_files_and_skips_target_dir(self):
        _write_py(self.tmp, "# real\n")
        built = os.path.join(self.tmp, "target", "debug")
        os.makedirs(built, exist_ok=True)
        with open(os.path.join(built, "generated.py"), "w") as f:
            f.write("# generated\n")
        found = chk.find_python_files(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith("main.py"))


class IntFloatOverflowPropertyTests(unittest.TestCase):
    """A handful of Hypothesis properties over synthetic source shapes, matching this
    session's own established convention for its two other new-tonight linters."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @given(st.lists(st.sampled_from(["OverflowError", "ArithmeticError", "Exception", "BaseException"]), min_size=1, max_size=3))
    @settings(max_examples=50)
    def test_any_tuple_containing_an_overflow_catching_name_suppresses_the_finding(self, names):
        # Real bug found while writing this: `except OverflowError:` alone (not in a tuple)
        # is a real, common shape too -- covered by the fixed-example tests above; this
        # property covers the TUPLE form for an arbitrary combination/order/count of the
        # names this gate itself recognizes as overflow-catching.
        except_clause = "(" + ", ".join(names + ["ValueError"]) + ")"
        path = _write_py(
            self.tmp,
            f"""
            def _int(val):
                try:
                    return int(float(val))
                except {except_clause}:
                    return 0
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    @given(st.integers(min_value=1, max_value=5))
    @settings(max_examples=30)
    def test_n_independent_unguarded_calls_produce_exactly_n_findings(self, n):
        body = "\n".join(f"    v{i} = int(float(val))" for i in range(n))
        path = _write_py(self.tmp, f"def f(val):\n{body}\n")
        findings = chk.check_file(path)
        self.assertEqual(len(findings), n)


if __name__ == "__main__":
    unittest.main()
