#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for odoo_test_batch.py, the Odoo test side of the coalescing batch queue (ADR 0115), and the `--solo`
option and batching hook in test.py.

The end-to-end tests use real processes, a real git repository with an origin and the real queue, with a fake
`test.py` in place of the Odoo runner: it "tests" each module named by `-u` (fails when a file of the module
contains BAD) and writes the same `filtered_test.txt` summary the real runner writes.

Tests [@ANCHOR: odoo_test_batch:module_scope]
Tests [@ANCHOR: odoo_test_batch:ineligible_reason]
Tests [@ANCHOR: odoo_test_batch:implicated_modules]
Tests [@ANCHOR: odoo_test_batch:module_finder]
Tests [@ANCHOR: odoo_test_batch:plan_machine_split]
Tests [@ANCHOR: odoo_test_batch:run_on_machine]
Tests [@ANCHOR: odoo_test_batch:estimate_seconds]
Tests [@ANCHOR: odoo_test_batch:load_durations]
Tests [@ANCHOR: odoo_test_batch:record_durations]
Tests [@ANCHOR: odoo_test_batch:write_pass_record]
Tests [@ANCHOR: odoo_test_batch:OdooTestBatch]
Tests [@ANCHOR: odoo_test_batch:maybe_run_batched]
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import odoo_test_batch as otb  # noqa: E402
import test_batch_queue as helpers  # noqa: E402

FAKE_TEST_PY = r'''
import os, sys, time
args = sys.argv[1:]
modules = args[args.index("-u") + 1].split(",")
logdir = args[args.index("-l") + 1]
os.makedirs(logdir, exist_ok=True)
with open(os.environ["FAKE_CALLS"], "a") as calls:
    calls.write(",".join(modules) + "\n")
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
bad = []
for module in modules:
    for root, dirs, files in os.walk(module):
        for name in files:
            if "BAD" in open(os.path.join(root, name)).read():
                bad.append(module)
bad = sorted(set(bad))
with open(os.path.join(logdir, "filtered_test.txt"), "w") as out:
    if os.path.exists("CRASH"):
        out.write("Odoo crashed before any test\n")
    elif bad:
        out.write("=== EXTRACTED TEST FAILURES & ERRORS (Captured 1 blocks) ===\n")
        out.write("Based on the tracebacks, these modules are implicated in the failure(s):\n")
        for module in bad:
            out.write("  - %s\n" % module)
        for module in bad:
            out.write("FAIL: test_x (odoo.addons.%s.tests.test_a.TestA)\n" % module)
    else:
        out.write("No errors or failures detected in the log.\n")
sys.exit(1 if bad or os.path.exists("CRASH") else 0)
'''


def namespace(**kw):
    base = dict(solo=False, mode="standard", module="ham_a,ham_b", test_tags=None, coverage=False, mcp=False,
                pause_on_fail=False, offline_isolation=False, profile=False, build_daemons=False,
                internal_ns_init=False, preflight_done=False, queue_status=False, db="hams_test",
                config="ignore_list.txt", wait_timeout=None)
    base.update(kw)
    return argparse.Namespace(**base)


class UnitTests(unittest.TestCase):
    def test_module_scope(self):
        self.assertEqual(otb.module_scope("ham_b, ham_a,ham_a"), ["ham_a", "ham_b"])
        self.assertEqual(otb.module_scope("hams_open/zero_sudo"), ["zero_sudo"])
        self.assertIsNone(otb.module_scope("ham_a:TestX"))
        self.assertIsNone(otb.module_scope(""))
        self.assertIsNone(otb.module_scope(None))
        self.assertIsNone(otb.module_scope("ham_a,,"))

    def reason(self, args=None, environ=None, cwd_is_root=True, dirty=False):
        return otb.ineligible_reason(args or namespace(), environ or {}, cwd_is_root, dirty)

    def test_plain_committed_module_run_is_eligible(self):
        self.assertIsNone(self.reason())

    def test_everything_that_runs_alone(self):
        self.assertIn("--solo", self.reason(namespace(solo=True)))
        self.assertIn("combined run", self.reason(environ={otb.RUNNER_ENV: "1"}))
        self.assertIn("off", self.reason(environ={"HAMS_TEST_BATCH": "off"}))
        self.assertIn("inside a test run", self.reason(environ={"HAMS_TEST_LOCK_HELD": "1"}))
        self.assertIn("internal", self.reason(namespace(internal_ns_init=True)))
        self.assertIn("internal", self.reason(namespace(preflight_done=True)))
        self.assertIn("mode xml", self.reason(namespace(mode="xml")))
        for flag in ("test_tags", "coverage", "mcp", "pause_on_fail", "offline_isolation", "profile", "build_daemons"):
            self.assertIn("--" + flag.replace("_", "-"), self.reason(namespace(**{flag: "x" if flag == "test_tags" else True})))
        self.assertIn("whole-module", self.reason(namespace(module=None)))
        self.assertIn("whole-module", self.reason(namespace(module="ham_a:TestX")))
        self.assertIn("repository root", self.reason(cwd_is_root=False))
        self.assertIn("uncommitted", self.reason(dirty=True))

    def test_implicated_modules_reads_the_extractor_list(self):
        text = ("noise\nBased on the tracebacks, these modules are implicated in the failure(s):\n  - ham_a\n  - ham_c\n"
                "****\nmore\n  - not_a_module_line_after_stars\n")
        self.assertEqual(otb.implicated_modules(text), ["ham_a", "ham_c"])
        self.assertEqual(otb.implicated_modules("No errors or failures detected in the log."), [])
        self.assertEqual(otb.implicated_modules(""), [])

    def test_module_finder_matches_whole_words_only(self):
        find = otb.module_finder(["ham_a", "ham_ab"])
        self.assertEqual(find("FAIL odoo.addons.ham_ab.tests"), ["ham_ab"])
        self.assertEqual(find("module ham_a and ham_ab"), ["ham_a", "ham_ab"])
        self.assertEqual(find("ham_abc nothing"), [])
        self.assertEqual(otb.module_finder([])("anything"), [])

    def test_plan_machine_split_balances_by_duration(self):
        durations = {"a": 600, "b": 300, "c": 300, "d": 200}
        plan = otb.plan_machine_split(["a", "b", "c", "d"], durations, ["local", "yoga"])
        self.assertEqual(sorted(sum(plan.values(), [])), ["a", "b", "c", "d"])
        loads = sorted(sum(durations[m] for m in mods) for mods in plan.values())
        self.assertEqual(loads, [600, 800])  # LPT: a alone, the rest together... within 4/3 of optimal
        self.assertEqual(otb.plan_machine_split(["z", "y"], {}, ["only"]), {"only": ["y", "z"]})
        self.assertEqual(otb.plan_machine_split(["z"], {}, []), {"local": ["z"]})
        unknown = otb.plan_machine_split(["a", "b", "c"], {}, ["m1", "m2"])  # unrecorded modules count the default
        self.assertEqual(sorted(len(v) for v in unknown.values()), [1, 2])

    def test_run_on_machine_is_a_clear_stub(self):
        with self.assertRaises(NotImplementedError):
            otb.run_on_machine("yoga", "/tree", ["ham_a"], "/logs")

    def test_estimate_and_durations_roundtrip(self):
        self.assertEqual(otb.estimate_seconds(["a", "b"], {"a": 10.0}), 10.0 + otb.DEFAULT_MODULE_SECONDS)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(otb.load_durations(tmp), {})
            otb.record_durations(tmp, ["a", "b"], 100.0)
            self.assertEqual(otb.load_durations(tmp), {"a": 50.0, "b": 50.0})
            otb.record_durations(tmp, ["a"], 150.0)
            self.assertEqual(otb.load_durations(tmp)["a"], 100.0)
            otb.record_durations(tmp, [], 5.0)
            otb.record_durations(tmp, ["a"], 0)
            self.assertEqual(otb.load_durations(tmp)["a"], 100.0)
            with open(os.path.join(tmp, "durations.json"), "w") as handle:
                handle.write('{"x": NaN, "y": 3, "z": "no"}')
            self.assertEqual(otb.load_durations(tmp), {"y": 3.0})

    def test_write_pass_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = otb.write_pass_record("abc", ["ham_a", "ham_b"], 4, {"XDG_CACHE_HOME": tmp})
            self.assertEqual(path, os.path.join(tmp, "hams-test", "passed", "abc"))
            text = open(path).read()
            self.assertTrue(text.startswith("tests "))
            self.assertTrue(text.rstrip().endswith("modules=ham_a,ham_b batch=4"))
            self.assertIsNone(otb.write_pass_record("none", [], 4, {"XDG_CACHE_HOME": tmp}))

    def test_test_py_has_the_solo_option_and_the_hook(self):
        spec = importlib.util.spec_from_file_location("_hams_test_py_for_batch_tests", os.path.join(_HERE, "test.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.build_arg_parser().parse_args(["--solo", "-u", "ham_a"]).solo)
        self.assertFalse(module.build_arg_parser().parse_args(["-u", "ham_a"]).solo)
        source = open(os.path.join(_HERE, "test.py"), encoding="utf-8").read()
        self.assertIn("odoo_test_batch.maybe_run_batched(early_args)", source)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.work = helpers.make_repo(self.tmp)
        self.fake = os.path.join(self.tmp, "fake_test.py")
        with open(self.fake, "w") as handle:
            handle.write(FAKE_TEST_PY)
        self.calls = os.path.join(self.tmp, "calls.log")
        self.records = os.path.join(self.tmp, "records")
        os.makedirs(self.records)
        self.sdir = os.path.join(self.tmp, "state")
        self.env = dict(os.environ, HAMS_BATCH_TREE_DIR=os.path.join(self.tmp, "trees"), FAKE_CALLS=self.calls,
                        FAKE_SLEEP="2", HAMS_BATCH_DIR=os.path.join(self.tmp, "b"))

    def module_branch(self, name, module, text="ok\n"):
        os.makedirs(os.path.join(self.work, module), exist_ok=True)
        return helpers.commit_branch(self.work, name, {"%s/f_%s.py" % (module, name.replace("-", "_")): text})

    def start(self, branch, tip, modules):
        spec = {"repo": self.work, "branch": branch, "tip": tip, "records": self.records, "fake": self.fake,
                "sdir": self.sdir, "modules": modules}
        return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--drive", json.dumps(spec)], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def finish(self, proc):
        out, _ = proc.communicate(timeout=120)
        line = [item for item in out.splitlines() if item.startswith("RESULT ")]
        self.assertTrue(line, out)
        return json.loads(line[0][7:])["code"], out

    def wait_for_calls(self, count):
        deadline = time.time() + 30
        while time.time() < deadline:
            if os.path.exists(self.calls) and len(open(self.calls).read().splitlines()) >= count:
                return
            time.sleep(0.05)
        self.fail("the first run never started")

    def test_modules_of_waiting_branches_are_tested_once_and_failures_are_attributed_by_module(self):
        first = self.module_branch("first", "ham_first")
        a = self.module_branch("good-a", "ham_a")
        b = self.module_branch("bad-b", "ham_b", "BAD\n")
        c = self.module_branch("good-c", "ham_c")
        p0 = self.start("first", first, "ham_first")
        self.wait_for_calls(1)
        procs = {name: self.start(name, tip, mod) for name, tip, mod in
                 (("good-a", a, "ham_a"), ("bad-b", b, "ham_b"), ("good-c", c, "ham_c"))}
        code0, out0 = self.finish(p0)
        results = {name: self.finish(proc) for name, proc in procs.items()}
        self.assertEqual(code0, 0, out0)
        self.assertEqual([results[n][0] for n in ("good-a", "bad-b", "good-c")], [0, 1, 0], results)
        runs = [line.split(",") for line in open(self.calls).read().splitlines()]
        self.assertEqual(len(runs), 2, runs)  # one run for the first, ONE for the three that waited
        self.assertEqual(sorted(runs[1]), ["ham_a", "ham_b", "ham_c"])  # the union of their modules
        self.assertIn("module ham_b", results["bad-b"][1])
        self.assertIn("FAIL: test_x (odoo.addons.ham_b", results["bad-b"][1])
        for name, tip in (("good-a", a), ("good-c", c), ("first", first)):
            self.assertTrue(os.path.exists(os.path.join(self.records, tip)), name)
        self.assertFalse(os.path.exists(os.path.join(self.records, b)))
        self.assertIn("batch=2", open(os.path.join(self.records, a)).read())
        self.assertTrue(os.path.exists(os.path.join(self.sdir, "durations.json")))  # durations were recorded
        trees = os.path.join(self.tmp, "trees")
        self.assertEqual(os.listdir(trees) if os.path.isdir(trees) else [], [])

    def test_a_crash_with_no_implicated_module_splits_and_passes_the_healthy_half(self):
        self.env["FAKE_SLEEP"] = "1"
        first = self.module_branch("first", "ham_first")
        a = self.module_branch("good-a", "ham_a")
        c = self.module_branch("good-c", "ham_c")
        d = self.module_branch("crash-d", "ham_d")
        os.makedirs(os.path.join(self.work, "x"), exist_ok=True)
        # main itself is fine; branch d adds a CRASH marker at the root, so any run that includes d crashes
        git = helpers.git
        git(self.work, "checkout", "-q", "crash-d")
        with open(os.path.join(self.work, "CRASH"), "w") as handle:
            handle.write("x\n")
        git(self.work, "add", "CRASH")
        git(self.work, "commit", "-q", "-m", "crash")
        d = git(self.work, "rev-parse", "HEAD")
        git(self.work, "checkout", "-q", "main")
        p0 = self.start("first", first, "ham_first")
        self.wait_for_calls(1)
        procs = {name: self.start(name, tip, mod) for name, tip, mod in
                 (("good-a", a, "ham_a"), ("good-c", c, "ham_c"), ("crash-d", d, "ham_d"))}
        self.finish(p0)
        results = {name: self.finish(proc) for name, proc in procs.items()}
        self.assertEqual(results["good-a"][0], 0, results["good-a"][1])
        self.assertEqual(results["good-c"][0], 0, results["good-c"][1])
        self.assertEqual(results["crash-d"][0], 1, results["crash-d"][1])
        self.assertIn("cannot be attributed to a branch: splitting", results["good-a"][1] + results["crash-d"][1]
                      + results["good-c"][1])


class MaybeRunBatchedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.work = helpers.make_repo(self.tmp)
        self.fake = os.path.join(self.tmp, "fake_test.py")
        with open(self.fake, "w") as handle:
            handle.write(FAKE_TEST_PY)
        self.env = dict(os.environ, HAMS_BATCH_TREE_DIR=os.path.join(self.tmp, "trees"), HAMS_BATCH_DIR=os.path.join(self.tmp, "b"),
                        XDG_CACHE_HOME=os.path.join(self.tmp, "cache"), FAKE_CALLS=os.path.join(self.tmp, "calls.log"))
        for name in (otb.RUNNER_ENV, "HAMS_TEST_LOCK_HELD", "HAMS_TEST_BATCH"):
            self.env.pop(name, None)
        self.lines = []

    def patched(self):
        fake = self.fake
        real = otb.OdooTestBatch

        class Fake(real):
            def __init__(self, *args, **kwargs):
                kwargs["fetch"] = False
                super().__init__(*args, **kwargs)
                self.prepare = lambda tree: None
                self.command = lambda tree, results: [sys.executable, fake, "-u", ",".join(
                    sorted({m for member in self.current_members for m in member["scope"]})), "-l",
                    os.path.join(os.path.dirname(results), "logs")]

        return Fake

    def test_ineligible_runs_return_none_and_say_why(self):
        self.assertIsNone(otb.maybe_run_batched(namespace(solo=True), self.env, self.work, out=self.lines.append))
        self.assertEqual(self.lines, [])  # --solo is a request, not worth a line
        with open(os.path.join(self.work, "dirty.txt"), "w") as handle:
            handle.write("x\n")
        self.assertIsNone(otb.maybe_run_batched(namespace(), self.env, self.work, out=self.lines.append))
        self.assertTrue(any("uncommitted" in line for line in self.lines))
        os.unlink(os.path.join(self.work, "dirty.txt"))
        self.lines.clear()
        sub = os.path.join(self.work, "sub")
        os.makedirs(sub)
        self.assertIsNone(otb.maybe_run_batched(namespace(), self.env, sub, out=self.lines.append))
        self.assertTrue(any("repository root" in line for line in self.lines))

    def test_a_committed_run_with_nothing_else_going_runs_at_once_and_passes(self):
        os.makedirs(os.path.join(self.work, "ham_a"))
        helpers.commit_branch(self.work, "feat", {"ham_a/x.py": "ok\n"})
        helpers.git(self.work, "checkout", "-q", "feat")
        original = otb.OdooTestBatch
        otb.OdooTestBatch = self.patched()
        old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            code = otb.maybe_run_batched(namespace(module="ham_a"), self.env, self.work, out=self.lines.append)
        finally:
            otb.OdooTestBatch = original
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(code, 0, self.lines)
        self.assertTrue(any("nothing is running here; starting at once" in line for line in self.lines))
        tip = helpers.git(self.work, "rev-parse", "HEAD")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "cache", "hams-test", "passed", tip)))


def drive(argv):
    spec = json.loads(argv[0])

    def record(tip, modules, batch_id):
        with open(os.path.join(spec["records"], tip), "w") as handle:
            handle.write("tests modules=%s batch=%s\n" % (",".join(modules), batch_id))

    runner = otb.OdooTestBatch(
        spec["repo"], "hams_demo", "origin/main", [], record, sdir=spec["sdir"], prepare=lambda tree: None,
        environ=dict(os.environ), fetch=False)
    runner.command = lambda tree, results: [
        sys.executable, spec["fake"], "-u", ",".join(sorted({m for member in runner.current_members
                                                           for m in member["scope"]})),
        "-l", os.path.join(os.path.dirname(results), "logs")]
    code = otb.lint_batch.run_member(spec["repo"], "hams_demo", spec["branch"], spec["tip"], "origin/main",
                                     [spec["modules"]], runner, timeout=60, poll=0.1, sdir=spec["sdir"], kind="test")
    print("RESULT " + json.dumps({"code": code}))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--drive":
        drive(sys.argv[2:])
    else:
        unittest.main()
