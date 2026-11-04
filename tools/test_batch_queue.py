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
Tests [@ANCHOR: batch_queue:caps_from_env]
Tests [@ANCHOR: batch_queue:join]
Tests [@ANCHOR: batch_queue:leave]
Tests [@ANCHOR: batch_queue:seal]
Tests [@ANCHOR: batch_queue:finish]
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
Tests [@ANCHOR: batch_queue:format_verdicts]
"""

import os
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

    def test_caps_from_env(self):
        self.assertEqual(bq.caps_from_env({}), {"max_branches": 8, "max_seconds": 1800.0})
        self.assertEqual(bq.caps_from_env({"HAMS_BATCH_MAX_BRANCHES": "3", "HAMS_BATCH_MAX_SECONDS": "0"}),
                         {"max_branches": 3, "max_seconds": 0.0})
        bad = bq.caps_from_env({"HAMS_BATCH_MAX_BRANCHES": "x", "HAMS_BATCH_MAX_SECONDS": "nan"})
        self.assertEqual(bad, {"max_branches": 8, "max_seconds": 1800.0})
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
        deadline = time.time() + 10
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for " + what)

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


if __name__ == "__main__":
    unittest.main()
