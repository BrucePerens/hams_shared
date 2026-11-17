#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for batch_queue.py, the coalescing queue behind the shared lint and test runs (ADR 0115).

No real linter and no machine lock: the state machine is driven with fake process liveness, the runner is a
function, and the git helpers run against throw-away repositories in a temporary directory.

Tests [@ANCHOR: batch_queue:Member]
Tests [@ANCHOR: batch_queue:BatchState]
Tests [@ANCHOR: batch_queue:repo_key]
Tests [@ANCHOR: batch_queue:state_dir]
Tests [@ANCHOR: batch_queue:locked]
Tests [@ANCHOR: batch_queue:prune]
Tests [@ANCHOR: batch_queue:running_batches]
Tests [@ANCHOR: batch_queue:caps_from_env]
Tests [@ANCHOR: batch_queue:join]
Tests [@ANCHOR: batch_queue:leave]
Tests [@ANCHOR: batch_queue:seal]
Tests [@ANCHOR: batch_queue:finish]
Tests [@ANCHOR: batch_queue:resolve_max_running]
Tests [@ANCHOR: batch_queue:write_verdicts]
Tests [@ANCHOR: batch_queue:read_verdicts]
Tests [@ANCHOR: batch_queue:coalesce]
Tests [@ANCHOR: batch_queue:tree_parent]
Tests [@ANCHOR: batch_queue:build_combined_tree]
Tests [@ANCHOR: batch_queue:remove_tree]
Tests [@ANCHOR: batch_queue:reap_orphan_trees]
Tests [@ANCHOR: batch_queue:RunResult]
Tests [@ANCHOR: batch_queue:Verdict]
Tests [@ANCHOR: batch_queue:blame_by_tokens]
Tests [@ANCHOR: batch_queue:settle]
Tests [@ANCHOR: batch_queue:other_tips]
Tests [@ANCHOR: batch_queue:format_verdicts]
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import batch_queue as bq  # noqa: E402
import lock_queue  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                           "-C", cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(root):
    """A bare origin plus a main checkout with one commit on main; returns the checkout path."""
    origin = os.path.join(root, "origin.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
    work = os.path.join(root, "hams_demo")
    subprocess.run(["git", "clone", "-q", origin, work], check=True, capture_output=True)
    git(work, "checkout", "-q", "-b", "main")
    for name, text in (("a.txt", "a\n"), ("b.txt", "b\n"), ("shared.txt", "1\n2\n3\n4\n5\n6\n7\n")):
        with open(os.path.join(work, name), "w") as handle:
            handle.write(text)
    git(work, "add", "a.txt", "b.txt", "shared.txt")
    git(work, "commit", "-q", "-m", "init")
    git(work, "push", "-q", "origin", "main")
    return work


def commit_branch(work, branch, files):
    git(work, "checkout", "-q", "-B", branch, "origin/main")
    for name, text in files.items():
        with open(os.path.join(work, name), "w") as handle:
            handle.write(text)
        git(work, "add", name)
    git(work, "commit", "-q", "-m", branch)
    tip = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-q", "main")
    return tip


def fake_member(n, tip=None, branch=None, **kw):
    return bq.make_member(branch or "br%d" % n, tip or ("t%d" % n) * 10, pid=1000 + n, start=5000 + n, **kw)


ALIVE = set()


def live(pid, start):
    return (pid, start) in ALIVE or pid == os.getpid()


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        ALIVE.clear()
        self.caps = {"max_branches": 3, "max_seconds": 100.0}

    def test_member_fields(self):
        self.assertNotIn("install", bq.make_member("b", "abc", scope=["x"]))  # old readers never see the key
        withinst = bq.make_member("b", "abc", scope=["x"], install=["z", "x", "z"])
        self.assertEqual(withinst["install"], ["z"])  # sorted, de-duplicated, and not what is already tested
        member = bq.make_member("b", "abc", scope=["y", "x", "x"], solo=True)
        self.assertEqual(member["pid"], os.getpid())
        self.assertEqual(member["scope"], ["x", "y"])
        self.assertTrue(member["solo"])
        self.assertEqual(member["start"], lock_queue.proc_start_time(os.getpid()))

    def test_empty_state_shape(self):
        state = bq.empty_state()
        self.assertEqual((state["forming"], state["running"], state["next_id"]), (None, None, 1))

    def test_repo_key_is_shared_by_worktrees_and_distinct_per_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "one"))
            os.makedirs(os.path.join(tmp, "two"))
            one = make_repo(os.path.join(tmp, "one"))
            two = make_repo(os.path.join(tmp, "two"))
            wt = os.path.join(tmp, "wt")
            git(one, "worktree", "add", "-q", "--detach", wt, "origin/main")
            self.assertEqual(bq.repo_key(wt), bq.repo_key(one))
            self.assertNotEqual(bq.repo_key(one), bq.repo_key(two))
            self.assertEqual(bq.repo_key(os.path.join(tmp, "nogit")), os.path.realpath(os.path.join(tmp, "nogit")))

    def test_repo_key_of_a_submodule_is_its_own_git_directory_and_shared_by_its_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sup"))
            os.makedirs(os.path.join(tmp, "sub"))
            sup = make_repo(os.path.join(tmp, "sup"))
            sub = make_repo(os.path.join(tmp, "sub"))
            git(sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "hams_shared")
            inside = os.path.join(sup, "hams_shared")
            module_dir = os.path.realpath(os.path.join(sup, ".git", "modules", "hams_shared"))
            self.assertEqual(bq.repo_key(inside), module_dir)  # not `.git/modules`, which every submodule would share
            wt = os.path.join(tmp, "wt")
            git(inside, "worktree", "add", "-q", "--detach", wt, "HEAD")
            self.assertEqual(bq.repo_key(wt), module_dir)
            self.assertNotEqual(bq.repo_key(sup), module_dir)

    def test_state_dir_per_kind_and_repo(self):
        env = {"HAMS_BATCH_DIR": "/x/b"}
        self.assertEqual(bq.state_dir("lint", "/srv/a/hams_com", env), "/x/b/lint-srv_a_hams_com")
        self.assertNotEqual(bq.state_dir("test", "/srv/a/hams_com", env), bq.state_dir("lint", "/srv/a/hams_com", env))
        self.assertTrue(bq.state_dir("lint", "/k", {"XDG_CACHE_HOME": "/c"}).startswith("/c/hams-lint/batches/"))

    def test_locked_persists_and_exception_does_not_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            with bq.locked(tmp) as state:
                state["next_id"] = 9
            with bq.locked(tmp) as state:
                self.assertEqual(state["next_id"], 9)
            with self.assertRaises(RuntimeError):
                with bq.locked(tmp) as state:
                    state["next_id"] = 99
                    raise RuntimeError("boom")
            with bq.locked(tmp) as state:
                self.assertEqual(state["next_id"], 9)
            with open(os.path.join(tmp, "state.json"), "w") as handle:
                handle.write("{not json")
            with bq.locked(tmp) as state:
                self.assertEqual(state["next_id"], 1)  # unreadable state starts over

    def test_locked_does_not_rewrite_an_unchanged_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with bq.locked(tmp) as state:
                state["next_id"] = 3
            path = os.path.join(tmp, "state.json")
            os.utime(path, (1.0, 1.0))
            with bq.locked(tmp):
                pass
            self.assertEqual(os.path.getmtime(path), 1.0)
            with bq.locked(tmp) as state:
                state["next_id"] = 4
            self.assertGreater(os.path.getmtime(path), 1.0)

    def test_caps_from_env(self):
        self.assertEqual(bq.caps_from_env({}), {"max_branches": 8, "max_seconds": 1800.0, "max_running": 1})
        self.assertEqual(bq.caps_from_env({"HAMS_BATCH_MAX_BRANCHES": "3", "HAMS_BATCH_MAX_SECONDS": "0"}),
                         {"max_branches": 3, "max_seconds": 0.0, "max_running": 1})
        bad = bq.caps_from_env({"HAMS_BATCH_MAX_BRANCHES": "x", "HAMS_BATCH_MAX_SECONDS": "nan"})
        self.assertEqual(bad, {"max_branches": 8, "max_seconds": 1800.0, "max_running": 1})
        self.assertEqual(bq.caps_from_env({"HAMS_BATCH_MAX_BRANCHES": "0"})["max_branches"], 1)

    def test_join_creates_then_joins_and_dedupes(self):
        state = bq.empty_state()
        a, b = fake_member(1), fake_member(2)
        self.assertEqual(bq.join(state, a, self.caps), 1)
        self.assertEqual(bq.join(state, b, self.caps), 1)
        self.assertEqual(bq.join(state, a, self.caps), 1)  # same process again: not duplicated
        self.assertEqual(len(state["forming"]["members"]), 2)
        twin = fake_member(3, tip=a["tip"])  # another caller for the same tip shares the slot
        self.assertEqual(bq.join(state, twin, self.caps), 1)
        self.assertEqual(len(state["forming"]["members"]), 3)
        self.assertEqual(state["next_id"], 2)

    def test_join_cap_on_branches_not_callers(self):
        state = bq.empty_state()
        for n in (1, 2, 3):
            self.assertEqual(bq.join(state, fake_member(n), self.caps), 1)
        self.assertIsNone(bq.join(state, fake_member(4), self.caps))  # 4th branch: full
        self.assertEqual(bq.join(state, fake_member(5, tip=fake_member(1)["tip"]), self.caps), 1)

    def test_join_cap_on_estimated_seconds(self):
        state = bq.empty_state()
        first = fake_member(1, scope=["s1"])
        second = fake_member(2, scope=["s2"])
        est = lambda scope: 60.0 * len(scope)  # noqa: E731
        self.assertEqual(bq.join(state, first, self.caps, estimate=est), 1)  # the first is always accepted
        self.assertIsNone(bq.join(state, second, self.caps, estimate=est))  # 120 s > 100 s
        self.assertEqual(bq.join(state, second, dict(self.caps, max_seconds=0), estimate=est), 1)  # 0 = no cap

    def test_join_solo_rules(self):
        state = bq.empty_state()
        self.assertEqual(bq.join(state, fake_member(1, solo=True), self.caps), 1)
        self.assertIsNone(bq.join(state, fake_member(2), self.caps))
        state = bq.empty_state()
        bq.join(state, fake_member(1), self.caps)
        self.assertIsNone(bq.join(state, fake_member(2, solo=True), self.caps))

    def test_prune_drops_dead_members_and_dead_runner(self):
        state = bq.empty_state()
        a, b = fake_member(1), fake_member(2)
        bq.join(state, a, self.caps)
        bq.join(state, b, self.caps)
        ALIVE.update({(b["pid"], b["start"])})
        self.assertEqual(bq.prune(state, live), [])
        self.assertEqual([m["tip"] for m in state["forming"]["members"]], [b["tip"]])
        state["running"] = {"id": 7, "members": [], "runner": {"pid": 1, "start": 1}}
        self.assertEqual(bq.prune(state, live), [("runner_died", 7)])
        self.assertIsNone(state["running"])
        ALIVE.clear()
        bq.prune(state, live)
        self.assertIsNone(state["forming"])

    def test_seal_only_leader_and_only_when_nothing_runs(self):
        state = bq.empty_state()
        a, b = fake_member(1), fake_member(2)
        bq.join(state, a, self.caps)
        bq.join(state, b, self.caps)
        self.assertIsNone(bq.seal(state, b))  # not the oldest
        running = bq.seal(state, a, log="/l")
        self.assertEqual((running["id"], running["runner"]["pid"], running["log"]), (1, a["pid"], "/l"))
        self.assertIsNone(state["forming"])
        bq.join(state, b, self.caps)
        self.assertIsNone(bq.seal(state, b))  # a batch is running
        bq.finish(state, 99)
        self.assertIsNotNone(state["running"])
        bq.finish(state, 1)
        self.assertIsNone(state["running"])
        self.assertIsNotNone(bq.seal(state, b))

    def test_leave_removes_member_and_empty_batch(self):
        state = bq.empty_state()
        a, b = fake_member(1), fake_member(2)
        bq.join(state, a, self.caps)
        bq.join(state, b, self.caps)
        bq.leave(state, a)
        self.assertEqual(len(state["forming"]["members"]), 1)
        bq.leave(state, b)
        self.assertIsNone(state["forming"])

    def test_verdict_files_roundtrip_and_old_ones_are_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(bq.read_verdicts(tmp, 1))
            bq.write_verdicts(tmp, 1, {"verdicts": {"t": {"status": "passed"}}}, now=lambda: 1000.0)
            self.assertEqual(bq.read_verdicts(tmp, 1)["verdicts"]["t"]["status"], "passed")
            self.assertEqual(bq.read_verdicts(tmp, 1)["batch"], 1)
            old = os.path.join(tmp, "verdicts", "1.json")
            os.utime(old, (1.0, 1.0))
            bq.write_verdicts(tmp, 2, {"verdicts": {}}, now=time.time)
            self.assertFalse(os.path.exists(old))


class ConcurrentBatchStateTests(StateMachineTests):
    """Several batches running at once (up to caps["max_running"]); the state stays readable by a leader that only knows
    the "running" key. Runs StateMachineTests' own tests too, which pins that max_running of 1 changed nothing."""

    def sealed(self, state, n, max_running):
        member = fake_member(n)
        ALIVE.add((member["pid"], member["start"]))
        self.assertIsNotNone(bq.join(state, member, self.caps))
        return bq.seal(state, member, max_running=max_running)

    def test_a_second_batch_seals_while_one_runs_up_to_the_limit_and_not_beyond(self):
        state = bq.empty_state()
        first = self.sealed(state, 1, 2)
        second = self.sealed(state, 2, 2)
        self.assertEqual([b["id"] for b in bq.running_batches(state)], [first["id"], second["id"]])
        self.assertEqual(state["running"]["id"], first["id"], "the first running batch stays under the older key")
        self.assertEqual([b["id"] for b in state["running_more"]], [second["id"]])
        third = fake_member(3)
        ALIVE.add((third["pid"], third["start"]))
        bq.join(state, third, self.caps)
        self.assertIsNone(bq.seal(state, third, max_running=2), "both running slots are busy: it waits in the forming batch")
        self.assertIsNotNone(state["forming"])
        self.assertIsNotNone(bq.seal(state, third, max_running=3))

    def test_the_running_limit_may_be_a_function_that_grows_and_shrinks(self):
        self.assertEqual(bq.resolve_max_running({"max_running": 3}), 3)
        self.assertEqual(bq.resolve_max_running({"max_running": 0}), 1, "never below one")
        room = [6]
        self.assertEqual(bq.resolve_max_running({"max_running": lambda: room[0]}), 6)
        room[0] = 4
        self.assertEqual(bq.resolve_max_running({"max_running": lambda: room[0]}), 4)
        self.assertEqual(bq.resolve_max_running({"max_running": lambda: "x"}), 1, "a bad value is one running batch")
        state = bq.empty_state()
        self.sealed(state, 1, bq.resolve_max_running({"max_running": lambda: 2}))
        self.assertIsNotNone(self.sealed(state, 2, bq.resolve_max_running({"max_running": lambda: 2})))

    def test_finish_frees_only_its_own_slot_and_promotes_the_rest(self):
        state = bq.empty_state()
        first, second = self.sealed(state, 1, 3), self.sealed(state, 2, 3)
        bq.finish(state, first["id"])
        self.assertEqual([b["id"] for b in bq.running_batches(state)], [second["id"]])
        self.assertEqual(state["running"]["id"], second["id"])
        self.assertEqual(state["running_more"], [])
        bq.finish(state, 999)
        self.assertEqual(len(bq.running_batches(state)), 1)

    def test_prune_frees_just_the_dead_runner(self):
        state = bq.empty_state()
        first, second = self.sealed(state, 1, 3), self.sealed(state, 2, 3)
        ALIVE.discard((1001, 5001))
        events = bq.prune(state, live)
        self.assertEqual(events, [("runner_died", first["id"])])
        self.assertEqual([b["id"] for b in bq.running_batches(state)], [second["id"]])

    def test_a_state_file_written_before_concurrent_batches_loads_and_runs_as_one(self):
        old = {"version": bq.STATE_VERSION, "next_id": 5, "forming": None,
               "running": {"id": 4, "members": [fake_member(1)], "solo": False,
                           "runner": {"pid": 1001, "start": 5001}, "started": 1.0, "log": ""}}
        ALIVE.add((1001, 5001))
        self.assertEqual([b["id"] for b in bq.running_batches(old)], [4])
        member = fake_member(2)
        ALIVE.add((member["pid"], member["start"]))
        bq.join(old, member, self.caps)
        self.assertIsNone(bq.seal(old, member), "with the default limit of one, the old running batch blocks it")

    def test_caps_from_env_reads_the_running_limit(self):
        self.assertEqual(bq.caps_from_env({"HAMS_BATCH_MAX_RUNNING": "4"})["max_running"], 4)
        self.assertEqual(bq.caps_from_env({"HAMS_BATCH_MAX_RUNNING": "0"})["max_running"], 1)
        self.assertEqual(bq.caps_from_env({"HAMS_BATCH_MAX_RUNNING": "many"})["max_running"], 1)


class CoalesceTests(unittest.TestCase):
    """Real threads with fake identities: the leader's runner blocks until released."""

    def setUp(self):
        ALIVE.clear()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.caps = {"max_branches": 8, "max_seconds": 0}
        self.lines = []

    def start(self, n, runner, **kw):
        member = fake_member(n)
        ALIVE.add((member["pid"], member["start"]))
        box = {}

        def work():
            box["out"] = bq.coalesce(self.tmp, member, runner, caps=self.caps, poll=0.01, handle_signals=False,
                                     live=live, out=self.lines.append, **kw)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread, box

    def wait_for(self, predicate, what):
        # progress-based: no wall-clock deadline; a hung wait is caught by the test runner's own timeout
        while not predicate():
            time.sleep(0.01)

    def test_requests_during_a_run_share_the_next_run(self):
        release = threading.Event()
        calls = []

        def runner(batch_id, members, log):
            calls.append((batch_id, sorted(m["branch"] for m in members)))
            if batch_id == 1:
                release.wait(10)
            return {"verdicts": {m["tip"]: {"status": "passed", "exit_code": 0} for m in members}, "log": log}

        first, box1 = self.start(1, runner)
        self.wait_for(lambda: calls, "batch 1 to start")
        others = [self.start(n, runner) for n in (2, 3, 4)]
        self.wait_for(lambda: len([m for line in self.lines if "joined batch 2" in line for m in [line]]) == 3,
                      "three joins of batch 2")
        self.assertEqual(calls, [(1, ["br1"])])  # nothing ran for them yet
        release.set()
        first.join(10)
        for thread, _box in others:
            thread.join(10)
        self.assertEqual(calls, [(1, ["br1"]), (2, ["br2", "br3", "br4"])])  # ONE run for three requests
        self.assertEqual(box1["out"]["status"], "passed")
        self.assertEqual(box1["out"]["doc"]["batch"], 1)  # the leader's own document is complete too
        self.assertEqual([m["branch"] for m in box1["out"]["doc"]["members"]], ["br1"])
        for _thread, box in others:
            self.assertEqual(box["out"]["status"], "passed")
            self.assertEqual(box["out"]["batch"], 2)
            self.assertEqual(len(box["out"]["others"]), 2)
        self.assertEqual(sum(1 for box in (b for _t, b in others) if box["out"]["ran"]), 1)  # one leader
        self.assertTrue(any("joined batch 2 with 2 other branches (batch 1 running" in line for line in self.lines))
        self.assertTrue(any("nothing is running here; starting at once" in line for line in self.lines))
        self.assertFalse(any("is full" in line for line in self.lines))  # joining is not being refused

    def test_with_two_running_slots_a_new_request_starts_its_own_batch_at_once_and_the_rest_coalesce(self):
        self.caps = {"max_branches": 8, "max_seconds": 0, "max_running": 2}
        release = threading.Event()
        calls = []

        def runner(batch_id, members, log):
            calls.append((batch_id, sorted(m["branch"] for m in members)))
            if batch_id in (1, 2):
                release.wait(10)
            return {"verdicts": {m["tip"]: {"status": "passed", "exit_code": 0} for m in members}, "log": log}

        first, box1 = self.start(1, runner)
        self.wait_for(lambda: len(calls) >= 1, "batch 1 to start")
        second, box2 = self.start(2, runner)
        self.wait_for(lambda: len(calls) >= 2, "batch 2 to start while batch 1 still runs")
        self.assertEqual(calls, [(1, ["br1"]), (2, ["br2"])])
        self.assertTrue(any("1 of 2 running slots are in use" in line for line in self.lines))
        later = [self.start(n, runner) for n in (3, 4)]
        self.wait_for(lambda: len([line for line in self.lines if "joined batch 3" in line]) == 2,
                      "requests 3 and 4 to join batch 3 behind the two running batches")
        self.assertEqual(calls, [(1, ["br1"]), (2, ["br2"])], "both running slots are busy: nothing else started")
        release.set()
        for thread in (first, second, *[t for t, _ in later]):
            thread.join(10)
        self.assertEqual(calls[2:], [(3, ["br3", "br4"])], "the waiting requests ran as ONE batch")
        for box in (box1, box2, *[b for _, b in later]):
            self.assertEqual(box["out"]["status"], "passed")

    def test_cap_makes_extra_branches_wait_for_the_next_batch(self):
        self.caps = {"max_branches": 2, "max_seconds": 0}
        release = threading.Event()
        calls = []

        def runner(batch_id, members, log):
            calls.append((batch_id, len(members)))
            if batch_id == 1:
                release.wait(10)
            return {"verdicts": {m["tip"]: {"status": "passed"} for m in members}, "log": log}

        first, _b = self.start(1, runner)
        self.wait_for(lambda: calls, "batch 1")
        rest = [self.start(n, runner) for n in (2, 3, 4)]
        self.wait_for(lambda: any("joined batch 2" in line for line in self.lines), "a join")
        time.sleep(0.2)
        release.set()
        first.join(10)
        for thread, _box in rest:
            thread.join(10)
        self.assertEqual(sorted(calls)[0], (1, 1))
        self.assertTrue(all(count <= 2 for _id, count in calls))
        self.assertEqual(sum(count for _id, count in calls), 4)

    def test_dead_runner_members_requeue_and_one_becomes_leader(self):
        with bq.locked(self.tmp) as state:
            state["next_id"] = 6
            state["running"] = {"id": 5, "members": [], "runner": {"pid": 424242, "start": 1}, "started": time.time()}
        ran = []

        def runner(batch_id, members, log):
            ran.append((batch_id, sorted(m["branch"] for m in members)))
            return {"verdicts": {m["tip"]: {"status": "passed"} for m in members}, "log": log}

        threads = [self.start(n, runner) for n in (1, 2)]
        for thread, _box in threads:
            thread.join(10)
        self.assertEqual(sorted(b for _id, branches in ran for b in branches), ["br1", "br2"])  # nobody hung
        self.assertTrue(all(box["out"]["status"] == "passed" for _t, box in threads))
        self.assertTrue(any("its runner died" in line for line in self.lines))

    def test_runner_that_dies_without_verdict_sends_waiters_back_to_the_queue(self):
        release = threading.Event()
        runs = []

        def runner(batch_id, members, log):
            runs.append(batch_id)
            if batch_id == 1:
                release.wait(10)
                raise RuntimeError("runner crashed")
            return {"verdicts": {m["tip"]: {"status": "passed"} for m in members}, "log": log}

        errors = []

        def crashing():
            try:
                bq.coalesce(self.tmp, fake_member(1), runner, caps=self.caps, poll=0.01, handle_signals=False,
                            live=live, out=self.lines.append)
            except RuntimeError as error:
                errors.append(error)

        ALIVE.add((fake_member(1)["pid"], fake_member(1)["start"]))
        first = threading.Thread(target=crashing, daemon=True)
        first.start()
        self.wait_for(lambda: runs, "batch 1")
        waiter, box = self.start(2, runner)
        self.wait_for(lambda: any("joined batch 2" in line for line in self.lines), "waiter joined")
        release.set()
        first.join(10)
        waiter.join(10)
        self.assertEqual(len(errors), 1)
        self.assertEqual(box["out"]["status"], "passed")  # it ran the next batch itself

    def test_dead_leader_of_the_forming_batch_does_not_hang_the_others(self):
        # Batch 1 runs; members 2 (soon dead) and 3 queue; 2 is the oldest. After 2 dies 3 leads.
        release = threading.Event()
        ran = []

        def runner(batch_id, members, log):
            ran.append((batch_id, sorted(m["branch"] for m in members)))
            if batch_id == 1:
                release.wait(10)
            return {"verdicts": {m["tip"]: {"status": "passed"} for m in members}, "log": log}

        first, _b = self.start(1, runner)
        self.wait_for(lambda: ran, "batch 1")
        dead = fake_member(2)
        with bq.locked(self.tmp) as state:
            bq.join(state, dead, self.caps)  # never alive
        third, box = self.start(3, runner)
        self.wait_for(lambda: any("joined batch" in line for line in self.lines), "join")
        release.set()
        first.join(10)
        third.join(10)
        self.assertEqual(box["out"]["status"], "passed")
        self.assertEqual(ran[-1][1], ["br3"])  # the dead oldest member did not block the batch

    def test_timeout_leaves_the_queue(self):
        with bq.locked(self.tmp) as state:
            state["running"] = {"id": 1, "members": [], "runner": {"pid": os.getpid(), "start": lock_queue.proc_start_time(os.getpid())},
                                "started": time.time()}
            state["next_id"] = 2
        member = bq.make_member("b", "x" * 10)
        result = bq.coalesce(self.tmp, member, lambda *a: None, caps=self.caps, poll=0.01, timeout=0.05,
                             handle_signals=False, out=self.lines.append)
        self.assertEqual(result["status"], bq.TIMEOUT)
        with bq.locked(self.tmp) as state:
            self.assertIsNone(state["forming"])

    def test_solo_verdict_is_reported_as_solo(self):
        def runner(batch_id, members, log):
            return {"verdicts": {m["tip"]: {"status": "solo", "detail": "conflict"} for m in members}, "log": log}

        thread, box = self.start(1, runner)
        thread.join(10)
        self.assertEqual(box["out"]["status"], bq.SOLO)


class ScriptedCoalesceTests(unittest.TestCase):
    """coalesce driven single-threaded with a fake clock and a scripted `sleep`: exact output, order and state.

    Mutation run 2026-11-17 on coalesce: 172/269 killed by the older tests, 267/269 with these. The two survivors are
    equivalent: `while True` -> `while <True>` (the same loop) and `if event == "runner_died"` -> `if True` (prune returns
    no other event)."""

    def setUp(self):
        ALIVE.clear()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.caps = {"max_branches": 8, "max_seconds": 0}
        self.lines = []
        self.t = 1000.0
        self.sleeps = []
        self.script = lambda n: None

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        self.script(len(self.sleeps))

    def hold_slot_one(self, members=()):
        """Batch 1 is running here, its runner alive (this process), started 5 s ago."""
        with bq.locked(self.tmp) as state:
            state["next_id"] = 2
            state["running"] = {"id": 1, "members": list(members), "runner": {
                "pid": os.getpid(), "start": lock_queue.proc_start_time(os.getpid())}, "started": self.t - 5}

    def call(self, n=1, runner=None, **kw):
        member = fake_member(n)
        ALIVE.add((member["pid"], member["start"]))
        args = dict(caps=self.caps, poll=1.0, report_every=10.0, handle_signals=False, live=live,
                    out=self.lines.append, now=self.now, sleep=self.sleep)
        args.update(kw)
        return bq.coalesce(self.tmp, member, runner or (lambda *a: None), **args)

    def test_a_lone_leader_runs_at_once_and_returns_its_whole_result(self):
        calls = []

        def runner(batch_id, members, log):
            calls.append((batch_id, [m["branch"] for m in members], log))
            with bq.locked(self.tmp) as state:  # while it runs, its slot is held
                self.assertEqual(state["running"]["id"], 1)
            return {"verdicts": {members[0]["tip"]: {"status": "passed", "exit_code": 0}}, "log": log}

        result = self.call(1, runner, log_for=lambda batch_id: "/logs/%d.log" % batch_id)
        self.assertEqual(calls, [(1, ["br1"], "/logs/1.log")])
        self.assertEqual(self.lines, ["[*] batch 1: nothing is running here; starting at once",
                                      "[*] batch 1 starts with 1 branch: br1"])
        self.assertEqual(self.sleeps, [])
        tip = fake_member(1)["tip"]
        self.assertEqual(result["status"], bq.PASSED)
        self.assertEqual(result["verdict"], {"status": "passed", "exit_code": 0})
        self.assertEqual((result["batch"], result["others"], result["ran"]), (1, [], True))
        self.assertEqual(sorted(result["doc"]), ["batch", "log", "members", "verdicts"])
        self.assertEqual(result["doc"]["batch"], 1)
        self.assertEqual(result["doc"]["log"], "/logs/1.log")
        self.assertEqual(result["doc"]["verdicts"], {tip: {"status": "passed", "exit_code": 0}})
        self.assertEqual([m["branch"] for m in result["doc"]["members"]], ["br1"])
        self.assertEqual(bq.read_verdicts(self.tmp, 1), dict(result["doc"], finished=1000.0))  # published, stamped by the injected clock
        with bq.locked(self.tmp) as state:
            self.assertEqual(bq.running_batches(state), [])  # the slot is free again

    def test_without_log_for_the_runner_gets_an_empty_log_path(self):
        logs = []
        self.call(1, lambda batch_id, members, log: logs.append(log) or {"verdicts": {}, "log": log})
        self.assertEqual(logs, [""])

    def test_a_runner_that_returns_nothing_or_an_unknown_status_fails_the_member_and_frees_the_slot(self):
        result = self.call(1, lambda *a: None)
        self.assertEqual(result, {"status": bq.FAILED, "verdict": None, "batch": 1, "others": [], "ran": True, "doc": None})
        self.assertIsNone(bq.read_verdicts(self.tmp, 1))  # nothing to publish
        with bq.locked(self.tmp) as state:
            self.assertEqual(bq.running_batches(state), [])
        member = fake_member(2)
        result = self.call(2, lambda b, members, log: {"verdicts": {members[0]["tip"]: {"status": "bogus"}}, "log": log})
        self.assertEqual((result["status"], result["verdict"], result["batch"]), (bq.FAILED, {"status": "bogus"}, 2))
        self.assertEqual(member["branch"], "br2")

    def test_each_known_status_is_returned_as_it_is(self):
        for n, status in enumerate((bq.PASSED, bq.FAILED, bq.SOLO), start=1):
            result = self.call(n, lambda b, members, log, s=status: {"verdicts": {members[0]["tip"]: {"status": s}}})
            self.assertEqual(result["status"], status)

    def test_a_runner_that_raises_still_frees_the_slot_and_publishes_nothing(self):
        def runner(*a):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.call(1, runner)
        with bq.locked(self.tmp) as state:
            self.assertEqual(bq.running_batches(state), [])
        self.assertIsNone(bq.read_verdicts(self.tmp, 1))

    def test_waiting_behind_a_running_batch_reports_on_the_report_interval_only(self):
        self.hold_slot_one([fake_member(9)])
        result = self.call(1, timeout=25.0)
        self.assertEqual(result, {"status": bq.TIMEOUT, "verdict": None, "batch": 2, "others": [], "ran": False, "doc": None})
        self.assertEqual(self.sleeps, [1.0] * 25)  # timeout is reached at exactly 25 s, not 24 or 26
        self.assertEqual(self.lines, [
            "[*] joined batch 2 with 0 other branches (batch 1 running for 5 s (1 branch))",
            "[*] batch 2 is forming behind batch 1 running for 5 s (1 branch)",
            "[*] batch 2 is forming behind batch 1 running for 15 s (1 branch)",
            "[*] batch 2 is forming behind batch 1 running for 25 s (1 branch)"])
        with bq.locked(self.tmp) as state:
            self.assertIsNone(state["forming"])  # a timed-out member leaves the queue

    def test_joining_a_forming_batch_names_the_other_branches_and_their_count(self):
        self.hold_slot_one([fake_member(9)])
        with bq.locked(self.tmp) as state:
            for n in (7, 8):
                ALIVE.add((fake_member(n)["pid"], fake_member(n)["start"]))
                bq.join(state, fake_member(n), self.caps)
        self.call(1, timeout=0.5)
        self.assertEqual(self.lines[0], "[*] joined batch 2 with 2 other branches (batch 1 running for 5 s (1 branch))")
        self.lines.clear()
        with bq.locked(self.tmp) as state:
            state["forming"]["members"] = state["forming"]["members"][:1]
        self.call(2, timeout=0.5)
        self.assertEqual(self.lines[0], "[*] joined batch 2 with 1 other branch (batch 1 running for 6 s (1 branch))")  # the first call ran 1 s

    def test_a_full_batch_announces_waiting_for_the_next_one_once(self):
        self.caps = {"max_branches": 1, "max_seconds": 0}
        self.hold_slot_one()
        with bq.locked(self.tmp) as state:
            ALIVE.add((fake_member(8)["pid"], fake_member(8)["start"]))
            bq.join(state, fake_member(8), self.caps)
        result = self.call(1, timeout=25.0)
        self.assertEqual(result["status"], bq.TIMEOUT)
        self.assertEqual(result["batch"], None)
        self.assertEqual(self.lines, ["[*] the forming batch is full (1 branches): waiting for the next one"])

    def test_a_member_of_the_running_batch_reports_waiting_for_it_then_returns_its_verdict(self):
        leader, me = fake_member(8), fake_member(1)
        ALIVE.update({(leader["pid"], leader["start"]), (me["pid"], me["start"])})
        self.hold_slot_one()
        with bq.locked(self.tmp) as state:
            bq.join(state, leader, self.caps)

        def script(n):
            with bq.locked(self.tmp) as state:
                if n == 1:  # batch 1 ends; the leader seals and runs batch 2, which includes us
                    bq.finish(state, 1)
                    bq.seal(state, leader, now=self.now, log="/l")
                elif n == 12:
                    batch = bq.running_batches(state)[0]
                    bq.write_verdicts(self.tmp, 2, {"verdicts": {me["tip"]: {"status": "failed", "exit_code": 3},
                                                                 leader["tip"]: {"status": "passed"}},
                                                    "log": "/l", "members": batch["members"], "batch": 2}, now=self.now)
                    bq.finish(state, 2)
        self.script = script
        result = self.call(1, timeout=100.0)
        self.assertEqual(result["status"], bq.FAILED)
        self.assertEqual(result["verdict"], {"status": "failed", "exit_code": 3})
        self.assertEqual((result["batch"], result["others"], result["ran"]), (2, ["br8"], False))
        self.assertEqual(result["doc"]["log"], "/l")
        self.assertEqual(len(self.sleeps), 12)
        self.assertEqual(self.lines[0], "[*] joined batch 2 with 1 other branch (batch 1 running for 5 s (0 branches))")
        self.assertEqual(self.lines[1:], ["[*] batch 2 is forming behind batch 1 running for 5 s (0 branches)",
                                          "[*] waiting for batch 2 running for 9 s (2 branches)"])

    def test_a_verdict_file_that_does_not_name_this_member_is_ignored(self):
        self.hold_slot_one()
        me, other = fake_member(1), fake_member(2)

        def script(n):
            if n == 1:  # a stale file of an older state, from another process, for the same batch id
                bq.write_verdicts(self.tmp, 2, {"verdicts": {me["tip"]: {"status": "passed"}}, "log": "",
                                                "members": [other], "batch": 2}, now=self.now)
            elif n == 3:
                bq.write_verdicts(self.tmp, 2, {"verdicts": {me["tip"]: {"status": "failed"}}, "log": "",
                                                "members": [me, other], "batch": 2}, now=self.now)
        self.script = script
        result = self.call(1, timeout=100.0)
        self.assertEqual((result["status"], len(self.sleeps), result["others"]), (bq.FAILED, 3, ["br2"]))

    def test_a_verdict_document_without_this_member_makes_it_join_again(self):
        self.hold_slot_one()
        me, other = fake_member(1), fake_member(2)

        def script(n):
            if n == 10:  # a report is due right now, while this member has no batch
                bq.write_verdicts(self.tmp, 2, {"verdicts": {other["tip"]: {"status": "passed"}}, "log": "",
                                                "members": [me, other], "batch": 2}, now=self.now)
            elif n == 13:
                bq.write_verdicts(self.tmp, 2, {"verdicts": {me["tip"]: {"status": "passed"}}, "log": "",
                                                "members": [me], "batch": 2}, now=self.now)
        self.script = script
        result = self.call(1, timeout=100.0)
        self.assertEqual((result["status"], len(self.sleeps), result["others"]), (bq.PASSED, 13, []))
        self.assertFalse(any("ended without a verdict" in line for line in self.lines))
        # the report due at 10 s falls while the member has no batch: it prints nothing (and does not crash on batch None)
        self.assertEqual(self.lines, [
            "[*] joined batch 2 with 0 other branches (batch 1 running for 5 s (0 branches))",
            "[*] batch 2 is forming behind batch 1 running for 5 s (0 branches)"] + [
            "[*] joined batch 2 with 0 other branches (batch 1 running for %d s (0 branches))" % age for age in (16, 17, 18)])

    def test_a_batch_that_keeps_ending_without_a_verdict_gives_up_after_the_requeue_limit(self):
        self.hold_slot_one()

        def script(n):
            with bq.locked(self.tmp) as state:
                state["forming"] = None  # the forming batch vanishes: its runner died before sealing
        self.script = script
        result = self.call(1, timeout=1000.0)
        self.assertEqual(result, {"status": bq.GAVE_UP, "verdict": None, "batch": None, "others": [], "ran": False, "doc": None})
        self.assertEqual(bq.MAX_REQUEUES, 3)
        died = [line for line in self.lines if "ended without a verdict (its runner died); joining again" in line]
        self.assertEqual(len(died), bq.MAX_REQUEUES + 1)
        self.assertEqual(died[0], "[*] batch 2 ended without a verdict (its runner died); joining again")
        self.assertEqual(len(self.sleeps), bq.MAX_REQUEUES + 1)

    def test_a_dead_runner_is_reported_before_the_member_joins_again(self):
        with bq.locked(self.tmp) as state:
            state["next_id"] = 6
            state["running"] = {"id": 5, "members": [], "runner": {"pid": 424242, "start": 1}, "started": self.t}
        result = self.call(1, lambda b, members, log: {"verdicts": {members[0]["tip"]: {"status": "passed"}}, "log": log})
        self.assertEqual(self.lines[0], "[*] batch 5: its runner died; waiting members re-queue")
        self.assertEqual((result["status"], result["batch"], result["ran"]), (bq.PASSED, 6, True))

    def test_a_follower_gets_a_failed_status_for_an_unknown_verdict_status(self):
        self.hold_slot_one()
        me = fake_member(1)
        self.script = lambda n: bq.write_verdicts(self.tmp, 2, {"verdicts": {me["tip"]: {"status": "weird"}}, "log": "",
                                                                "members": [me], "batch": 2}, now=self.now)
        result = self.call(1, timeout=100.0)
        self.assertEqual((result["status"], result["verdict"], len(self.sleeps)), (bq.FAILED, {"status": "weird"}, 1))

    def test_waiting_for_the_batch_leader_names_no_running_batch_and_is_not_the_full_message(self):
        leader, me = fake_member(8), fake_member(1)
        ALIVE.add((leader["pid"], leader["start"]))
        dead = fake_member(7)  # never alive: pruned, and the runner_died event is not printed for it
        with bq.locked(self.tmp) as state:
            bq.join(state, dead, self.caps)
            bq.join(state, leader, self.caps)
            self.assertEqual(state["forming"]["id"], 1)

        def script(n):
            if n == 3:
                bq.write_verdicts(self.tmp, 1, {"verdicts": {me["tip"]: {"status": "passed"}}, "log": "",
                                                "members": [leader, me], "batch": 1}, now=self.now)
        self.script = script
        result = self.call(1, timeout=100.0)
        self.assertEqual(result["status"], bq.PASSED)
        self.assertEqual(self.lines, ["[*] joined batch 1 with 1 other branch"])  # nothing running, nothing full, nobody died
        self.assertEqual(len(self.sleeps), 3)

    def test_a_leader_of_several_branches_names_them_all_in_order(self):
        self.hold_slot_one()
        other = fake_member(2)
        ALIVE.add((other["pid"], other["start"]))

        def script(n):
            if n == 2:
                with bq.locked(self.tmp) as state:
                    bq.join(state, other, self.caps)
                    bq.finish(state, 1)  # the slot frees: the first member of the forming batch seals it
        self.script = script
        calls = []

        def runner(batch_id, members, log):
            calls.append([m["branch"] for m in members])
            return {"verdicts": {m["tip"]: {"status": "passed"} for m in members}, "log": log}
        result = self.call(1, runner, timeout=100.0)
        self.assertEqual(calls, [["br1", "br2"]])
        self.assertEqual(self.lines[-1], "[*] batch 2 starts with 2 branches: br1, br2")
        self.assertEqual((result["status"], result["others"], result["ran"]), (bq.PASSED, ["br2"], True))

    def test_a_slot_available_while_others_run_is_announced_with_the_counts(self):
        self.caps = {"max_branches": 8, "max_seconds": 0, "max_running": 2}
        self.hold_slot_one([fake_member(9)])
        self.call(1, lambda b, members, log: {"verdicts": {}, "log": log})
        self.assertEqual(self.lines[0], "[*] batch 2: 1 of 2 running slots are in use here; starting at once")

    def test_handle_signals_defaults_on_and_off_leaves_the_handlers_alone(self):
        def sentinel(signum, _frame):
            raise AssertionError("not expected")

        old = {s: signal.signal(s, sentinel) for s in (signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in old.items()])
        self.hold_slot_one()
        seen = []

        def script(n):
            seen.append((signal.getsignal(signal.SIGTERM) is sentinel, signal.getsignal(signal.SIGHUP) is sentinel))
        self.script = script
        self.call(1, timeout=2.0, handle_signals=False)
        self.call(2, timeout=2.0, handle_signals=True)
        kw = dict(caps=self.caps, poll=1.0, timeout=2.0, live=live, out=self.lines.append, now=self.now, sleep=self.sleep)
        member = fake_member(3)
        ALIVE.add((member["pid"], member["start"]))
        bq.coalesce(self.tmp, member, lambda *a: None, **kw)  # handle_signals not given: the default
        self.assertEqual(seen, [(True, True)] * 2 + [(False, False)] * 2 + [(False, False)] * 2)

    def test_a_signal_while_waiting_leaves_the_queue_exits_128_plus_it_and_restores_the_handlers(self):
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            self.lines.clear()
            self.hold_slot_one()
            seen = {}

            def sentinel(signum, _frame):
                raise AssertionError("the sentinel handler ran: coalesce did not install its own")

            old = {s: signal.signal(s, sentinel) for s in (signal.SIGTERM, signal.SIGHUP)}
            self.addCleanup(lambda old=old: [signal.signal(s, h) for s, h in old.items()])

            def script(n, sig=sig, seen=seen):
                seen["handler"] = signal.getsignal(sig)
                signal.raise_signal(sig)
            self.script = script
            with self.assertRaises(SystemExit) as caught:
                self.call(1, handle_signals=True, timeout=1000.0)
            self.assertEqual(caught.exception.code, code)
            self.assertIsNot(seen["handler"], sentinel)
            self.assertIs(signal.getsignal(signal.SIGTERM), sentinel)
            self.assertIs(signal.getsignal(signal.SIGHUP), sentinel)
            self.assertEqual(self.sleeps[-1:], [1.0])
            with bq.locked(self.tmp) as state:
                self.assertIsNone(state["forming"])  # it left the queue

    def test_leaving_on_a_signal_survives_an_unusable_state_directory(self):
        self.hold_slot_one()

        def script(n):
            shutil.rmtree(self.tmp)
            with open(self.tmp, "w"):
                pass
        self.script = script
        self.addCleanup(lambda: os.path.isfile(self.tmp) and os.remove(self.tmp))
        old = signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGTERM, old)

        def fire(n):
            script(n)
            signal.raise_signal(signal.SIGTERM)
        self.script = fire
        with self.assertRaises(SystemExit) as caught:
            self.call(1, handle_signals=True, timeout=1000.0)
        self.assertEqual(caught.exception.code, 143)

    def test_handle_signals_off_the_main_thread_is_harmless_and_handlers_are_restored_after_a_normal_run(self):
        outcome = {}

        def work():
            outcome["result"] = self.call(1, lambda b, members, log: {"verdicts": {members[0]["tip"]: {"status": "passed"}}},
                                          handle_signals=True)
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        self.assertEqual(outcome["result"]["status"], bq.PASSED)
        before = signal.getsignal(signal.SIGTERM)
        self.call(2, lambda b, members, log: {"verdicts": {members[0]["tip"]: {"status": "passed"}}}, handle_signals=True)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.work = make_repo(self.tmp)

    def test_tree_parent_name(self):
        path = bq.tree_parent("lint", "hams_com", pid=12, start=34, suffix="_b1", environ={"HAMS_BATCH_TREE_DIR": "/t"})
        self.assertEqual(path, "/t/batch_lint_hams_com_12-34_b1")

    def test_merges_branches_and_drops_the_conflicting_one(self):
        tip1 = commit_branch(self.work, "one", {"c.txt": "c\n"})
        tip2 = commit_branch(self.work, "two", {"d.txt": "d\n", "shared.txt": "1\nTWO\n3\n4\n5\n6\n7\n"})
        tip3 = commit_branch(self.work, "three", {"shared.txt": "1\n2\n3\n4\n5\nSIX\n7\n", "e.txt": "e\n"})
        tip4 = commit_branch(self.work, "four", {"shared.txt": "1\nTWO-other\n3\n4\n5\n6\n7\n"})
        members = [bq.make_member(n, t, pid=1, start=1) for n, t in (("one", tip1), ("two", tip2), ("three", tip3), ("four", tip4))]
        dest = os.path.join(bq.tree_parent("lint", "hams_demo", environ={"HAMS_BATCH_TREE_DIR": self.tmp}), "hams_demo")
        built = bq.build_combined_tree(self.work, "origin/main", members, dest)
        self.assertEqual([m["branch"] for m in built["merged"]], ["one", "two", "three"])
        self.assertEqual([m["branch"] for _m, _t in built["conflicted"] for m in [_m]], ["four"])
        for name in ("c.txt", "d.txt", "e.txt"):
            self.assertTrue(os.path.exists(os.path.join(dest, name)), name)
        self.assertEqual(git(dest, "status", "--porcelain"), "")  # the failed merge left no mess
        bq.remove_tree(self.work, dest)
        self.assertFalse(os.path.exists(dest))
        self.assertNotIn(dest, git(self.work, "worktree", "list"))
        bq.remove_tree(self.work, dest)  # twice is fine

    def test_bad_base_reports_an_error(self):
        dest = os.path.join(self.tmp, "p", "hams_demo")
        built = bq.build_combined_tree(self.work, "no-such-ref", [], dest)
        self.assertIsNone(built["head"])
        self.assertTrue(built["error"])

    def test_reap_removes_only_trees_of_dead_creators(self):
        env = {"HAMS_BATCH_TREE_DIR": self.tmp}
        dead = os.path.join(bq.tree_parent("lint", "hams_demo", pid=999999, start=1, environ=env), "hams_demo")
        mine = os.path.join(bq.tree_parent("lint", "hams_demo", environ=env), "hams_demo")
        for dest in (dead, mine):
            self.assertIsNotNone(bq.build_combined_tree(self.work, "origin/main", [], dest)["head"])
        removed = bq.reap_orphan_trees(self.work, "lint", "hams_demo", environ=env)
        self.assertEqual(removed, [os.path.dirname(dead)])
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.exists(mine))
        bq.remove_tree(self.work, mine)


class AttributionTests(unittest.TestCase):
    OWNERS = {"a/x.py": ["TA"], "b/y.py": ["TB"], "shared.py": ["TA", "TB"]}

    @staticmethod
    def finder(line):
        return [token for token in AttributionTests.OWNERS if token in line]

    def test_blame_by_tokens_attributes_by_named_files(self):
        blamed, unattributed = bq.blame_by_tokens(
            {"flake8": "a/x.py:3: F401 unused\nother.py:1: ok", "anchors": "Feature 'm:f' is missing"},
            self.OWNERS, self.finder)
        self.assertEqual(sorted(blamed), ["TA"])
        self.assertTrue(blamed["TA"][0].startswith("flake8: a/x.py:3"))
        self.assertEqual(unattributed, ["anchors"])

    def test_blame_counts_a_line_naming_two_tokens_of_one_owner_once(self):
        owners = {"a/x.py": ["TA"], "x.py": ["TA"]}
        blamed, _ = bq.blame_by_tokens({"s": "a/x.py:1: bad"}, owners, lambda line: ["a/x.py", "x.py"])
        self.assertEqual(len(blamed["TA"]), 1)

    def test_blame_shared_file_blames_every_owner(self):
        blamed, unattributed = bq.blame_by_tokens({"s": "shared.py:1: bad"}, self.OWNERS, self.finder)
        self.assertEqual(sorted(blamed), ["TA", "TB"])
        self.assertEqual(unattributed, [])

    def run_once_factory(self, bad_tips, red_main=False, calls=None):
        def run_once(group):
            if calls is not None:
                calls.append([m["branch"] for m in group])
            failures = {}
            if red_main:
                failures["whole"] = "global failure with no file"
            for m in group:
                if m["tip"] in bad_tips:
                    failures["flake8"] = failures.get("flake8", "") + "%s/x.py:1: bad\n" % m["branch"]
            return {"exit_code": 1 if failures else 0, "failures": failures, "conflicted": []}
        return run_once

    @staticmethod
    def attribute_factory(members):
        owners = {"%s/x.py" % m["branch"]: [m["tip"]] for m in members}
        return lambda failures, live: bq.blame_by_tokens(failures, owners, lambda line: [t for t in owners if t in line])

    def members(self, n):
        return [fake_member(i + 1, branch="b%d" % (i + 1)) for i in range(n)]

    def test_settle_all_pass_in_one_run(self):
        members, calls = self.members(3), []
        verdicts = bq.settle(members, self.run_once_factory(set(), calls=calls), self.attribute_factory(members))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(v["status"] == bq.PASSED for v in verdicts.values()))

    def test_a_passed_verdict_names_the_other_tips_of_the_run_it_passed_in(self):
        members = self.members(3)
        verdicts = bq.settle(members, self.run_once_factory(set()), self.attribute_factory(members))
        for member in members:
            self.assertEqual(verdicts[member["tip"]]["with"], [m["tip"] for m in members if m is not member])
        # After a split the pass names only its own half; a twin of one tip is not "another" branch.
        twin = dict(members[0], pid=999)
        group = [members[0], twin, members[1]]
        self.assertEqual(bq._other_tips(group, members[0]), [members[1]["tip"]])
        # A branch that failed has no such field; a clean branch beside a failing one is checked with it.
        verdicts = bq.settle(members, self.run_once_factory({members[1]["tip"]}), self.attribute_factory(members))
        self.assertNotIn("with", verdicts[members[1]["tip"]])
        self.assertEqual(verdicts[members[0]["tip"]]["with"], [members[1]["tip"], members[2]["tip"]])

    def test_settle_attributed_failure_fails_only_the_owner_and_passes_the_rest_at_once(self):
        members, calls = self.members(3), []
        verdicts = bq.settle(members, self.run_once_factory({members[1]["tip"]}, calls=calls),
                             self.attribute_factory(members))
        self.assertEqual(len(calls), 1)  # no rerun: the clean branches pass immediately
        self.assertEqual([verdicts[m["tip"]]["status"] for m in members], ["passed", "failed", "passed"])
        self.assertIn("b2/x.py:1", verdicts[members[1]["tip"]]["findings"][0])

    def test_settle_unattributable_failure_splits_and_does_not_hold_back_passing_branches(self):
        members, calls = self.members(4), []

        def run_once(group):
            calls.append([m["branch"] for m in group])
            bad = any(m["branch"] == "b3" for m in group)  # b3 fails with no file named
            return {"exit_code": 1 if bad else 0, "failures": {"anchors": "no path here"} if bad else {},
                    "conflicted": []}

        verdicts = bq.settle(members, run_once, self.attribute_factory(members))
        self.assertEqual([verdicts[m["tip"]]["status"] for m in members], ["passed", "passed", "failed", "passed"])
        self.assertEqual(calls[0], ["b1", "b2", "b3", "b4"])
        self.assertLessEqual(len(calls), 2 * 2 + 1)  # 2*log2(K)+1

    def test_settle_twins_of_one_tip_are_one_branch_when_a_failure_cannot_be_attributed(self):
        # Two callers with the same tip share one slot (`join`); a split must not run the one branch twice more.
        member = self.members(1)[0]
        twins, calls = [member, dict(member, pid=999)], []

        def run_once(group):
            calls.append(len(group))
            return {"exit_code": 1, "failures": {"anchors": "no path here"}, "conflicted": []}

        verdicts = bq.settle(twins, run_once, self.attribute_factory([member]))
        self.assertEqual(calls, [1])
        self.assertEqual(verdicts[member["tip"]]["status"], bq.FAILED)

    def test_settle_red_main_stops_at_the_budget(self):
        members, calls = self.members(8), []
        verdicts = bq.settle(members, self.run_once_factory(set(), red_main=True, calls=calls),
                             self.attribute_factory(members), budget=5, log=lambda _t: None)
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(v["status"] == bq.FAILED for v in verdicts.values()))

    def test_settle_conflicted_members_become_solo_and_fatal_fails_everyone(self):
        members = self.members(2)

        def run_once(group):
            return {"exit_code": 0, "failures": {}, "conflicted": [(members[0], "CONFLICT in f")]}

        verdicts = bq.settle(members, run_once, self.attribute_factory(members))
        self.assertEqual(verdicts[members[0]["tip"]]["status"], bq.SOLO)
        self.assertEqual(verdicts[members[1]["tip"]]["status"], bq.PASSED)
        fatal = bq.settle(members, lambda g: {"exit_code": 75, "fatal": True, "detail": "no lock"},
                          self.attribute_factory(members))
        self.assertTrue(all(v["status"] == bq.FAILED and v["exit_code"] == 75 for v in fatal.values()))

    def test_format_verdicts(self):
        members = self.members(2)
        doc = {"batch": 4, "log": "/l.log", "members": members + [members[0]],
               "verdicts": {members[0]["tip"]: {"status": "passed"},
                            members[1]["tip"]: {"status": "failed", "detail": "1 finding"}}}
        lines = bq.format_verdicts(doc, members[1]["tip"])
        self.assertEqual(len(lines), 3)
        self.assertIn("batch 4 verdicts (log: /l.log)", lines[0])
        self.assertIn("PASSED", lines[1])
        self.assertIn("<- you", lines[2])
        self.assertIn("1 finding", lines[2])


class MutationPinTests(unittest.TestCase):
    """Cases a mutation run (2026-11-17) showed the other tests did not pin."""

    def test_with_field_names_the_first_twelve_characters_of_each_tip(self):
        self.assertEqual(bq.with_field(["a" * 40, "b" * 40]), " with=" + "a" * 12 + "," + "b" * 12)
        self.assertEqual(bq.with_field([]), "")

    def test_describe_running_counts_distinct_branches_and_shows_the_age(self):
        running = {"id": 7, "started": 100.0, "members": [{"tip": "a"}, {"tip": "a"}, {"tip": "b"}]}
        self.assertEqual(bq._describe_running(running, lambda: 400.0), "batch 7 running for 5 min (2 branches)")
        one = {"id": 8, "started": 100.0, "members": [{"tip": "a"}, {"tip": "a"}]}
        self.assertEqual(bq._describe_running(one, lambda: 130.0), "batch 8 running for 30 s (1 branch)")
        unknown = {"id": 9, "started": None, "members": [{"tip": "a"}]}
        self.assertEqual(bq._describe_running(unknown, lambda: 130.0), "batch 9 running for ? (1 branch)")

    def test_locked_creates_the_directory_and_a_world_writable_lock_file(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        with tempfile.TemporaryDirectory() as tmp:
            sdir = os.path.join(tmp, "a", "b")
            with bq.locked(sdir) as state:
                state["next_id"] = 4
            self.assertEqual(os.stat(os.path.join(sdir, "state.lock")).st_mode & 0o777, 0o666)
            with bq.locked(sdir) as state:
                self.assertEqual(state["next_id"], 4)

    def test_locked_discards_a_state_of_another_version_and_a_non_dict_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            for content in ('{"version": %d, "next_id": 9}' % (bq.STATE_VERSION + 1), "[1, 2]"):
                with open(os.path.join(tmp, "state.json"), "w") as handle:
                    handle.write(content)
                with bq.locked(tmp) as state:
                    self.assertEqual(state, bq.empty_state())

    def test_repo_key_of_a_separate_git_dir_named_modules_is_its_parent_checkout_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = os.path.join(tmp, "checkout")
            gitdir = os.path.join(tmp, "modules", "elsewhere")
            os.makedirs(checkout)
            os.makedirs(os.path.dirname(gitdir))
            subprocess.run(["git", "init", "-q", "--separate-git-dir", gitdir, checkout], check=True, capture_output=True)
            # the git directory is not under `.git/modules`, so the identity is the directory that holds it
            self.assertEqual(bq.repo_key(checkout), os.path.realpath(os.path.join(tmp, "modules")))

    def test_remove_tree_removes_the_parent_without_sudo_when_it_can(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "p")
            os.makedirs(os.path.join(parent, "repo", "sub"))
            bq.remove_tree("/r", os.path.join(parent, "repo"), run=run)
            self.assertFalse(os.path.exists(parent))
        self.assertEqual([c[0] for c in calls], ["git", "git"])
        self.assertEqual(calls[0][-4:], ["worktree", "remove", "--force", os.path.join(parent, "repo")])
        self.assertEqual(calls[1][-2:], ["worktree", "prune"])

    def test_remove_tree_falls_back_to_sudo_for_files_it_cannot_delete(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "p")
            stuck = os.path.join(parent, "repo", "stuck")
            os.makedirs(stuck)
            with open(os.path.join(stuck, "f"), "w") as handle:
                handle.write("x")
            os.chmod(stuck, 0o500)  # its file cannot be unlinked

            def run(argv, **kwargs):
                calls.append(argv)
                if argv[0] == "sudo":
                    os.chmod(stuck, 0o700)
                    shutil.rmtree(parent)
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.addCleanup(lambda: os.path.exists(stuck) and os.chmod(stuck, 0o700))
            bq.remove_tree("/r", os.path.join(parent, "repo"), run=run)
            self.assertIn(["sudo", "-n", "rm", "-rf", parent], calls)
            self.assertFalse(os.path.exists(parent))


if __name__ == "__main__":
    unittest.main()

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: batch_queue:batch_queue__interrupted_class]
