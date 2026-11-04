#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for lint_batch.py, the lint side of the coalescing batch queue (ADR 0115), and the run_linters.py
options that go with it.

The end-to-end tests use REAL processes, a real git repository with an origin, the real queue and the real
combined-tree builder, and a fake linter script (it fails on any file containing the word BAD and names
that file, like flake8 would) in place of run_linters.py.

Tests [@ANCHOR: lint_batch:ineligible_reason]
Tests [@ANCHOR: lint_batch:changed_paths]
Tests [@ANCHOR: lint_batch:path_finder]
Tests [@ANCHOR: lint_batch:owners_of]
Tests [@ANCHOR: lint_batch:default_command]
Tests [@ANCHOR: lint_batch:read_results]
Tests [@ANCHOR: lint_batch:LintBatch]
Tests [@ANCHOR: lint_batch:run_member]
Tests [@ANCHOR: run_linters:write_batch_stamp]
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import lint_batch  # noqa: E402
import test_batch_queue as helpers  # noqa: E402

FAKE_LINTER = r'''
import json, os, subprocess, sys, time
results = sys.argv[1]
with open(os.environ["FAKE_CALLS"], "a") as calls:
    heads = subprocess.run(["git", "log", "--merges", "--format=%s", "-n", "20"], capture_output=True, text=True).stdout
    calls.write(json.dumps(sorted(l.replace("batch: merge ", "") for l in heads.splitlines())) + "\n")
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
text, rc = "", 0
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d != ".git"]
    for name in files:
        path = os.path.join(root, name)[2:]
        if "BAD" in open(path).read():
            text += "%s:1: BAD word\n" % path
            rc = 1
if os.path.exists("RED_MAIN"):
    text += "global check failed with no file\n"
    rc = 1
print(text, end="")
json.dump({"steps": [{"name": "fake", "returncode": rc, "seconds": 0.1}], "selected": ["fake", "other"],
           "outputs": {"fake": text} if rc else {}}, open(results, "w"))
sys.exit(rc)
'''


def drive(argv):
    """Child process: act as one `run_linters.py --changed` caller. Prints RESULT <json>."""
    spec = json.loads(argv[0])
    stamps = spec["stamps"]

    def stamp_writer(tip, names, batch_id):
        with open(os.path.join(stamps, tip), "w") as handle:
            handle.write("changed steps=%d names=%s batch=%s\n" % (len(names), ",".join(names), batch_id))

    runner = lint_batch.LintBatch(
        spec["repo"], "hams_demo", "origin/main", [], stamp_writer,
        command=lambda tree, results: [sys.executable, spec["fake"], results], prepare=lambda tree: None,
        environ=dict(os.environ), fetch=False)
    code = lint_batch.run_member(spec["repo"], "hams_demo", spec["branch"], spec["tip"], "origin/main", ["fake"],
                                 runner, timeout=60, poll=0.1, sdir=spec["sdir"])
    print("RESULT " + json.dumps({"code": code}))


class UnitTests(unittest.TestCase):
    def reason(self, **kw):
        base = dict(changed="origin/main", only="", skip="", modules="", solo=False, bundle="", environ={},
                    dirty=False, is_shared_checkout=False, lock_needed=True)
        base.update(kw)
        return lint_batch.ineligible_reason(**base)

    def test_a_plain_committed_heavy_changed_run_is_eligible(self):
        self.assertIsNone(self.reason())

    def test_everything_that_must_run_alone(self):
        self.assertIn("--solo", self.reason(solo=True))
        self.assertIn("combined run", self.reason(environ={lint_batch.RUNNER_ENV: "1"}))
        self.assertIn("NO_LOCK", self.reason(environ={"HAMS_LINT_NO_LOCK": "1"}))
        self.assertIn("another lint run", self.reason(environ={"HAMS_LINT_LOCK_HELD": "9"}))
        self.assertIn("off", self.reason(environ={"HAMS_LINT_BATCH": "off"}))
        self.assertIn("not a --changed", self.reason(changed=None))
        self.assertIn("--only", self.reason(only="flake8"))
        self.assertIn("--only", self.reason(skip="flake8"))
        self.assertIn("--only", self.reason(modules="ham_x"))
        self.assertIn("bundle", self.reason(bundle="b1"))
        self.assertIn("uncommitted", self.reason(dirty=True))
        self.assertIn("hams_shared", self.reason(is_shared_checkout=True))
        self.assertIn("cheap", self.reason(lock_needed=False))

    def test_path_finder_respects_file_name_boundaries(self):
        find = lint_batch.path_finder(["a.py", "m/data.py", "tools/a.py"])
        self.assertEqual(find("/w/tools/a.py:3: E1"), ["tools/a.py", "a.py"])  # ambiguous: both owners are named
        self.assertIn("a.py", find("a.py:1: x"))
        self.assertEqual(find("data.py:1: x"), [])  # `a.py` is not inside `data.py`
        self.assertEqual(find("/tree/m/data.py:9: bad"), ["m/data.py"])
        self.assertEqual(find("nothing here"), [])

    def test_owners_of(self):
        self.assertEqual(lint_batch.owners_of({"t1": ["a", "b"], "t2": ["b"]}), {"a": ["t1"], "b": ["t1", "t2"]})

    def test_default_command_and_read_results(self):
        argv = lint_batch.default_command("/tree", "/r.json", ["-j", "2"], "origin/main")
        self.assertEqual(argv[0:3], ["nice", "-n", "10"])
        self.assertIn("/tree/tools/run_linters.py", argv)
        self.assertEqual(argv[argv.index("--changed") + 1], "origin/main")
        self.assertIn("--solo", argv)
        self.assertEqual(argv[argv.index("--results-json") + 1], "/r.json")
        self.assertEqual(argv[-2:], ["-j", "2"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            self.assertEqual(lint_batch.read_results(path), ([], {}))
            with open(path, "w") as handle:
                json.dump({"steps": [{"name": "a", "returncode": 0}, {"name": "b", "returncode": 1}],
                           "selected": ["a", "b", "c"], "outputs": {"b": "out"}}, handle)
            self.assertEqual(lint_batch.read_results(path), (["a", "b", "c"], {"b": "out"}))
            with open(path, "w") as handle:
                json.dump({"steps": [{"name": "a", "returncode": 0}]}, handle)
            self.assertEqual(lint_batch.read_results(path), (["a"], {}))

    def test_changed_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = helpers.make_repo(tmp)
            os.makedirs(os.path.join(work, "n"), exist_ok=True)
            tip = helpers.commit_branch(work, "x", {"n/new.txt": "x\n"})
            self.assertEqual(lint_batch.changed_paths(work, "origin/main", tip), ["n/new.txt"])
            self.assertEqual(lint_batch.changed_paths(work, "no-such", tip), [])

    def test_write_batch_stamp_format_is_accepted_by_the_stamp_gate(self):
        import lint_stamp_gate
        import run_linters
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("XDG_CACHE_HOME")
            os.environ["XDG_CACHE_HOME"] = tmp
            try:
                run_linters.write_batch_stamp("abc123", ["flake8", "check_cargo_clippy[x]"], 7)
                run_linters.write_batch_stamp("empty", [], 7)
                with open(os.path.join(tmp, "hams-lint", "passed", "abc123")) as handle:
                    text = handle.read()
                self.assertFalse(os.path.exists(os.path.join(tmp, "hams-lint", "passed", "empty")))
            finally:
                if old is None:
                    del os.environ["XDG_CACHE_HOME"]
                else:
                    os.environ["XDG_CACHE_HOME"] = old
        self.assertTrue(text.startswith("changed "))
        self.assertTrue(text.rstrip().endswith("batch=7"))
        self.assertEqual(lint_stamp_gate.parse_stamp(text)[0], "changed")
        self.assertEqual(lint_stamp_gate.parse_stamp(text)[1], 2)
        self.assertIsNone(lint_stamp_gate.refusal(text, ["a.py"]))

    def test_run_linters_accepts_solo(self):
        out = subprocess.run([sys.executable, os.path.join(_HERE, "run_linters.py"), "--help"],
                             capture_output=True, text=True).stdout
        self.assertIn("--solo", out)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.work = helpers.make_repo(self.tmp)
        self.fake = os.path.join(self.tmp, "fake_linter.py")
        with open(self.fake, "w") as handle:
            handle.write(FAKE_LINTER)
        self.calls = os.path.join(self.tmp, "calls.log")
        self.stamps = os.path.join(self.tmp, "stamps")
        os.makedirs(self.stamps)
        self.sdir = os.path.join(self.tmp, "state")
        self.env = dict(os.environ, HAMS_BATCH_TREE_DIR=os.path.join(self.tmp, "trees"), FAKE_CALLS=self.calls,
                        FAKE_SLEEP="2", HAMS_BATCH_DIR=os.path.join(self.tmp, "b"))
        for name in ("HAMS_LINT_LOCK_HELD", lint_batch.RUNNER_ENV):
            self.env.pop(name, None)

    def start(self, branch, tip):
        spec = {"repo": self.work, "branch": branch, "tip": tip, "stamps": self.stamps, "fake": self.fake,
                "sdir": self.sdir}
        return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--drive", json.dumps(spec)],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

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

    def test_requests_during_a_run_become_one_combined_run_with_per_branch_verdicts(self):
        first = helpers.commit_branch(self.work, "first", {"f1.txt": "ok\n"})
        good_a = helpers.commit_branch(self.work, "good-a", {"a1.txt": "ok\n"})
        bad_b = helpers.commit_branch(self.work, "bad-b", {"b1.txt": "this is BAD\n"})
        good_c = helpers.commit_branch(self.work, "good-c", {"c1.txt": "ok\n"})
        p0 = self.start("first", first)
        self.wait_for_calls(1)  # batch 1 is running its (slow) fake linter
        procs = [(name, tip, self.start(name, tip)) for name, tip in
                 (("good-a", good_a), ("bad-b", bad_b), ("good-c", good_c))]
        code0, out0 = self.finish(p0)
        results = {name: self.finish(proc) for name, _tip, proc in procs}
        self.assertEqual(code0, 0, out0)
        self.assertEqual(results["good-a"][0], 0, results["good-a"][1])
        self.assertEqual(results["good-c"][0], 0, results["good-c"][1])
        self.assertEqual(results["bad-b"][0], 1, results["bad-b"][1])
        # Exactly two runs for four requests: the first alone, then ONE over the three that arrived meanwhile.
        runs = [json.loads(line) for line in open(self.calls).read().splitlines()]
        self.assertEqual(len(runs), 2, runs)
        self.assertEqual(runs[0], ["first"])
        self.assertEqual(runs[1], ["bad-b", "good-a", "good-c"])
        # Messages and verdicts.
        for name in ("good-a", "bad-b", "good-c"):
            self.assertIn("joined batch 2 with", results[name][1])
        self.assertIn("combined run of 3 branches", results["bad-b"][1])
        self.assertIn("checked together with 2 other branches", results["good-a"][1])
        self.assertIn("b1.txt:1: BAD word", results["bad-b"][1])
        self.assertIn("FAILED", results["bad-b"][1])
        self.assertIn("PASSED", results["good-a"][1])
        # The clean branches are stamped at once, the failing one is not.
        self.assertTrue(os.path.exists(os.path.join(self.stamps, good_a)))
        self.assertTrue(os.path.exists(os.path.join(self.stamps, good_c)))
        self.assertTrue(os.path.exists(os.path.join(self.stamps, first)))
        self.assertFalse(os.path.exists(os.path.join(self.stamps, bad_b)))
        self.assertIn("batch=2", open(os.path.join(self.stamps, good_a)).read())
        # No combined tree is left behind.
        trees = os.path.join(self.tmp, "trees")
        self.assertEqual(os.listdir(trees) if os.path.isdir(trees) else [], [])
        self.assertNotIn("batch_lint", helpers.git(self.work, "worktree", "list"))

    def test_conflicting_branch_runs_alone_and_unattributable_failure_splits(self):
        self.env["FAKE_SLEEP"] = "1"
        base_tip = helpers.commit_branch(self.work, "base", {"z.txt": "z\n"})
        one = helpers.commit_branch(self.work, "one", {"shared.txt": "1\nONE\n3\n4\n5\n6\n7\n"})
        two = helpers.commit_branch(self.work, "two", {"shared.txt": "1\nTWO\n3\n4\n5\n6\n7\n"})
        p0 = self.start("base", base_tip)
        self.wait_for_calls(1)
        p1, p2 = self.start("one", one), self.start("two", two)
        code0, _ = self.finish(p0)
        r1, r2 = self.finish(p1), self.finish(p2)
        self.assertEqual(code0, 0)
        # One of the pair conflicts with the other in the combined tree and is told to run alone (code None).
        outs = r1[1] + r2[1]
        self.assertIn("conflicts with another branch", outs)
        self.assertEqual(sorted(("passed" if "PASSED" in out else "solo") for out in (r1[1], r2[1])), ["passed", "solo"])


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--drive":
        drive(sys.argv[2:])
    else:
        unittest.main()
