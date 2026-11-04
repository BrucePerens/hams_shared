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
Tests [@ANCHOR: odoo_test_batch:failing_tests]
Tests [@ANCHOR: odoo_test_batch:module_finder]
Tests [@ANCHOR: odoo_test_batch:plan_machine_split]
Tests [@ANCHOR: odoo_test_batch:run_on_machine]
Tests [@ANCHOR: odoo_test_batch:idle_machines]
Tests [@ANCHOR: odoo_test_batch:RemoteTestBatch]
Tests [@ANCHOR: odoo_test_batch:run_remote_member]
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

    def test_implicated_modules_reads_two_hosts_texts_joined(self):
        one = "these modules are implicated in the failure(s):\n  - ham_a\nlater section\n"
        two = "these modules are implicated in the failure(s):\n  - ham_b\n"
        self.assertEqual(otb.implicated_modules(one + two), ["ham_a", "ham_b"])

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


FAKE_DRIVER = r'''
import json, os, sys
a = sys.argv[1:]
machine, tree, modules = a[0], a[1], a[2].split(",")
members = json.load(open(a[a.index("--members-file") + 1]))
bad = [m for m in modules if m in os.environ.get("FAKE_BAD", "").split(",")]
with open(os.environ["FAKE_CALLS"], "a") as calls:
    calls.write("%s:%s:%s\n" % (machine, ",".join(modules), len(members)))
with open(a[a.index("--log") + 1], "w") as log:
    log.write("host log of %s\n" % machine)
text = ("these modules are implicated in the failure(s):\n" + "".join("  - %s\n" % m for m in bad)) if bad else ""
json.dump({"host": machine, "exit_code": 1 if bad else 0, "merged": [m["tip"] for m in members], "conflicted": [],
           "filtered": text, "log": a[a.index("--log") + 1], "seconds": 5.0, "error": None},
          open(a[a.index("--result-json") + 1], "w"))
'''


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.work = helpers.make_repo(self.tmp)
        self.sdir = os.path.join(self.tmp, "state")
        self.stamps = []
        self.lines = []

    def members(self, *specs):
        out = []
        for name, module in specs:
            os.makedirs(os.path.join(self.work, module), exist_ok=True)
            tip = helpers.commit_branch(self.work, name, {"%s/f_%s.py" % (module, name): "ok\n"})
            out.append({"pid": os.getpid(), "start": 1, "branch": name, "tip": tip, "scope": [module],
                        "queued_at": 0, "solo": False, "info": ""})
        return out

    def batch(self, runner, machines=("yoga", "mac1")):
        return otb.RemoteTestBatch(
            self.work, "hams_demo", "origin/main", [], lambda tip, modules, bid: self.stamps.append((tip, modules)),
            self.sdir, list(machines), ["driver"], work_base=self.tmp, probe=lambda names: list(names),
            runner=runner, out=self.lines.append)

    def test_idle_machines_skips_a_host_whose_lock_is_held(self):
        import fcntl
        with open(os.path.join(self.tmp, "yoga.lock"), "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(otb.idle_machines(["yoga", "mac1"], self.tmp), ["mac1"])
        self.assertEqual(otb.idle_machines(["yoga", "mac1"], self.tmp), ["yoga", "mac1"])

    def test_a_batch_is_split_over_the_hosts_and_each_branch_gets_its_own_verdict(self):
        members = self.members(("a", "ham_a"), ("b", "ham_b"), ("c", "ham_c"))
        seen = {}

        def runner(machine, repo, repo_name, group, mods, driver, log_path, work, union=None):
            seen[machine] = (list(mods), [m["tip"] for m in group])
            with open(log_path, "w") as log:
                log.write("log of %s\n" % machine)
            bad = [m for m in mods if m == "ham_b"]
            return {"host": machine, "exit_code": 1 if bad else 0, "merged": [], "conflicted": [], "seconds": 40.0,
                    "error": None, "log": log_path,
                    "filtered": "modules are implicated in the failure(s):\n" + "".join("  - %s\n" % m for m in bad)}

        log_path = os.path.join(self.tmp, "batch.log")
        result = self.batch(runner)(1, members, log_path)
        verdicts = result["verdicts"]
        self.assertEqual([verdicts[m["tip"]]["status"] for m in members], ["passed", "failed", "passed"])
        self.assertEqual(sorted(seen), ["mac1", "yoga"])  # both hosts got a share
        self.assertEqual(sorted(sum((v[0] for v in seen.values()), [])), ["ham_a", "ham_b", "ham_c"])
        self.assertTrue(all(len(v[1]) == 3 for v in seen.values()))  # every host merges every branch
        self.assertEqual(sorted(tip for tip, _ in self.stamps), sorted([members[0]["tip"], members[2]["tip"]]))
        self.assertIn("log of yoga", open(log_path).read())
        self.assertTrue(os.path.exists(os.path.join(self.sdir, "durations.json")))

    def test_a_clean_combined_run_marks_every_branch_passed(self):
        members = self.members(("a", "ham_a"), ("b", "ham_b"))

        def runner(machine, repo, repo_name, group, mods, driver, log_path, work, union=None):
            return {"host": machine, "exit_code": 0, "merged": [], "conflicted": [], "filtered": "", "seconds": 1.0,
                    "error": None, "log": log_path}

        verdicts = self.batch(runner)(1, members, "")["verdicts"]
        self.assertEqual({v["status"] for v in verdicts.values()}, {"passed"})
        self.assertEqual(len(self.stamps), 2)

    def test_a_host_that_cannot_start_is_exit_75_not_a_test_failure(self):
        members = self.members(("a", "ham_a"))

        def runner(machine, repo, repo_name, group, mods, driver, log_path, work, union=None):
            return {"host": machine, "exit_code": 75, "error": "ssh refused", "seconds": 0.0, "log": log_path}

        verdict = self.batch(runner)(1, members, "")["verdicts"][members[0]["tip"]]
        self.assertEqual(verdict["exit_code"], 75)
        self.assertIn("run on yoga could not start: ssh refused", verdict["detail"])
        self.assertEqual(self.stamps, [])

    def test_a_conflict_found_by_the_hosts_sends_that_branch_to_run_alone(self):
        members = self.members(("a", "ham_a"), ("b", "ham_b"))

        def runner(machine, repo, repo_name, group, mods, driver, log_path, work, union=None):
            return {"host": machine, "exit_code": 0, "conflicted": [members[1]["tip"]], "filtered": "", "seconds": 1.0,
                    "error": None, "log": log_path}

        verdicts = self.batch(runner, machines=("yoga",))(1, members, "")["verdicts"]
        self.assertEqual(verdicts[members[0]["tip"]]["status"], "passed")
        self.assertEqual(verdicts[members[1]["tip"]]["status"], "solo")

    def test_run_on_machine_calls_the_driver_and_reads_its_result(self):
        driver = os.path.join(self.tmp, "driver.py")
        with open(driver, "w") as handle:
            handle.write(FAKE_DRIVER)
        calls = os.path.join(self.tmp, "calls")
        old = os.environ.get("FAKE_CALLS")
        os.environ["FAKE_CALLS"] = calls
        try:
            members = self.members(("a", "ham_a"))
            result = otb.run_on_machine("yoga", self.work, "hams_com", members + members, ["ham_a"],
                                        [sys.executable, driver], os.path.join(self.tmp, "yoga.log"), self.tmp)
        finally:
            os.environ.pop("FAKE_CALLS") if old is None else os.environ.update(FAKE_CALLS=old)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(open(calls).read().strip(), "yoga:ham_a:1")  # the duplicate tip is sent once
        crash = otb.run_on_machine("yoga", self.work, "hams_com", members, ["ham_a"], [sys.executable, "-c", "pass"],
                                   os.path.join(self.tmp, "y.log"), self.tmp)
        self.assertEqual(crash["exit_code"], 75)
        self.assertIn("wrote no result", crash["error"])

    def test_failing_tests_reads_both_line_forms_and_counts_a_failure_once(self):
        text = ("2026-11-04 20:42:24,011 1 ERROR hams_test odoo.addons.ham_events.tests.test_member_manual_not_listed: "
                "ERROR: TestNotListedManualLinks.test_x\n"
                "2026-11-04 20:42:24,011 1 ERROR hams_test odoo.addons.ham_events.tests.test_member_manual_not_listed: "
                "ERROR: TestNotListedManualLinks.test_x\n"
                "2026-11-04 20:59:22,291 1 ERROR hams_test odoo.tests.suite: ERROR: setUpClass "
                "(odoo.addons.ham_com.tests.test_a.TestA)\n"
                "FAIL: test_y (odoo.addons.ham_b.tests.test_a.TestB)\n"
                "Odoo crashed before any test\n")
        self.assertEqual(otb.failing_tests(text), {"ham_events:TestNotListedManualLinks.test_x",
                                                   "ham_com:TestA.setUpClass", "ham_b:TestB.test_y"})
        self.assertEqual(otb.failing_tests("Odoo crashed\n"), set())

    def test_run_on_machine_installs_the_whole_union_and_tests_only_the_share(self):
        driver = os.path.join(self.tmp, "argv_driver.py")
        with open(driver, "w") as handle:
            handle.write("import json, sys\na = sys.argv[1:]\nopen(a[a.index('--result-json') + 1], 'w').write("
                         "json.dumps({'host': a[0], 'exit_code': 0, 'argv': a, 'filtered': ''}))\n")
        members = self.members(("a", "ham_a"))
        split = otb.run_on_machine("mac1", self.work, "hams_com", members, ["ham_events"], [sys.executable, driver],
                                   os.path.join(self.tmp, "m.log"), self.tmp,
                                   union=["ham_events", "ham_communications_consent", "ham_club_management"])
        argv = split["argv"]
        self.assertEqual(argv[2], "ham_club_management,ham_communications_consent,ham_events")  # all installed
        self.assertEqual(argv[argv.index("--share") + 1], "ham_events")  # one share tested
        whole = otb.run_on_machine("mac1", self.work, "hams_com", members, ["ham_a"], [sys.executable, driver],
                                   os.path.join(self.tmp, "m.log"), self.tmp, union=["ham_a"])
        self.assertNotIn("--share", whole["argv"])

    def test_splitting_never_changes_the_installed_set(self):
        # The case that broke the first real use: ham_events' tests need ham_communications_consent INSTALLED.
        # A host that tests ham_events without it errors exactly as the real hosts did.
        asked = ["ham_club_management", "ham_communications_consent", "ham_events"]
        members = self.members(("pr", asked[0]))
        members[0]["scope"] = asked
        calls = {}

        def runner(machine, repo, repo_name, group, mods, driver, log_path, work, union=None):
            calls[machine] = (list(mods), list(union or mods))
            broken = "ham_events" in mods and "ham_communications_consent" not in (union or mods)
            filtered = ("2026-11-04 20:42:24,011 1 ERROR hams_test odoo.addons.ham_events.tests.test_listing: ERROR: "
                        "TestAdd.test_x\n") if broken else ""
            return {"host": machine, "exit_code": 1 if broken else 0, "conflicted": [], "filtered": filtered,
                    "seconds": 5.0, "error": None, "log": log_path}

        verdict = self.batch(runner)(1, members, "")["verdicts"][members[0]["tip"]]
        self.assertEqual(verdict["status"], "passed", verdict)
        self.assertEqual(sorted(calls), ["mac1", "yoga"])  # it WAS split over both hosts
        self.assertEqual(sorted(sum((c[0] for c in calls.values()), [])), asked)  # the tests are divided
        self.assertTrue(all(c[1] == asked for c in calls.values()), calls)  # every host installs all three

    def test_a_failure_identical_on_origin_main_is_environmental_and_not_charged_to_a_branch(self):
        members = self.members(("a", "ham_a"), ("b", "ham_b"))
        line = "20:42 1 ERROR hams_test odoo.addons.%s.tests.test_x: ERROR: Test%s.test_env\n"

        def runner(machine, repo, repo_name, group, mods, driver, log_path, work, union=None):
            return {"host": machine, "exit_code": 1, "conflicted": [], "seconds": 1.0, "error": None, "log": log_path,
                    "filtered": line % ("ham_a", "A"), "baseline_exit": 1, "baseline_filtered": line % ("ham_a", "A")}

        result = self.batch(runner, machines=("yoga",))(1, members, "")
        verdicts = result["verdicts"]
        self.assertEqual({v["status"] for v in verdicts.values()}, {"passed"})
        self.assertIn("identically on origin/main", verdicts[members[0]["tip"]]["note"])
        self.assertEqual(len(self.stamps), 2)

    def test_a_new_failure_next_to_an_environmental_one_is_still_charged_to_its_branch(self):
        members = self.members(("a", "ham_a"), ("b", "ham_b"))
        line = "20:42 1 ERROR hams_test odoo.addons.%s.tests.test_x: ERROR: Test%s.test_%s\n"

        def runner(machine, repo, repo_name, group, mods, driver, log_path, work, union=None):
            return {"host": machine, "exit_code": 1, "conflicted": [], "seconds": 1.0, "error": None, "log": log_path,
                    "filtered": line % ("ham_a", "A", "env") + line % ("ham_b", "B", "mine"),
                    "baseline_exit": 1, "baseline_filtered": line % ("ham_a", "A", "env")}

        verdicts = self.batch(runner, machines=("yoga",))(1, members, "")["verdicts"]
        self.assertEqual([verdicts[m["tip"]]["status"] for m in members], ["passed", "failed"])
        self.assertTrue(any("ham_b:TestB.test_mine" in f for f in verdicts[members[1]["tip"]]["findings"]))

    def test_run_remote_member_end_to_end_with_a_stand_in_driver(self):
        driver = os.path.join(self.tmp, "driver.py")
        with open(driver, "w") as handle:
            handle.write(FAKE_DRIVER)
        env = dict(os.environ, FAKE_CALLS=os.path.join(self.tmp, "calls"), HAMS_BATCH_DIR=os.path.join(self.tmp, "b"),
                   XDG_CACHE_HOME=os.path.join(self.tmp, "cache"), HAMS_BATCH_TREE_DIR=os.path.join(self.tmp, "trees"),
                   FAKE_BAD="ham_b")
        old = dict(os.environ)
        os.environ.update(env)
        try:
            members = self.members(("a", "ham_a"), ("b", "ham_b"))
            good = otb.run_remote_member(self.work, "hams_com", "a", members[0]["tip"], "ham_a", ["yoga"],
                                         [sys.executable, driver], environ=env, out=self.lines.append)
            bad = otb.run_remote_member(self.work, "hams_com", "b", members[1]["tip"], "ham_b", ["yoga"],
                                        [sys.executable, driver], environ=env, out=self.lines.append)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual((good, bad), (0, 1), self.lines)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "cache", "hams-test", "passed", members[0]["tip"])))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cache", "hams-test", "passed", members[1]["tip"])))
        with self.assertRaises(ValueError):
            otb.run_remote_member(self.work, "hams_com", "a", members[0]["tip"], "ham_a:TestX", ["yoga"], [], environ=env)


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
