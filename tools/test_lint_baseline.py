#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for lint_baseline.py, the known-red baseline of the lint run (ADR 0117), and the pieces of
run_linters.py, lint_batch.py and lint_stamp_gate.py that carry it into a verdict and a pass stamp.

The canary test uses a real git repository with an origin and the real combined-tree builder; only the
linter command is a fake script that writes the `--results-json` file.

Tests [@ANCHOR: lint_baseline:BaselineFile]
Tests [@ANCHOR: lint_baseline:cargo_panic_message]
Tests [@ANCHOR: lint_baseline:enabled]
Tests [@ANCHOR: lint_baseline:baseline_dir]
Tests [@ANCHOR: lint_baseline:baseline_path]
Tests [@ANCHOR: lint_baseline:normalize_message]
Tests [@ANCHOR: lint_baseline:failure_items]
Tests [@ANCHOR: lint_baseline:items_from_failures]
Tests [@ANCHOR: lint_baseline:read_baseline]
Tests [@ANCHOR: lint_baseline:write_baseline]
Tests [@ANCHOR: lint_baseline:classify]
Tests [@ANCHOR: lint_baseline:judge]
Tests [@ANCHOR: lint_baseline:report_lines]
Tests [@ANCHOR: lint_baseline:stamp_field]
Tests [@ANCHOR: lint_baseline:refresh]
Tests [@ANCHOR: lint_baseline:show_lines]
Tests [@ANCHOR: lint_baseline:max_age_days]
Tests [@ANCHOR: lint_baseline:item_age_days]
Tests [@ANCHOR: lint_baseline:renew]
Tests [@ANCHOR: lint_batch:read_known_red]
Tests [@ANCHOR: lint_stamp_gate:known_red_note]
Tests [@ANCHOR: run_linters:baseline_command]
"""

import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import lint_baseline as lb  # noqa: E402
import lint_batch  # noqa: E402
import lint_stamp_gate  # noqa: E402
import run_linters  # noqa: E402
import test_batch_queue as helpers  # noqa: E402

PYTEST_OUT = """..F.
=========================== short test summary info ============================
FAILED tools/test_a.py::TestA::test_one - AssertionError: 3 != 4 at /tmp/pytest-of-x/pytest-12/a.py:44
ERROR tools/test_b.py - ImportError: no module named zzz
2 failed, 90 passed in 12.30s
"""


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_CACHE_HOME": os.path.join(self.tmp.name, "cache"),
                    "HAMS_BATCH_TREE_DIR": os.path.join(self.tmp.name, "trees")}


class ItemTests(Env):
    def test_switch_and_paths(self):
        self.assertTrue(lb.enabled({}))
        self.assertFalse(lb.enabled({"HAMS_LINT_BASELINE": "off"}))
        self.assertFalse(lb.enabled({"HAMS_LINT_BASELINE": "0"}))
        self.assertEqual(lb.baseline_dir(self.env), os.path.join(self.env["XDG_CACHE_HOME"], "hams-lint", "baseline"))
        self.assertTrue(lb.baseline_path("hams_com", self.env).endswith("baseline/hams_com.json"))

    def test_normalize_removes_what_changes_between_runs_and_keeps_numbers(self):
        a = lb.normalize_message("3 != 4 at /tmp/x/a.py:44 in 1.5s 0xdeadbeef run deadbeef01")
        b = lb.normalize_message("3 != 4 at /var/y/a.py:90 in 20ms 0xcafe0001 run cafecafe99")
        self.assertEqual(a, b)
        self.assertNotEqual(lb.normalize_message("3 != 4"), lb.normalize_message("3 != 5"))

    def test_a_fraction_and_a_long_decimal_are_not_a_path_or_a_hex_id(self):
        self.assertNotEqual(lb.normalize_message("got 3/4"), lb.normalize_message("got 3/5"))
        self.assertNotEqual(lb.normalize_message("limit 10000000"), lb.normalize_message("limit 10000001"))
        self.assertEqual(lb.normalize_message("at /tmp/a/b run deadbeef01"), lb.normalize_message("at /var/c run cafecafe99"))

    def test_pytest_lines_give_one_item_per_test_without_the_shard(self):
        items = lb.failure_items("pytest tools/[2of4]", PYTEST_OUT)
        self.assertEqual(set(items), {"pytest tools/::tools/test_a.py::TestA::test_one", "pytest tools/::tools/test_b.py"})
        other_shard = lb.failure_items("pytest tools/[3of4]", PYTEST_OUT)
        self.assertEqual(items, other_shard)

    def test_unittest_and_cargo_lines(self):
        unit = "FAIL: test_x (pkg.mod.Case.test_x)\n----\nTraceback\nAssertionError: nope\n\nRan 3 tests\n"
        got = lb.failure_items("unit", "ERROR: test_y (pkg.mod.Case)\n---\nValueError: bad\n=====\n" + unit)
        self.assertIn("unit::pkg.mod.Case.test_y", got)
        self.assertIn("unit::pkg.mod.Case.test_x", got)
        self.assertEqual(got["unit::pkg.mod.Case.test_y"]["message"], "ValueError: bad")
        cargo = lb.failure_items("cargo test", "test a::b::c ... FAILED\ntest a::d ... ok\n")
        self.assertEqual(list(cargo), ["cargo test::a::b::c"])

    def test_cargo_failure_carries_the_panic_message_without_its_line_number(self):
        def out(line, message):
            return ("test a::b ... FAILED\n\nfailures:\n\n---- a::b stdout ----\nthread 'a::b' panicked at src/x.rs:%d:5:\n%s\n"
                    "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n\nfailures:\n    a::b\n" % (line, message))
        first = lb.failure_items("cargo test", out(10, "assertion `left == right` failed\n  left: 3\n right: 4"))["cargo test::a::b"]
        moved = lb.failure_items("cargo test", out(99, "assertion `left == right` failed\n  left: 3\n right: 4"))["cargo test::a::b"]
        other = lb.failure_items("cargo test", out(10, "assertion `left == right` failed\n  left: 3\n right: 5"))["cargo test::a::b"]
        self.assertIn("left: 3", first["message"])
        self.assertNotIn("x.rs", first["message"])
        self.assertEqual(first["signature"], moved["signature"])
        self.assertNotEqual(first["signature"], other["signature"])
        self.assertEqual(lb.cargo_panic_message(["test a ... FAILED"], "a"), "")

    def test_step_without_tests_is_one_item_whose_signature_follows_the_findings(self):
        out1 = "x.py:1: E1 bad\nx.py:2: E2 worse\n....\n"
        out2 = "x.py:2: E2 worse\nx.py:1: E1 bad\n\n"
        out3 = out1 + "y.py:9: E3 new\n"
        one, two, three = (lb.failure_items("flake8", o)["flake8"] for o in (out1, out2, out3))
        self.assertEqual(one["signature"], two["signature"])
        self.assertNotEqual(one["signature"], three["signature"])
        merged = lb.items_from_failures({"flake8": out1, "pytest tools/[1of2]": PYTEST_OUT})
        self.assertEqual(len(merged), 3)

    def test_shards_with_different_failures_do_not_hide_one_another(self):
        known, fresh = "x.py:1: E1 bad\n", "z.py:7: E9 something else\n"
        recorded = lb.items_from_failures({"step [1of2]": known})["step"]
        both = lb.items_from_failures({"step [1of2]": known, "step [2of2]": fresh})["step"]
        reverse = lb.items_from_failures({"step [2of2]": fresh, "step [1of2]": known})["step"]
        self.assertNotEqual(both["signature"], recorded["signature"])
        self.assertEqual(both["signature"], reverse["signature"])
        same = lb.items_from_failures({"step [1of2]": known, "step [2of2]": known})["step"]
        self.assertEqual(same["signature"], recorded["signature"])


class BaselineFileTests(Env):
    def test_write_read_and_since_is_carried_over(self):
        self.assertIsNone(lb.read_baseline("hams_com", self.env))
        items = lb.failure_items("flake8", "x.py:1: E1 bad\n")
        first = lb.write_baseline("hams_com", "aaa111", items, self.env)
        self.assertEqual(first["items"]["flake8"]["since"], "aaa111")
        self.assertEqual(lb.read_baseline("hams_com", self.env)["main"], "aaa111")
        again = lb.write_baseline("hams_com", "bbb222", items, self.env, previous=first)
        self.assertEqual(again["main"], "bbb222")
        self.assertEqual(again["items"]["flake8"]["since"], "aaa111")
        changed = lb.failure_items("flake8", "x.py:1: E1 different\n")
        third = lb.write_baseline("hams_com", "ccc333", changed, self.env, previous=again)
        self.assertEqual(third["items"]["flake8"]["since"], "ccc333")
        with open(lb.baseline_path("hams_com", self.env), "w") as handle:
            handle.write("not json")
        self.assertIsNone(lb.read_baseline("hams_com", self.env))
        self.assertEqual(lb.show_lines("nothing", self.env)[0][:9], "baseline:")

    def test_show_lines_list_items(self):
        lb.write_baseline("hams_com", "aaa111", lb.failure_items("flake8", "x.py:1: bad\n"), self.env)
        text = "\n".join(lb.show_lines("hams_com", self.env))
        self.assertIn("flake8", text)
        self.assertIn("1 known-red", text)


class JudgeTests(Env):
    def doc(self, failed, since="aaa"):
        items = lb.items_from_failures(failed)
        return {"items": {k: dict(v, since=since) for k, v in items.items()}, "main": "aaa"}

    def test_same_failure_is_tolerated_new_and_changed_ones_block(self):
        base = self.doc({"pytest tools/[1of2]": PYTEST_OUT})
        same = lb.items_from_failures({"pytest tools/[2of2]": PYTEST_OUT})
        tolerated, blocking = lb.classify(same, base)
        self.assertEqual(len(tolerated), 2)
        self.assertEqual(blocking, {})
        worse = PYTEST_OUT.replace("3 != 4", "5 != 6") + "FAILED tools/test_c.py::t - boom\n"
        tolerated, blocking = lb.classify(lb.items_from_failures({"pytest tools/": worse}), base)
        self.assertEqual(len(tolerated), 1)
        reasons = sorted(blocking.values())
        self.assertTrue(reasons[0].startswith("changed"))
        self.assertTrue(reasons[1].startswith("new"))
        self.assertEqual(lb.classify(same, None)[0], {})

    def test_negative_controls_a_recorded_failure_cannot_hide_a_failure_the_output_does_not_name(self):
        base = self.doc({"pytest tools/": PYTEST_OUT})
        # pytest counts four failures and lists two: the other two are nobody's known red.
        more = PYTEST_OUT.replace("2 failed, 90 passed", "4 failed, 88 passed")
        tolerated, blocking = lb.classify(lb.items_from_failures({"pytest tools/": more}), base)
        self.assertEqual(len(tolerated), 2)
        self.assertEqual(len(blocking), 1)
        self.assertTrue(next(iter(blocking.values())).startswith("new"))
        # the worker died after the recorded failure was printed
        crashed = PYTEST_OUT + "INTERNALERROR> Traceback (most recent call last):\n"
        tolerated, blocking = lb.classify(lb.items_from_failures({"pytest tools/": crashed}), base)
        self.assertEqual(len(blocking), 1)
        # an exact repeat of main's own failure is still tolerated
        tolerated, blocking = lb.classify(lb.items_from_failures({"pytest tools/": PYTEST_OUT}), base)
        self.assertEqual((len(tolerated), blocking), (2, {}))

    def test_negative_controls_empty_output_and_an_empty_baseline_tolerate_nothing(self):
        base = self.doc({"pytest tools/": PYTEST_OUT})
        tolerated, blocking = lb.classify(lb.items_from_failures({"flake8": ""}), base)
        self.assertEqual((tolerated, len(blocking)), ({}, 1))
        empty = {"items": {}, "main": "aaa"}
        tolerated, blocking = lb.classify(lb.items_from_failures({"pytest tools/": PYTEST_OUT}), empty)
        self.assertEqual((tolerated, len(blocking)), ({}, 2))
        self.assertFalse(lb.judge({"flake8": "x"}, "nokey", "/r", environ=self.env)["passes"])
        self.assertFalse(lb.judge({}, "nokey", "/r", environ=self.env)["passes"])

    def test_since_must_be_in_the_branch_history(self):
        base = self.doc({"flake8": "x.py:1: bad\n"}, since="feedface")
        items = lb.items_from_failures({"flake8": "x.py:1: bad\n"})
        seen = []

        def contains(repo, commit, head):
            seen.append((commit, head))
            return False

        tolerated, blocking = lb.classify(items, base, repo="/r", head="HEAD", contains=contains)
        self.assertEqual(tolerated, {})
        self.assertIn("rebase", blocking["flake8"])
        self.assertEqual(seen, [("feedface", "HEAD")])

    def test_judge_passes_only_when_every_failure_is_tolerated(self):
        work = helpers.make_repo(self.tmp.name)
        main = helpers.git(work, "rev-parse", "HEAD")
        failed = {"flake8": "x.py:1: bad\n"}
        lb.write_baseline("hams_demo", main, lb.items_from_failures(failed), self.env)
        env = dict(self.env)
        verdict = lb.judge(failed, "hams_demo", work, "HEAD", env)
        self.assertTrue(verdict["passes"])
        mixed = dict(failed, check_x="boom\n")
        verdict = lb.judge(mixed, "hams_demo", work, "HEAD", env)
        self.assertFalse(verdict["passes"])
        self.assertEqual(list(verdict["blocking"]), ["check_x"])
        self.assertEqual(list(verdict["tolerated"]), ["flake8"])
        off = lb.judge(failed, "hams_demo", work, "HEAD", dict(env, HAMS_LINT_BASELINE="off"))
        self.assertFalse(off["passes"])
        self.assertFalse(lb.judge({}, "hams_demo", work, "HEAD", env)["passes"])
        self.assertFalse(lb.judge(failed, "no_such_repo", work, "HEAD", env)["passes"])

    def test_report_and_stamp_field_name_what_was_tolerated(self):
        work = helpers.make_repo(self.tmp.name)
        main = helpers.git(work, "rev-parse", "HEAD")
        lb.write_baseline("hams_demo", main, lb.items_from_failures({"flake8": "x.py:1: bad\n"}), self.env)
        verdict = lb.judge({"flake8": "x.py:1: bad\n", "other": "y\n"}, "hams_demo", work, "HEAD", self.env)
        text = "\n".join(lb.report_lines(verdict))
        self.assertIn("KNOWN RED ON MAIN", text)
        self.assertIn("known red: flake8", text)
        self.assertIn("BLOCKS: other", text)
        field = lb.stamp_field({"pytest tools/::a b": {"age_days": 3.04, "owner": "ann lee"}, "flake8": {}}, "0123456789abcdef")
        self.assertEqual(field, " known_red=flake8(0.0d;unassigned),pytest_tools/::a_b(3.0d;ann_lee) baseline=0123456789ab")
        many = lb.stamp_field({"s%02d" % i: {} for i in range(15)}, "m")
        self.assertIn(",+3 ", many + " ")
        self.assertEqual(lb.stamp_field({}, "m"), "")


class StampTests(unittest.TestCase):
    def test_stamp_text_and_gate_note(self):
        text = run_linters.stamp_text("changed", ["flake8"], known_red=" known_red=flake8 baseline=abc")
        self.assertIn("known_red=flake8 baseline=abc", text)
        self.assertIn("known red on main", lint_stamp_gate.known_red_note(text))
        self.assertIsNone(lint_stamp_gate.known_red_note(run_linters.stamp_text("changed", ["flake8"])))
        self.assertIsNone(lint_stamp_gate.refusal(text, ["a.py"]))

    def test_read_known_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            self.assertEqual(lint_batch.read_known_red(path), "")
            with open(path, "w") as handle:
                json.dump({"known_red": " known_red=a baseline=b"}, handle)
            self.assertEqual(lint_batch.read_known_red(path), " known_red=a baseline=b")


FAKE = r'''
import json, sys
with open(sys.argv[1], "w") as handle:
    json.dump({"selected": ["flake8", "pytest tools/[1of2]"],
               "steps": [{"name": "flake8", "returncode": 1}, {"name": "pytest tools/[1of2]", "returncode": 0}],
               "outputs": {"flake8": "x.py:1: E1 bad\n"}}, handle)
sys.exit(1)
'''


class RefreshTests(Env):
    def command(self, tree, results):
        script = os.path.join(self.tmp.name, "fake.py")
        with open(script, "w") as handle:
            handle.write(FAKE)
        self.trees_seen = getattr(self, "trees_seen", []) + [tree]
        return [sys.executable, script, results]

    def test_canary_records_a_clean_main_and_is_idempotent(self):
        work = helpers.make_repo(self.tmp.name)
        lines = []
        code = lb.refresh(work, "hams_demo", "hams_demo", environ=self.env, out=lines.append, command=self.command,
                          prepare=lambda tree: None)
        self.assertEqual(code, 0, lines)
        doc = lb.read_baseline("hams_demo", self.env)
        self.assertEqual(doc["main"], helpers.git(work, "rev-parse", "origin/main"))
        self.assertEqual(list(doc["items"]), ["flake8"])
        self.assertFalse(os.path.exists(self.trees_seen[0]), "the temporary tree is removed")
        calls = len(self.trees_seen)
        lb.refresh(work, "hams_demo", "hams_demo", environ=self.env, out=lines.append, command=self.command)
        self.assertEqual(len(self.trees_seen), calls, "current baseline: no second run")
        self.assertIn("is current", lines[-1])
        lb.refresh(work, "hams_demo", "hams_demo", environ=self.env, out=lines.append, command=self.command,
                   force=True, prepare=lambda tree: None)
        self.assertEqual(len(self.trees_seen), calls + 1)

    def test_a_run_that_reports_no_steps_keeps_the_old_baseline(self):
        work = helpers.make_repo(self.tmp.name)
        lb.write_baseline("hams_demo", "old", {"x": {"step": "x", "test": "", "signature": "s", "message": "m"}}, self.env)
        lines = []
        code = lb.refresh(work, "hams_demo", "hams_demo", environ=self.env, out=lines.append,
                          command=lambda tree, results: [sys.executable, "-c", "import sys; sys.exit(75)"],
                          prepare=lambda tree: None)
        self.assertEqual(code, 75)
        self.assertEqual(lb.read_baseline("hams_demo", self.env)["main"], "old")

    def test_baseline_command_show_and_main_shared_checkout(self):
        class Options:
            baseline_show = True
            baseline_refresh = False
            baseline_force = False
            baseline_renew = None
            remote = ""

        work = helpers.make_repo(self.tmp.name)
        self.assertEqual(run_linters.baseline_command(Options, work, work), 0)


DAY = 86400.0


class AccountabilityTests(Env):
    """Known-red items age: they are tolerated for a limited time, then block unless renewed with a reason."""

    def setUp(self):
        super().setUp()
        self.t0 = 1_800_000_000.0
        self.items = lb.failure_items("flake8", "x.py:1: E1 bad\n")
        self.doc = lb.write_baseline("hams_com", "aaa111", self.items, self.env, now=lambda: self.t0)

    def at(self, days):
        return lambda: self.t0 + days * DAY

    # Tests [@ANCHOR: lint_baseline:max_age_days]
    def test_the_limit_defaults_to_three_days_and_a_bad_value_is_an_error(self):
        self.assertEqual(lb.max_age_days({}), 3.0)
        self.assertEqual(lb.max_age_days({lb.MAX_AGE_ENV: "0.5"}), 0.5)
        for bad in ("0", "-1", "abc", "inf", "nan"):
            with self.assertRaises(ValueError, msg=bad):
                lb.max_age_days({lb.MAX_AGE_ENV: bad})

    # Tests [@ANCHOR: lint_baseline:item_age_days]
    def test_age_counts_from_first_seen_then_from_a_renewal_and_old_records_use_created(self):
        item = self.doc["items"]["flake8"]
        self.assertAlmostEqual(lb.item_age_days(item, self.doc["created"], self.at(2.5)), 2.5, places=2)
        renewed = dict(item, renewed={"time": time_text(self.t0 + 2 * DAY), "reason": "r"})
        self.assertAlmostEqual(lb.item_age_days(renewed, None, self.at(2.5)), 0.5, places=2)
        legacy = {k: v for k, v in item.items() if k != "first_seen_time"}
        self.assertAlmostEqual(lb.item_age_days(legacy, self.doc["created"], self.at(1)), 1.0, places=2)
        self.assertEqual(lb.item_age_days({}, None, self.at(1)), 0.0)
        self.assertEqual(lb.item_age_days({"first_seen_time": "garbage"}, None, self.at(1)), 0.0)

    # Tests [@ANCHOR: lint_baseline:write_baseline]
    def test_a_refresh_keeps_first_seen_owner_and_renewal_and_a_changed_failure_starts_again(self):
        lb.renew("hams_com", "flake8", "waiting for the vendor", owner="ann", environ=self.env, now=self.at(1))
        later = lb.write_baseline("hams_com", "bbb222", self.items, self.env, previous=lb.read_baseline("hams_com", self.env),
                                  now=self.at(2))
        item = later["items"]["flake8"]
        self.assertEqual((item["first_seen_time"], item["owner"], item["renewed"]["reason"]),
                         (self.doc["items"]["flake8"]["first_seen_time"], "ann", "waiting for the vendor"))
        changed = lb.write_baseline("hams_com", "ccc333", lb.failure_items("flake8", "x.py:1: E1 other\n"), self.env,
                                    previous=later, now=self.at(3))["items"]["flake8"]
        self.assertEqual((changed["owner"], "renewed" in changed), ("unassigned", False))
        self.assertNotEqual(changed["first_seen_time"], item["first_seen_time"])

    # Tests [@ANCHOR: lint_baseline:classify]
    # Tests [@ANCHOR: lint_baseline:judge]
    def test_an_item_older_than_the_limit_stops_being_tolerated_until_renewed(self):
        fresh = lb.judge({"flake8": "x.py:1: E1 bad\n"}, "hams_com", None, environ=self.env, now=self.at(2.9))
        self.assertTrue(fresh["passes"])
        self.assertAlmostEqual(fresh["tolerated"]["flake8"]["age_days"], 2.9, places=2)
        old = lb.judge({"flake8": "x.py:1: E1 bad\n"}, "hams_com", None, environ=self.env, now=self.at(3.1))
        self.assertFalse(old["passes"])
        self.assertIn("known red for 3.1 days (limit 3 days", old["blocking"]["flake8"])
        self.assertIn("--baseline-renew 'flake8'", old["blocking"]["flake8"])
        lb.renew("hams_com", "flake8", "tracked in the vendor's queue", environ=self.env, now=self.at(3.1))
        renewed = lb.judge({"flake8": "x.py:1: E1 bad\n"}, "hams_com", None, environ=self.env, now=self.at(4))
        self.assertTrue(renewed["passes"])
        self.assertTrue(lb.judge({"flake8": "x.py:1: E1 bad\n"}, "hams_com", None, environ=dict(self.env, **{lb.MAX_AGE_ENV: "10"}),
                                 now=self.at(9))["passes"])

    # Tests [@ANCHOR: lint_baseline:renew]
    def test_a_renewal_waits_for_the_canary_run_that_holds_the_lock_and_then_keeps_what_it_wrote(self):
        import fcntl
        import threading
        import time
        os.makedirs(lb.baseline_dir(self.env), exist_ok=True)
        canary = open(os.path.join(lb.baseline_dir(self.env), "hams_com.lock"), "a+")
        fcntl.flock(canary, fcntl.LOCK_EX)
        done = []
        worker = threading.Thread(target=lambda: done.append(lb.renew("hams_com", "flake8", "why", environ=self.env, now=self.at(1))))
        worker.start()
        time.sleep(0.5)
        self.assertEqual(done, [])  # still waiting for the canary
        fcntl.flock(canary, fcntl.LOCK_UN)
        canary.close()
        worker.join(10)
        self.assertEqual(len(done), 1)
        self.assertEqual(lb.read_baseline("hams_com", self.env)["items"]["flake8"]["renewed"]["reason"], "why")

    # Tests [@ANCHOR: lint_baseline:renew]
    def test_a_renewal_needs_a_reason_and_an_existing_item(self):
        with self.assertRaises(ValueError):
            lb.renew("hams_com", "flake8", "  ", environ=self.env)
        with self.assertRaises(KeyError):
            lb.renew("hams_com", "no such item", "why", environ=self.env)
        with self.assertRaises(KeyError):
            lb.renew("hams_open", "flake8", "why", environ=self.env)

    # Tests [@ANCHOR: lint_baseline:show_lines]
    # Tests [@ANCHOR: lint_baseline:report_lines]
    def test_show_lists_the_oldest_first_with_age_owner_and_expiry_and_the_report_names_age_and_owner(self):
        more = lb.failure_items("pytest tools/", "FAILED tools/test_a.py::T::t - boom\n")
        lb.write_baseline("hams_com", "bbb222", dict(self.items, **more), self.env, previous=lb.read_baseline("hams_com", self.env),
                          now=self.at(2))
        lines = lb.show_lines("hams_com", self.env, now=self.at(3.5))
        self.assertIn("tolerated for at most 3 days", lines[0])
        self.assertIn("known red for 3.5 days (EXPIRED: blocks), owner unassigned", lines[1])
        self.assertIn("known red for 1.5 days", lines[2])
        verdict = lb.judge({"flake8": "x.py:1: E1 bad\n"}, "hams_com", None, environ=self.env, now=self.at(1))
        self.assertIn("known red for 1.0 days, owner unassigned", "\n".join(lb.report_lines(verdict)))

    # Tests [@ANCHOR: run_linters:baseline_command]
    def test_baseline_renew_command_renews_or_refuses_with_the_cause(self):
        work = helpers.make_repo(self.tmp.name)

        class Options:
            baseline_show = False
            baseline_refresh = False
            baseline_renew = "flake8"
            baseline_reason = "waiting"
            baseline_owner = "ann"
            baseline_force = False
            remote = ""

        key = os.path.basename(run_linters._resolve_main_checkout_root(work).rstrip(os.sep))
        lb.write_baseline(key, "aaa111", self.items, dict(os.environ))
        try:
            self.assertEqual(run_linters.baseline_command(Options, work, work), 0)
            self.assertEqual(lb.read_baseline(key)["items"]["flake8"]["owner"], "ann")
            Options.baseline_reason = ""
            self.assertEqual(run_linters.baseline_command(Options, work, work), 2)
        finally:
            os.unlink(lb.baseline_path(key))


def time_text(epoch):
    import time
    return time.strftime(lb.TIME_FORMAT, time.localtime(epoch))


if __name__ == "__main__":
    unittest.main()
