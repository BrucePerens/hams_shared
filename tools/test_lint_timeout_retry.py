#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Step timeouts on a saturated machine are a distinct outcome from step failures (ADR 0117): the step is rerun once, alone, after the
run; a pass is recorded in the stamp as `timeout-retried=`; a second failure is real. The step's own time limit stretches with load.

Tests [@ANCHOR: lint_exec:cpu_pressure]
Tests [@ANCHOR: lint_exec:saturation]
Tests [@ANCHOR: lint_exec:timeout_scale]
Tests [@ANCHOR: lint_exec:timeout_suspect]
Tests [@ANCHOR: run_linters:retry_timed_out_steps]
Tests [@ANCHOR: run_linters:timeout_retried_field]
Tests [@ANCHOR: lint_stamp_gate:known_red_note]
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lint_exec  # noqa: E402
import lint_stamp_gate  # noqa: E402
import run_linters  # noqa: E402

PSI = "some avg10=%.2f avg60=1.00 avg300=1.00 total=123\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"


def result(name="check_pip_audit", code=1, output="pip-audit did not complete within 300s", saturated="load 90", cached=False):
    return lint_exec.Result(name, code, output, 300.0, cached=cached, saturated=saturated)


class SaturationTests(unittest.TestCase):
    def test_cpu_pressure_reads_some_avg10_and_tolerates_a_missing_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".psi") as handle:
            handle.write(PSI % 41.5)
            handle.flush()
            self.assertEqual(lint_exec.cpu_pressure(handle.name), 41.5)
        self.assertIsNone(lint_exec.cpu_pressure("/nonexistent/psi"))
        with tempfile.NamedTemporaryFile("w") as handle:
            handle.write("garbage")
            handle.flush()
            self.assertIsNone(lint_exec.cpu_pressure(handle.name))

    def test_saturation_by_pressure_or_load_and_quiet(self):
        quiet = lint_exec.saturation({}, loadavg=lambda: (2.0, 2, 2), cpus=8, pressure=lambda: 3.0)
        self.assertEqual(quiet, (False, ""))
        by_psi = lint_exec.saturation({}, loadavg=lambda: (2.0, 2, 2), cpus=8, pressure=lambda: 40.0)
        self.assertTrue(by_psi[0])
        self.assertIn("CPU pressure 40%", by_psi[1])
        by_load = lint_exec.saturation({}, loadavg=lambda: (85.0, 80, 70), cpus=32, pressure=lambda: None)
        self.assertTrue(by_load[0])
        self.assertIn("load 85.0 on 32", by_load[1])
        lenient = lint_exec.saturation({"HAMS_LINT_TIMEOUT_LOAD_RATIO": "4"}, loadavg=lambda: (85.0, 80, 70), cpus=32, pressure=lambda: None)
        self.assertFalse(lenient[0])
        self.assertEqual(lint_exec.saturation({"HAMS_LINT_TIMEOUT_PSI_PERCENT": "x"}, loadavg=lambda: (0, 0, 0), cpus=2,
                                              pressure=lambda: 0.0), (False, ""))

    def test_timeout_scale_is_one_when_quiet_and_follows_the_load_up_to_four(self):
        quiet = dict(loadavg=lambda: (1.0, 1, 1), cpus=8, pressure=lambda: 0.0)
        self.assertEqual(lint_exec.timeout_scale({}, **quiet), 1.0)
        self.assertEqual(lint_exec.timeout_scale({}, loadavg=lambda: (20.0, 1, 1), cpus=8, pressure=lambda: 0.0), 2.5)
        self.assertEqual(lint_exec.timeout_scale({}, loadavg=lambda: (90.0, 1, 1), cpus=8, pressure=lambda: 0.0), 4.0)
        self.assertEqual(lint_exec.timeout_scale({"HAMS_LINT_TIMEOUT_SCALE_FIXED": "1"}, loadavg=lambda: (90.0, 1, 1), cpus=8,
                                                 pressure=lambda: 90.0), 1.0)

    def test_a_step_gets_the_scale_in_its_environment_and_a_failed_step_records_saturation(self):
        step = lint_exec.Step(name="probe", argv=[sys.executable, "-c",
                                                  "import os,sys; print('scale=' + os.environ['HAMS_LINT_TIMEOUT_SCALE']); "
                                                  "print('timed out'); sys.exit(1)"],
                              roots=(), suffixes=None)
        with mock.patch.object(lint_exec, "timeout_scale", return_value=2.5), \
                mock.patch.object(lint_exec, "saturation", return_value=(True, "load 90")):
            got = lint_exec.run_step(step, {})
        self.assertIn("scale=2.5", got.output)
        self.assertEqual(got.saturated, "load 90")
        self.assertTrue(lint_exec.timeout_suspect(got))
        passing = lint_exec.Step(name="ok", argv=[sys.executable, "-c", "pass"], roots=(), suffixes=None)
        with mock.patch.object(lint_exec, "saturation", return_value=(True, "load 90")):
            self.assertEqual(lint_exec.run_step(passing, {}).saturated, "")

    def test_suspect_needs_a_failure_a_real_run_saturation_and_timeout_text(self):
        self.assertTrue(lint_exec.timeout_suspect(result()))
        self.assertTrue(lint_exec.timeout_suspect(result(output="Read timed out. (read timeout=10)")))
        self.assertFalse(lint_exec.timeout_suspect(result(code=0)))
        self.assertFalse(lint_exec.timeout_suspect(result(cached=True)))
        self.assertFalse(lint_exec.timeout_suspect(result(saturated="")))
        self.assertFalse(lint_exec.timeout_suspect(result(output="E501 line too long")))


class PipAuditScaleTests(unittest.TestCase):
    def test_the_audit_limit_scale_is_clamped_and_bad_values_mean_one(self):
        import check_pip_audit
        scale = check_pip_audit._timeout_scale
        self.assertEqual((scale(None), scale("2.5"), scale("9"), scale("0.2")), (1.0, 2.5, 4.0, 1.0))
        self.assertEqual((scale("nan"), scale("inf"), scale("abc")), (1.0, 1.0, 1.0))
        self.assertEqual(lint_exec._finite("inf", 7.0), 7.0)
        self.assertEqual(lint_exec._finite("3", 7.0), 3.0)


class RetryTests(unittest.TestCase):
    def steps(self):
        return [lint_exec.Step(name=n, argv=["true"], roots=(), suffixes=None) for n in ("check_pip_audit", "flake8", "pytest daemons/relay_ca")]

    def test_a_timed_out_step_that_passes_alone_is_replaced_and_named(self):
        results = [result(), result("flake8", 0, "", ""), result("pytest daemons/relay_ca", 1, "E fsync timed out")]
        ran = []

        def execute(wanted):
            ran.append([s.name for s in wanted])
            return [lint_exec.Result(wanted[0].name, 0, "ok", 1.0)]

        lines = []
        with mock.patch.dict(os.environ, {"HAMS_LINT_TIMEOUT_RETRY": "on"}):
            new, retried = run_linters.retry_timed_out_steps(results, self.steps(), execute, lines.append)
        self.assertEqual(ran, [["check_pip_audit"], ["pytest daemons/relay_ca"]], "one at a time, in order")
        self.assertEqual(retried, ["check_pip_audit", "pytest daemons/relay_ca"])
        self.assertTrue(all(r.returncode == 0 for r in new))
        self.assertEqual(results[0].returncode, 1, "the input list is not modified")
        self.assertTrue(any("recorded as timeout-retried" in line for line in lines))

    def test_a_step_that_fails_again_stays_failed_and_is_not_recorded(self):
        lines = []
        new, retried = run_linters.retry_timed_out_steps(
            [result()], self.steps(), lambda wanted: [lint_exec.Result(wanted[0].name, 1, "timed out again", 1.0)], lines.append)
        self.assertEqual(retried, [])
        self.assertEqual(new[0].returncode, 1)
        self.assertIn("did not complete", new[0].output)
        self.assertTrue(any("failed again" in line for line in lines))

    def test_nothing_is_rerun_for_plain_failures_unknown_steps_or_when_switched_off(self):
        def boom(wanted):
            raise AssertionError("must not run")

        plain = [result("flake8", 1, "E501", "load 90")]
        self.assertEqual(run_linters.retry_timed_out_steps(plain, self.steps(), boom), (plain, []))
        stranger = [result("unknown_step")]
        self.assertEqual(run_linters.retry_timed_out_steps(stranger, self.steps(), boom), (stranger, []))
        with mock.patch.dict(os.environ, {"HAMS_LINT_TIMEOUT_RETRY": "off"}):
            self.assertEqual(run_linters.retry_timed_out_steps([result()], self.steps(), boom)[1], [])

    def test_stamp_field_and_gate_note_name_the_retried_steps(self):
        self.assertEqual(run_linters.timeout_retried_field([]), "")
        field = run_linters.timeout_retried_field(["check_pip_audit", "pytest daemons/relay_ca"])
        self.assertEqual(field, " timeout-retried=check_pip_audit,pytest_daemons/relay_ca")
        text = run_linters.stamp_text("changed", ["check_pip_audit"], known_red=field + " known_red=x baseline=abc")
        note = lint_stamp_gate.known_red_note(text)
        self.assertIn("serial rerun", note)
        self.assertIn("known red on main", note)
        self.assertIsNone(lint_stamp_gate.refusal(text, ["a.py"]))


if __name__ == "__main__":
    unittest.main()
