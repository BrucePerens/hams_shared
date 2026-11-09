#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for known_red.py and test.py's apply_failure_policy: the known-red baseline and the one-time flake rerun
(ADR 0118). The log lines are copied from a real Odoo run.

Tests [@ANCHOR: known_red:FailureRecord]
Tests [@ANCHOR: known_red:parse_failure_line]
Tests [@ANCHOR: known_red:parse_started_line]
Tests [@ANCHOR: known_red:RunObservation]
Tests [@ANCHOR: known_red:BaselineEntry]
Tests [@ANCHOR: known_red:baseline_paths]
Tests [@ANCHOR: known_red:load_baseline]
Tests [@ANCHOR: known_red:flake_threshold]
Tests [@ANCHOR: known_red:load_flakes]
Tests [@ANCHOR: known_red:record_flakes]
Tests [@ANCHOR: known_red:Classification]
Tests [@ANCHOR: known_red:classify]
Tests [@ANCHOR: known_red:rerun_tags]
Tests [@ANCHOR: known_red:PolicyOutcome]
Tests [@ANCHOR: known_red:decide]
Tests [@ANCHOR: known_red:raw_outcome]
Tests [@ANCHOR: known_red:git_head]
Tests [@ANCHOR: known_red:write_results]
Tests [@ANCHOR: known_red:max_age_days]
Tests [@ANCHOR: known_red:age_text]
Tests [@ANCHOR: known_red:show_lines]
Tests [@ANCHOR: known_red:renew_entry]
Tests [@ANCHOR: known_red:main]
Tests [@ANCHOR: test:apply_failure_policy]
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
from types import SimpleNamespace

import known_red

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_test_py():
    spec = importlib.util.spec_from_file_location("_hams_test_runner_for_known_red", os.path.join(_HERE, "test.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREFIX = "2026-11-08 11:30:30,133 1056808 "


def fail(module, cls, method, flavour="FAIL"):
    return f"{PREFIX}ERROR hams_test odoo.addons.{module}.tests.test_x: {flavour}: {cls}.{method}"


def started(module, cls, method):
    return f"{PREFIX}INFO hams_test odoo.addons.{module}.tests.test_x: Starting {cls}.{method} ..."


def result(failed, errors, total):
    return f"{PREFIX}ERROR hams_test odoo.tests.result: {failed} failed, {errors} error(s) of {total} tests when loading database 'x'"


def observe(*lines):
    observation = known_red.RunObservation()
    for line in lines:
        observation.feed(line)
    return observation


def when(days_ago):
    return time.strftime(known_red.TIME_FORMAT, time.gmtime(time.time() - days_ago * 86400))


def entry(test_id, owner="alice", reason="needs a box fix", first="abc1234def5678", days_ago=1 / 24):
    return {"test_id": test_id, "first_seen": first, "first_seen_time": when(days_ago), "owner": owner, "reason": reason}


def write_baseline(directory, *entries, name="known_red_baseline.json"):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "entries": list(entries)}, handle)
    return path


class ParsingTests(unittest.TestCase):
    # Tests [@ANCHOR: known_red:parse_failure_line]
    # Tests [@ANCHOR: known_red:FailureRecord]
    def test_a_failure_and_an_error_line_name_module_class_and_method(self):
        self.assertEqual(
            known_red.parse_failure_line(fail("zero_sudo", "TestIntegrationFacility", "test_01_daemon_lifecycle", "ERROR")),
            known_red.FailureRecord("zero_sudo:TestIntegrationFacility.test_01_daemon_lifecycle", "ERROR", False),
        )
        self.assertEqual(known_red.parse_failure_line(fail("web_map", "TestTour", "test_01")).flavour, "FAIL")

    # Tests [@ANCHOR: known_red:parse_failure_line]
    def test_a_subtest_failure_names_its_test(self):
        line = f"{PREFIX}ERROR hams_test odoo.addons.m.tests.t: FAIL: Subtest TestA.test_b (x=1)"
        self.assertEqual(known_red.parse_failure_line(line).test_id, "m:TestA.test_b")

    # Tests [@ANCHOR: known_red:parse_failure_line]
    def test_a_class_level_error_is_a_setup_error_named_after_its_class(self):
        line = f"{PREFIX}ERROR hams_test unittest.suite: ERROR: setUpClass (odoo.addons.ham_x.tests.test_y.TestZ)"
        record = known_red.parse_failure_line(line)
        self.assertEqual((record.test_id, record.setup), ("ham_x:TestZ.setUpClass", True))

    # Tests [@ANCHOR: known_red:parse_failure_line]
    def test_other_lines_are_not_failures(self):
        for line in (
            "ERROR: could not serialize access due to concurrent update",
            f"{PREFIX}INFO hams_test odoo.addons.m.tests.t: Starting TestA.test_b ...",
            f"{PREFIX}ERROR hams_test some.other.logger: ERROR: Something.happened",
            result(1, 0, 5),
        ):
            self.assertIsNone(known_red.parse_failure_line(line), line)

    # Tests [@ANCHOR: known_red:parse_started_line]
    def test_a_starting_line_names_the_test(self):
        self.assertEqual(known_red.parse_started_line(started("m", "TestA", "test_b")), "m:TestA.test_b")
        self.assertIsNone(known_red.parse_started_line(fail("m", "TestA", "test_b")))

    # Tests [@ANCHOR: known_red:RunObservation]
    def test_an_observation_collects_failures_starts_and_the_verdict_and_works_as_a_run_cmd_consumer(self):
        observation = observe(started("m", "A", "t1"), fail("m", "A", "t1"), result(1, 0, 1))
        self.assertEqual(observation.failed_ids(), {"m:A.t1"})
        self.assertEqual(observation.started, {"m:A.t1"})
        self.assertEqual(observation.counted(), 1)
        observation.process_line("\x1b[31m" + started("m", "A", "t2") + "\x1b[0m")
        observation.append_diagnostic("ignored")
        self.assertIn("m:A.t2", observation.started)


class BaselineFileTests(unittest.TestCase):
    # Tests [@ANCHOR: known_red:load_baseline]
    # Tests [@ANCHOR: known_red:BaselineEntry]
    def test_a_good_file_loads_with_every_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_baseline(directory, entry("/m:A.t1", owner="bob"))
            loaded = known_red.load_baseline([path])
        self.assertEqual(loaded["m:A.t1"].owner, "bob")
        self.assertEqual(loaded["m:A.t1"].first_seen, "abc1234def5678")

    # Tests [@ANCHOR: known_red:load_baseline]
    def test_a_wrong_file_stops_the_run_instead_of_tolerating_or_dropping_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = {
                "missing owner": write_baseline(directory, {"test_id": "m:A.t", "first_seen": "a", "first_seen_time": when(1), "reason": "r"}, name="a.json"),
                "missing time": write_baseline(directory, {"test_id": "m:A.t", "first_seen": "a", "owner": "o", "reason": "r"}, name="f.json"),
                "bad time": write_baseline(directory, dict(entry("m:A.t"), first_seen_time="yesterday"), name="g.json"),
                "renewal without reason": write_baseline(directory, dict(entry("m:A.t"), renewed={"time": when(0), "reason": " "}), name="h.json"),
                "bad id": write_baseline(directory, entry("not a test id"), name="b.json"),
                "duplicate": write_baseline(directory, entry("m:A.t"), entry("m:A.t"), name="c.json"),
            }
            with open(os.path.join(directory, "d.json"), "w") as handle:
                handle.write('{"version": 2, "entries": []}')
            bad["version"] = os.path.join(directory, "d.json")
            with open(os.path.join(directory, "e.json"), "w") as handle:
                handle.write("{not json")
            bad["json"] = os.path.join(directory, "e.json")
            for label, path in bad.items():
                with self.assertRaises(known_red.BaselineError, msg=label):
                    known_red.load_baseline([path])

    # Tests [@ANCHOR: known_red:load_baseline]
    def test_a_time_in_the_future_stops_the_run_because_it_would_never_expire(self):
        with tempfile.TemporaryDirectory() as directory:
            late = write_baseline(directory, entry("m:A.t", days_ago=-30), name="a.json")
            renewed = write_baseline(directory, dict(entry("m:A.t"), renewed={"time": when(-30), "reason": "r"}), name="b.json")
            for path in (late, renewed):
                with self.assertRaises(known_red.BaselineError):
                    known_red.load_baseline([path])
            skewed = write_baseline(directory, entry("m:A.t", days_ago=-1 / 48), name="c.json")
            self.assertIn("m:A.t", known_red.load_baseline([skewed]))

    # Tests [@ANCHOR: known_red:baseline_paths]
    def test_paths_come_from_the_environment_or_the_repository_roots_and_only_existing_files_count(self):
        with tempfile.TemporaryDirectory() as directory:
            own = write_baseline(directory)
            self.assertEqual(known_red.baseline_paths(directory, {known_red.BASELINE_ENV: own + os.pathsep + "/nonexistent"}), [os.path.realpath(own)])
            self.assertIn(os.path.realpath(own), known_red.baseline_paths(directory, {}))
        self.assertEqual(known_red.baseline_paths("/nonexistent-dir", {known_red.BASELINE_ENV: "/nonexistent/x.json"}), [])

    # Tests [@ANCHOR: known_red:baseline_paths]
    def test_the_shipped_baseline_is_valid(self):
        paths = known_red.baseline_paths("/nonexistent-dir", {})
        self.assertTrue(any(p.endswith("tools/known_red_baseline.json") for p in paths))
        known_red.load_baseline(paths)


class FlakeFileTests(unittest.TestCase):
    # Tests [@ANCHOR: known_red:flake_threshold]
    def test_threshold_default_override_and_bad_values(self):
        self.assertEqual(known_red.flake_threshold({}), 3)
        self.assertEqual(known_red.flake_threshold({known_red.FLAKE_THRESHOLD_ENV: "5"}), 5)
        for bad in ("0", "-1", "x", "1.5"):
            with self.assertRaises(known_red.BaselineError):
                known_red.flake_threshold({known_red.FLAKE_THRESHOLD_ENV: bad})

    # Tests [@ANCHOR: known_red:record_flakes]
    # Tests [@ANCHOR: known_red:load_flakes]
    def test_counts_accumulate_and_concurrent_recorders_lose_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "sub", "flakes.json")
            self.assertEqual(known_red.load_flakes(path), {})
            threads = [threading.Thread(target=known_red.record_flakes, args=(path, ["m:A.t"], "c%d" % i)) for i in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            updated = known_red.record_flakes(path, ["m:A.t", "m:B.u"], "last", now="2026-11-08T00:00:00Z")
            loaded = known_red.load_flakes(path)
        self.assertEqual(loaded["m:A.t"]["count"], 9)
        self.assertEqual(loaded["m:B.u"]["count"], 1)
        self.assertEqual(updated["m:B.u"]["last_commit"], "last")

    # Tests [@ANCHOR: known_red:load_flakes]
    def test_a_corrupt_flake_file_is_an_error_not_an_empty_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "f.json")
            with open(path, "w") as handle:
                handle.write("[1")
            with self.assertRaises(known_red.BaselineError):
                known_red.load_flakes(path)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.flakes = os.path.join(self.directory.name, "flakes.json")
        self.reruns = []

    def baseline(self, *ids):
        path = write_baseline(self.directory.name, *[entry(i) for i in ids])
        return known_red.load_baseline([path])

    def rerun_passing(self, ids):
        self.reruns.append(list(ids))
        return observe(*[started(*i.replace(":", ".").split(".")[0:1], *i.split(":")[1].split(".")) for i in ids], result(0, 0, len(ids)))

    def rerun_failing(self, ids):
        self.reruns.append(list(ids))
        return observe(
            *[started(i.split(":")[0], *i.split(":")[1].split(".")) for i in ids],
            *[fail(i.split(":")[0], *i.split(":")[1].split(".")) for i in ids],
            result(len(ids), 0, len(ids)),
        )

    def decide(self, first, baseline, rerun, **kwargs):
        return known_red.decide(first, baseline, rerun, self.flakes, "deadbeef", 3, **kwargs)

    # Tests [@ANCHOR: known_red:classify]
    # Tests [@ANCHOR: known_red:Classification]
    def test_classification_separates_known_new_setup_unparsed_and_now_passing(self):
        first = observe(
            started("m", "A", "t1"), fail("m", "A", "t1"), fail("m", "A", "t2"),
            f"{PREFIX}ERROR hams_test unittest.suite: ERROR: setUpClass (odoo.addons.m.tests.t.B)",
            started("m", "A", "fixed"), result(3, 1, 9),
        )
        cls = known_red.classify(first, self.baseline("m:A.t1", "m:A.fixed", "m:Z.notrun"))
        self.assertEqual(list(cls.known_red), ["m:A.t1"])
        self.assertEqual(cls.new, ["m:A.t2"])
        self.assertEqual(cls.setup_new, ["m:B.setUpClass"])
        self.assertEqual(cls.unparsed, 1)
        self.assertEqual(cls.now_passing, ["m:A.fixed"])

    # Tests [@ANCHOR: known_red:decide]
    # Tests [@ANCHOR: known_red:PolicyOutcome]
    def test_a_baselined_failure_alone_is_green_listed_and_never_rerun(self):
        outcome = self.decide(observe(fail("m", "A", "t1"), result(1, 0, 3)), self.baseline("m:A.t1"), self.rerun_failing)
        self.assertTrue(outcome.run_green)
        self.assertEqual(self.reruns, [])
        text = "\n".join(outcome.summary_lines())
        self.assertIn("m:A.t1", text)
        self.assertIn("alice", text)
        self.assertIn("needs a box fix", text)

    # Tests [@ANCHOR: known_red:decide]
    def test_a_new_failure_that_passes_alone_is_a_counted_flake_and_green(self):
        outcome = self.decide(observe(fail("m", "A", "t2"), result(1, 0, 3)), {}, self.rerun_passing)
        self.assertTrue(outcome.run_green)
        self.assertEqual(self.reruns, [["m:A.t2"]])
        self.assertEqual(known_red.load_flakes(self.flakes)["m:A.t2"]["count"], 1)
        self.assertIn("m:A.t2  flake count 1", "\n".join(outcome.summary_lines()))

    # Tests [@ANCHOR: known_red:decide]
    def test_a_new_failure_that_fails_alone_too_is_red_and_not_counted_as_a_flake(self):
        outcome = self.decide(observe(fail("m", "A", "t2"), result(1, 0, 3)), {}, self.rerun_failing)
        self.assertFalse(outcome.run_green)
        self.assertEqual(outcome.failed_again, ["m:A.t2"])
        self.assertEqual(known_red.load_flakes(self.flakes), {})

    # Tests [@ANCHOR: known_red:decide]
    def test_a_rerun_that_did_not_run_the_test_or_gave_no_verdict_is_not_a_pass(self):
        outcome = self.decide(observe(fail("m", "A", "t2"), result(1, 0, 3)), {}, lambda ids: observe(result(0, 0, 0)))
        self.assertFalse(outcome.run_green)
        outcome = self.decide(observe(fail("m", "A", "t2"), result(1, 0, 3)), {}, lambda ids: observe())
        self.assertFalse(outcome.run_green)

    # Tests [@ANCHOR: known_red:decide]
    def test_a_setup_error_is_never_rerun_and_is_red_unless_baselined(self):
        line = f"{PREFIX}ERROR hams_test unittest.suite: ERROR: setUpClass (odoo.addons.m.tests.t.B)"
        outcome = self.decide(observe(line, result(0, 1, 3)), {}, self.rerun_passing)
        self.assertFalse(outcome.run_green)
        self.assertEqual(self.reruns, [])
        self.assertIn("NEW SETUP/CLASS ERROR (never rerun): m:B.setUpClass", "\n".join(outcome.summary_lines()))
        outcome = self.decide(observe(line, result(0, 1, 3)), self.baseline("m:B.setUpClass"), self.rerun_passing)
        self.assertTrue(outcome.run_green)

    # Tests [@ANCHOR: known_red:decide]
    def test_an_unnamed_failure_or_a_systemic_number_of_failures_is_red_without_a_rerun(self):
        outcome = self.decide(observe(fail("m", "A", "t2"), result(2, 0, 3)), {}, self.rerun_passing)
        self.assertFalse(outcome.run_green)
        self.assertEqual(self.reruns, [])
        many = [fail("m", "A", "t%d" % i) for i in range(5)] + [result(5, 0, 9)]
        outcome = self.decide(observe(*many), {}, self.rerun_passing, max_rerun=3)
        self.assertFalse(outcome.run_green)
        self.assertEqual(self.reruns, [])

    # Tests [@ANCHOR: known_red:decide]
    def test_a_new_failure_next_to_a_known_red_one_is_still_red(self):
        outcome = self.decide(
            observe(fail("m", "A", "t1"), fail("m", "A", "t2"), result(2, 0, 3)), self.baseline("m:A.t1"), self.rerun_failing
        )
        self.assertFalse(outcome.run_green)
        self.assertEqual(self.reruns, [["m:A.t2"]])

    # Tests [@ANCHOR: known_red:decide]
    def test_a_test_above_the_threshold_is_listed_as_fix_me_for_the_modules_under_test(self):
        for _ in range(4):
            known_red.record_flakes(self.flakes, ["m:A.t2", "other:B.t"], "c")
        outcome = self.decide(observe(fail("m", "A", "t2"), result(1, 0, 3)), {}, self.rerun_passing, modules={"m"})
        self.assertEqual(sorted(outcome.fix_me), ["m:A.t2"])
        self.assertIn("FIX ME", "\n".join(outcome.summary_lines()))

    # Tests [@ANCHOR: known_red:raw_outcome]
    def test_raw_outcome_reports_every_failure_as_new(self):
        outcome = known_red.raw_outcome(observe(fail("m", "A", "t1"), fail("m", "A", "t1"), result(2, 0, 3)))
        self.assertFalse(outcome.run_green)
        self.assertEqual(outcome.failed_again, ["m:A.t1"])

    # Tests [@ANCHOR: known_red:rerun_tags]
    def test_rerun_tags_select_exactly_the_tests(self):
        self.assertEqual(known_red.rerun_tags(["b:C.m", "a:D.n"]), "/a:D.n,/b:C.m")

    # Tests [@ANCHOR: known_red:write_results]
    # Tests [@ANCHOR: known_red:PolicyOutcome]
    def test_results_json_lists_known_red_flakes_and_new_failures(self):
        first = observe(fail("m", "A", "t1"), fail("m", "A", "t2"), result(2, 0, 7))
        outcome = self.decide(first, self.baseline("m:A.t1"), self.rerun_failing)
        path = os.path.join(self.directory.name, "r.json")
        known_red.write_results(path, outcome, "deadbeef", first)
        with open(path) as handle:
            data = json.load(handle)
        self.assertEqual(data["commit"], "deadbeef")
        self.assertEqual(data["counts"], {"failed": 2, "errors": 0, "total": 7})
        self.assertEqual(data["new_failures"], ["m:A.t2"])
        self.assertEqual(list(data["known_red"]), ["m:A.t1"])

    # Tests [@ANCHOR: known_red:git_head]
    def test_git_head_names_the_commit_or_is_empty(self):
        self.assertEqual(known_red.git_head(self.directory.name), "")
        subprocess.run(["git", "init", "-q", self.directory.name], check=True)
        subprocess.run(["git", "-C", self.directory.name, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        self.assertRegex(known_red.git_head(self.directory.name), r"^[0-9a-f]{40}$")


class ApplyFailurePolicyTests(unittest.TestCase):
    """`apply_failure_policy` in test.py turns the policy's verdict into the run's exit status."""

    @classmethod
    def setUpClass(cls):
        cls.runner = _load_test_py()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = unittest.mock.patch.dict(os.environ, {
            known_red.BASELINE_ENV: write_baseline(self.directory.name, entry("m:A.t1")),
            known_red.FLAKE_FILE_ENV: os.path.join(self.directory.name, "flakes.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def extractor(self, *lines):
        observation = observe(*lines)
        return SimpleNamespace(
            observation=observation, output_path=os.path.join(self.directory.name, "filtered_test.txt"),
            cpu_limit_line=None, signal_line=None, policy_outcome=None,
        )

    def apply(self, extractor, rc=1, raw=False, rerun=None):
        return self.runner.apply_failure_policy(
            SimpleNamespace(raw_failures=raw), extractor, rc, rerun or (lambda ids: observe(result(0, 0, 0))), self.directory.name, ["m"], 1
        )

    # Tests [@ANCHOR: test:apply_failure_policy]
    def test_only_tolerated_failures_make_the_run_exit_zero_and_write_the_results(self):
        extractor = self.extractor(fail("m", "A", "t1"), result(1, 0, 4))
        self.assertEqual(self.apply(extractor), 0)
        self.assertTrue(extractor.policy_outcome.run_green)
        with open(os.path.join(self.directory.name, "test_results.json")) as handle:
            self.assertTrue(json.load(handle)["run_green"])

    # Tests [@ANCHOR: test:apply_failure_policy]
    def test_a_new_failure_keeps_the_exit_status(self):
        extractor = self.extractor(fail("m", "A", "t2"), result(1, 0, 4))
        self.assertEqual(self.apply(extractor), 1)

    # Tests [@ANCHOR: test:apply_failure_policy]
    def test_a_run_with_no_verdict_or_killed_by_a_signal_is_never_made_green(self):
        self.assertEqual(self.apply(self.extractor()), 1)
        self.assertEqual(self.apply(self.extractor(fail("m", "A", "t1"), result(1, 0, 4)), rc=-9), -9)
        limited = self.extractor(fail("m", "A", "t1"), result(1, 0, 4))
        limited.cpu_limit_line = "cpu limit"
        self.assertEqual(self.apply(limited), 1)

    # Tests [@ANCHOR: test:apply_failure_policy]
    def test_raw_failures_ignores_the_baseline_and_still_writes_the_results(self):
        extractor = self.extractor(fail("m", "A", "t1"), result(1, 0, 4))
        self.assertEqual(self.apply(extractor, raw=True), 1)
        self.assertIsNone(extractor.policy_outcome)
        with open(os.path.join(self.directory.name, "test_results.json")) as handle:
            self.assertEqual(json.load(handle)["new_failures"], ["m:A.t1"])

    # Tests [@ANCHOR: test:apply_failure_policy]
    def test_a_broken_baseline_keeps_the_exit_status_and_says_why(self):
        with unittest.mock.patch.dict(os.environ, {known_red.BASELINE_ENV: "/nonexistent/x.json"}):
            extractor = self.extractor(fail("m", "A", "t1"), result(1, 0, 4))
            self.assertEqual(self.apply(extractor), 1)

    # Tests [@ANCHOR: test:apply_failure_policy]
    def test_the_extractor_records_what_it_reads_and_prints_the_tolerated_tests(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as logs, unittest.mock.patch.dict(os.environ, {"HAMS_REAL_LOG_DIRECTORY": logs}):
            extractor = self.runner.FailureExtractor(logs, disable_atexit=True)
            for line in (fail("m", "A", "t1"), result(1, 0, 4)):
                extractor.process_line(line + "\n")
            self.assertEqual(extractor.observation.failed_ids(), {"m:A.t1"})
            extractor.output_path = os.path.join(logs, "filtered_test.txt")
            self.assertEqual(self.apply(extractor), 0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                extractor.finish_and_write()
        self.assertIn("none is new", out.getvalue())
        self.assertIn("m:A.t1", out.getvalue())
        self.assertNotIn("Odoo exited with status", out.getvalue())


class AccountabilityTests(unittest.TestCase):
    """A baselined test is tolerated for a limited time, then fails the run unless renewed with a reason."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.flakes = os.path.join(self.tmp.name, "flakes.json")

    def baseline(self, *entries):
        return known_red.load_baseline([write_baseline(self.tmp.name, *entries)])

    # Tests [@ANCHOR: known_red:max_age_days]
    def test_limit_default_override_and_bad_values(self):
        self.assertEqual(known_red.max_age_days({}), 3.0)
        self.assertEqual(known_red.max_age_days({known_red.MAX_AGE_ENV: "0.25"}), 0.25)
        for bad in ("0", "-2", "x", "inf", "nan"):
            with self.assertRaises(known_red.BaselineError, msg=bad):
                known_red.max_age_days({known_red.MAX_AGE_ENV: bad})

    # Tests [@ANCHOR: known_red:age_text]
    def test_age_text(self):
        self.assertEqual(known_red.age_text(3.14), "3.1 days")
        self.assertEqual(known_red.age_text(0.5), "12 hours")

    # Tests [@ANCHOR: known_red:BaselineEntry]
    def test_age_counts_from_first_seen_then_from_the_last_renewal(self):
        entries = self.baseline(entry("m:A.old", days_ago=5), dict(entry("m:A.renewed", days_ago=5), renewed={"time": when(1), "reason": "waiting"}))
        self.assertAlmostEqual(entries["m:A.old"].age_days(), 5.0, places=1)
        self.assertAlmostEqual(entries["m:A.renewed"].age_days(), 1.0, places=1)

    # Tests [@ANCHOR: known_red:decide]
    # Tests [@ANCHOR: known_red:classify]
    def test_an_entry_older_than_the_limit_fails_the_run_and_is_not_rerun(self):
        reruns = []
        first = observe(fail("m", "A", "old"), result(1, 0, 3))
        outcome = known_red.decide(first, self.baseline(entry("m:A.old", days_ago=3.5)), lambda ids: reruns.append(ids), self.flakes, "c", 3)
        self.assertFalse(outcome.run_green)
        self.assertEqual(reruns, [])
        self.assertEqual(list(outcome.expired), ["m:A.old"])
        text = "\n".join(outcome.summary_lines())
        self.assertIn("EXPIRED known red (3.5 days, over the 3 day limit, owner alice)", text)
        self.assertIn("known_red.py renew m:A.old --reason", text)
        self.assertEqual(outcome.results("c")["new_failures"], ["m:A.old"])

    # Tests [@ANCHOR: known_red:decide]
    def test_a_young_entry_is_tolerated_with_its_age_and_owner_in_the_summary(self):
        outcome = known_red.decide(observe(fail("m", "A", "t"), result(1, 0, 3)), self.baseline(entry("m:A.t", owner="bob", days_ago=2)),
                                   lambda ids: None, self.flakes, "c", 3)
        self.assertTrue(outcome.run_green)
        self.assertIn("m:A.t  known red for 2.0 days, owner bob", "\n".join(outcome.summary_lines()))
        self.assertAlmostEqual(outcome.results("c")["known_red"]["m:A.t"]["age_days"], 2.0, places=1)

    # Tests [@ANCHOR: known_red:decide]
    def test_a_renewal_with_a_reason_restores_tolerance_and_the_limit_is_configurable(self):
        old = dict(entry("m:A.t", days_ago=9), renewed={"time": when(1), "reason": "vendor fix pending"})
        failing = observe(fail("m", "A", "t"), result(1, 0, 3))
        self.assertTrue(known_red.decide(failing, self.baseline(old), lambda ids: None, self.flakes, "c", 3).run_green)
        self.assertTrue(known_red.decide(failing, self.baseline(entry("m:A.t", days_ago=9)), lambda ids: None, self.flakes, "c", 3,
                                         max_age=10).run_green)

    # Tests [@ANCHOR: known_red:show_lines]
    def test_show_lists_the_oldest_first_and_marks_the_expired(self):
        lines = known_red.show_lines(self.baseline(entry("m:A.young", days_ago=1), entry("m:A.old", days_ago=4, owner="zed")))
        self.assertIn("tolerated for at most 3 days", lines[0])
        self.assertIn("m:A.old  known red for 4.0 days (EXPIRED: fails the run), owner zed", lines[1])
        self.assertIn("m:A.young", lines[2])
        self.assertIn("empty", known_red.show_lines({})[0])

    # Tests [@ANCHOR: known_red:renew_entry]
    # Tests [@ANCHOR: known_red:main]
    def test_renew_through_the_command_line_rewrites_the_file_and_refuses_bad_requests(self):
        path = write_baseline(self.tmp.name, entry("m:A.t", days_ago=5), entry("m:A.other"))
        out = []
        env = {known_red.BASELINE_ENV: path}
        with unittest.mock.patch.dict(os.environ, env):
            self.assertEqual(known_red.main(["show"], out.append), 0)
            self.assertIn("EXPIRED", out[1])
            self.assertEqual(known_red.main(["renew", "m:A.t", "--reason", "fix in review", "--owner", "carol"], out.append), 0)
            self.assertNotIn("EXPIRED", "\n".join(out[-3:]))
            self.assertEqual(known_red.main(["renew", "m:A.nope", "--reason", "x"], out.append), 2)
            self.assertEqual(known_red.main(["renew", "m:A.t", "--reason", "  "], out.append), 2)
        with open(path) as handle:
            data = json.load(handle)
        renewed = [e for e in data["entries"] if e["test_id"] == "m:A.t"][0]
        self.assertEqual((renewed["owner"], renewed["renewed"]["reason"]), ("carol", "fix in review"))
        self.assertEqual(len(data["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
