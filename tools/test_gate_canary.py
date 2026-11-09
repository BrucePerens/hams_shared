#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for gate_canary.py (ADR 0118): status aggregation, the baseline-format output, the bisection of a new failure, the lock,
the production refusal and the systemd unit files. The snapshot and the shard commands are replaced by fakes.

Tests [@ANCHOR: gate_canary:read_shard_results]
Tests [@ANCHOR: gate_canary:aggregate]
Tests [@ANCHOR: gate_canary:baseline_candidates]
Tests [@ANCHOR: gate_canary:note_lines]
Tests [@ANCHOR: gate_canary:failure_tags]
Tests [@ANCHOR: gate_canary:bisect_command]
Tests [@ANCHOR: gate_canary:choose_jobs]
Tests [@ANCHOR: gate_canary:shell_runner]
Tests [@ANCHOR: gate_canary:run_canary]
Tests [@ANCHOR: gate_canary:main]
"""

import configparser
import fcntl
import json
import os
import tempfile
import unittest
import unittest.mock

import gate_canary as gc
import gate_snapshot
import known_red

HERE = os.path.dirname(os.path.abspath(__file__))
COMMITS = {"com": "c" * 40, "open": "o" * 40, "shared": "s" * 40}


def write_results(directory, new=(), known=None, flakes=None, unparsed=0, name="test_results.json"):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "w") as handle:
        json.dump({"version": 1, "commit": COMMITS["com"], "new_failures": list(new), "known_red": known or {},
                   "flakes": flakes or {}, "now_passing": [], "fix_me": [], "unparsed": unparsed,
                   "counts": {"failed": len(new), "errors": 0, "total": 10}}, handle)


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # Tests [@ANCHOR: gate_canary:read_shard_results]
    def test_results_of_slot_files_are_merged_and_none_means_no_file(self):
        directory = os.path.join(self.tmp.name, "s")
        self.assertIsNone(gc.read_shard_results(directory))
        write_results(directory, new=["a:B.c"])
        write_results(directory, new=["d:E.f"], flakes={"g:H.i": {"count": 1}}, name="test_results.slot-2.json")
        merged = gc.read_shard_results(directory)
        self.assertEqual(merged["new_failures"], ["a:B.c", "d:E.f"])
        self.assertEqual(merged["counts"]["total"], 20)
        with open(os.path.join(directory, "test_results.slot-3.json"), "w") as handle:
            handle.write("[]")
        with self.assertRaises(gc.CanaryError):
            gc.read_shard_results(directory)

    # Tests [@ANCHOR: gate_canary:aggregate]
    def test_status_lists_new_tolerated_and_flaky_tests_and_is_red_only_for_new_ones(self):
        shards = [
            {"number": 1, "modules": ["a"], "exit_status": 0, "results": None},
            {"number": 2, "modules": ["b"], "exit_status": 0, "results": {
                "new_failures": [], "known_red": {"b:T.t": {}}, "flakes": {"b:T.f": {}}, "now_passing": ["b:T.g"], "fix_me": [],
                "unparsed": 0, "counts": {"failed": 1, "errors": 0, "total": 9}}},
        ]
        status = gc.aggregate(shards, COMMITS, now="2026-11-09T00:00:00Z")
        self.assertEqual(status["status"], "green")
        self.assertEqual((status["commit"], status["time"]), (COMMITS["com"], "2026-11-09T00:00:00Z"))
        self.assertEqual((status["tolerated"], status["flakes"], status["now_passing"]), (["b:T.t"], ["b:T.f"], ["b:T.g"]))
        shards[1]["results"]["new_failures"] = ["b:T.n"]
        self.assertEqual(gc.aggregate(shards, COMMITS)["status"], "red")

    # Tests [@ANCHOR: gate_canary:aggregate]
    def test_a_shard_that_died_without_results_or_with_unnamed_failures_is_never_green(self):
        died = gc.aggregate([{"number": 3, "exit_status": 1, "results": None}], COMMITS)
        self.assertEqual(died["status"], "red")
        self.assertIn("no verdict", died["shard_errors"][0])
        unnamed = gc.aggregate([{"number": 1, "exit_status": 1, "results": {
            "new_failures": [], "known_red": {}, "flakes": {}, "now_passing": [], "fix_me": [], "unparsed": 2,
            "counts": {"failed": 2, "errors": 0, "total": 3}}}], COMMITS)
        self.assertEqual(unnamed["status"], "red")

    # Tests [@ANCHOR: gate_canary:baseline_candidates]
    def test_candidates_use_the_baseline_format_and_load_with_the_real_loader(self):
        status = gc.aggregate([{"number": 1, "exit_status": 1, "results": {
            "new_failures": ["m:N.t"], "known_red": {"m:K.t": {}}, "flakes": {}, "now_passing": [], "fix_me": [], "unparsed": 0,
            "counts": {"failed": 2, "errors": 0, "total": 3}}}], COMMITS, now="2026-11-09T00:00:00Z")
        data = gc.baseline_candidates(status, {"m:K.t": {"owner": "bob", "reason": "box", "first_seen": "abc"}})
        path = os.path.join(self.tmp.name, "c.json")
        with open(path, "w") as handle:
            json.dump(data, handle)
        loaded = known_red.load_baseline([path])
        self.assertEqual(loaded["m:K.t"].owner, "bob")
        self.assertEqual(loaded["m:N.t"].first_seen, COMMITS["com"])
        self.assertEqual(loaded["m:N.t"].owner, "unassigned")

    # Tests [@ANCHOR: gate_canary:note_lines]
    def test_the_note_lists_every_tolerated_item_with_age_and_owner(self):
        status = gc.aggregate([{"number": 1, "exit_status": 1, "results": {
            "new_failures": ["m:N.n"], "known_red": {"m:K.k": {"owner": "bob", "reason": "box", "age_days": 2.5}}, "flakes": {"m:F.f": {}},
            "now_passing": ["m:P.p"], "fix_me": ["m:F.f"], "unparsed": 0, "counts": {"failed": 2, "errors": 0, "total": 3}}}], COMMITS)
        text = "\n".join(gc.note_lines(status, ["  lint item known red for 1.0 days"]))
        for part in ("NEW or EXPIRED failure: m:N.n", "m:K.k, known red for 2.5 days, owner bob: box", "flaky: m:F.f",
                     "FIX ME (flaky above the threshold): m:F.f", "remove from the baseline (now passing): m:P.p", "lint item known red"):
            self.assertIn(part, text)

    # Tests [@ANCHOR: gate_canary:failure_tags]
    # Tests [@ANCHOR: gate_canary:bisect_command]
    def test_failures_become_class_tags_and_the_bisect_command_extends_the_shard_command(self):
        self.assertEqual(gc.failure_tags(["b:T.x", "a:U.y", "a:U.z", "bad"]), "/a:U,/b:T")
        command = gc.bisect_command("cd /g && test.py --wait -u a,b", "/a:U", "/r.json")
        self.assertTrue(command.startswith("cd /g && test.py --wait -u a,b --bisect-modules --test-tags /a:U"))
        self.assertTrue(command.endswith("--bisect-report /r.json"))

    # Tests [@ANCHOR: gate_canary:choose_jobs]
    def test_jobs_rise_only_when_idle_and_never_beyond_slots_or_shards(self):
        self.assertEqual(gc.choose_jobs(1, 2, 8, 4, load=10.0), 1)
        self.assertEqual(gc.choose_jobs(1, 2, 8, 4, load=0.5), 2)
        self.assertEqual(gc.choose_jobs(1, 4, 8, 3, load=0.5), 3)
        self.assertEqual(gc.choose_jobs(2, 4, 1, 4, load=0.5), 1)

    # Tests [@ANCHOR: gate_canary:shell_runner]
    def test_the_shell_runner_returns_the_exit_status_and_logs_the_output(self):
        log = os.path.join(self.tmp.name, "l.log")
        self.assertEqual(gc.shell_runner("echo hi; exit 3", log), 3)
        with open(log) as handle:
            self.assertEqual(handle.read(), "hi\n")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.gate = os.path.join(self.tmp.name, "gate")
        for shard in (1, 2):
            os.makedirs(os.path.join(self.gate, "logs", "shard%d" % shard))
        self.commands = []
        self.out = []
        patcher = unittest.mock.patch.object(gate_snapshot, "verify_snapshot", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def snapshot(self, *args, **kwargs):
        return {"trees": {"parent": self.gate, "open": self.gate + "/hams_open", "com_commit": COMMITS["com"],
                          "open_commit": COMMITS["open"], "shared_commit": COMMITS["shared"]},
                "shards": [["mod_a"], ["mod_b"]], "commands": ["run shard 1 -u mod_a", "run shard 2 -u mod_b"], "cargo_target_dir": "/c"}

    def runner_for(self, results):
        def runner(command, log_path):
            self.commands.append(command)
            if "--bisect-modules" in command:
                return 0
            number = int(command.split("shard ")[1][0])
            if number in results:
                results[number](os.path.join(self.gate, "logs", "shard%d" % number))
                return 1
            return 0
        return runner

    def run_canary(self, results, **kwargs):
        kwargs.setdefault("slots", 2)
        kwargs.setdefault("load", 99.0)
        return gc.run_canary(state_dir=self.state, runner=self.runner_for(results), snapshot=self.snapshot, out=self.out.append,
                             hostname="devbox", **kwargs)

    # Tests [@ANCHOR: gate_canary:run_canary]
    def test_a_clean_night_writes_a_green_status_and_does_not_bisect(self):
        code, status = self.run_canary({})
        self.assertEqual((code, status["status"]), (0, "green"))
        with open(os.path.join(self.state, "status.json")) as handle:
            written = json.load(handle)
        for key in ("commit", "time", "new_failures", "tolerated", "flakes"):
            self.assertIn(key, written)
        self.assertEqual(len(self.commands), 2)
        with open(os.path.join(self.state, "note.txt")) as handle:
            self.assertIn("canary green", handle.read())

    # Tests [@ANCHOR: gate_canary:run_canary]
    def test_a_new_failure_is_recorded_and_bisected_on_its_shard_only(self):
        code, status = self.run_canary({2: lambda d: write_results(d, new=["mod_b:TestX.test_y"], known={"mod_a:K.k": {"owner": "al", "reason": "r", "first_seen": "f"}})})
        self.assertEqual((code, status["status"]), (1, "red"))
        self.assertEqual(status["new_failures"], ["mod_b:TestX.test_y"])
        bisects = [c for c in self.commands if "--bisect-modules" in c]
        self.assertEqual(len(bisects), 1)
        self.assertIn("-u mod_b", bisects[0])
        self.assertIn("--test-tags /mod_b:TestX", bisects[0])
        self.assertEqual(status["bisect"]["2"]["tags"], "/mod_b:TestX")
        with open(os.path.join(self.state, "known_red_candidates.json")) as handle:
            ids = [e["test_id"] for e in json.load(handle)["entries"]]
        self.assertEqual(ids, ["mod_a:K.k", "mod_b:TestX.test_y"])

    # Tests [@ANCHOR: gate_canary:run_canary]
    def test_no_bisect_skips_it_and_tolerated_and_flaky_tests_keep_the_night_green(self):
        code, status = self.run_canary({1: lambda d: write_results(d, known={"mod_a:K.k": {}}, flakes={"mod_a:F.f": {"count": 2}})}, bisect=False)
        self.assertEqual((code, status["tolerated"], status["flakes"]), (0, ["mod_a:K.k"], ["mod_a:F.f"]))
        code, status = self.run_canary({1: lambda d: write_results(d, new=["mod_a:N.n"])}, bisect=False)
        self.assertEqual(code, 1)
        self.assertFalse([c for c in self.commands if "--bisect-modules" in c])

    # Tests [@ANCHOR: gate_canary:run_canary]
    def test_the_second_run_while_one_holds_the_lock_exits_at_once(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "canary.lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code, status = self.run_canary({})
        self.assertEqual((code, status), (3, None))
        self.assertEqual(self.commands, [])

    # Tests [@ANCHOR: gate_canary:run_canary]
    def test_the_production_host_a_stale_snapshot_and_a_stale_tree_during_the_run_are_refused(self):
        code, _ = gc.run_canary(state_dir=self.state, runner=self.runner_for({}), snapshot=self.snapshot, out=self.out.append, hostname="hams1")
        self.assertEqual(code, 2)

        def stale(*a, **k):
            raise gate_snapshot.SnapshotError("hams_open is behind origin/main")

        code, _ = gc.run_canary(state_dir=self.state, runner=self.runner_for({}), snapshot=stale, out=self.out.append, hostname="devbox")
        self.assertEqual(code, 2)
        self.assertTrue(any("behind origin/main" in line for line in self.out))
        with unittest.mock.patch.object(gate_snapshot, "verify_snapshot", return_value=["libcloudflared.so is older than main.go"]):
            code, status = self.run_canary({})
        self.assertEqual(code, 1)
        self.assertIn("older than main.go", status["shard_errors"][0])
        self.assertEqual(self.commands, [])

    # Tests [@ANCHOR: gate_canary:run_canary]
    def test_only_if_new_commit_skips_an_unchanged_origin(self):
        self.run_canary({})
        self.commands.clear()
        with unittest.mock.patch.object(gate_snapshot, "origin_main", side_effect=lambda repo, fetch=True: COMMITS["com"] if repo.endswith("hams_com") else COMMITS["open"]):
            code, status = self.run_canary({}, only_if_new_commit=True)
        self.assertEqual((code, status, self.commands), (0, None, []))

    # Tests [@ANCHOR: gate_canary:main]
    def test_main_prints_the_status_and_reports_a_missing_one(self):
        out = []
        self.assertEqual(gc.main(["status", "--state-dir", self.state], out.append), 1)
        self.run_canary({})
        self.assertEqual(gc.main(["status", "--state-dir", self.state], out.append), 0)
        self.assertIn('"status": "green"', out[-1])


class UnitFileTests(unittest.TestCase):
    # Tests [@ANCHOR: gate_canary:run_canary]
    def test_the_user_units_run_the_canary_niced_and_are_not_installed_or_enabled(self):
        service = configparser.ConfigParser(strict=False, interpolation=None)
        service.optionxform = str
        service.read(os.path.join(HERE, "hams-gate-canary.service"))
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertEqual(service["Service"]["Nice"], "10")
        self.assertIn("gate_canary.py run", service["Service"]["ExecStart"])
        self.assertIn("--only-if-new-commit", service["Service"]["ExecStart"])
        timer = configparser.ConfigParser(strict=False, interpolation=None)
        timer.optionxform = str
        timer.read(os.path.join(HERE, "hams-gate-canary.timer"))
        self.assertIn("OnCalendar", timer["Timer"])
        self.assertTrue(timer["Timer"]["Persistent"] in ("true", "false"))
        with open(os.path.join(HERE, "hams-gate-canary.service")) as handle:
            self.assertIn("NOT enabled", handle.read())


if __name__ == "__main__":
    unittest.main()
