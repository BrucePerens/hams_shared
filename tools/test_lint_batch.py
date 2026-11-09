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
Tests [@ANCHOR: lint_batch:batches_that_may_run]
Tests [@ANCHOR: lint_batch:batch_limit]

Tests [@ANCHOR: lint_batch:batching_tools_changed]
Tests [@ANCHOR: lint_batch:path_finder]
Tests [@ANCHOR: lint_batch:owners_of]
Tests [@ANCHOR: lint_batch:changed_anchors]
Tests [@ANCHOR: lint_batch:changed_function_anchors]
Tests [@ANCHOR: lint_batch:anchor_finder]
Tests [@ANCHOR: lint_batch:default_command]
Tests [@ANCHOR: lint_batch:read_results]
Tests [@ANCHOR: lint_batch:LintBatch]
Tests [@ANCHOR: lint_batch:run_member]
Tests [@ANCHOR: run_linters:write_batch_stamp]
Tests [@ANCHOR: batch_queue:with_field]
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
import batch_queue  # noqa: E402
import lint_batch  # noqa: E402
import test_batch_queue as helpers  # noqa: E402

FAKE_LINTER = r'''
import json, os, subprocess, sys, time
results = sys.argv[1]
with open(os.environ["FAKE_CALLS"], "a") as calls:
    heads = subprocess.run(["git", "log", "--merges", "--format=%s", "-n", "20"], capture_output=True, text=True).stdout
    calls.write(json.dumps(sorted(l.replace("batch: merge ", "") for l in heads.splitlines())) + "\n")
if os.environ.get("FAKE_CWD_LOG"):
    with open(os.environ["FAKE_CWD_LOG"], "a") as cwd_log:
        cwd_log.write(os.path.basename(os.getcwd()) + "\n")
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

    def stamp_writer(tip, names, batch_id, with_tips):
        with open(os.path.join(stamps, tip), "w") as handle:
            handle.write("changed steps=%d names=%s batch=%s%s\n" % (
                len(names), ",".join(names), batch_id, batch_queue.with_field(with_tips)))

    name = spec.get("name", "hams_demo")
    runner = lint_batch.LintBatch(
        spec["repo"], name, "origin/main", [], stamp_writer,
        command=lambda tree, results: [sys.executable, spec["fake"], results], prepare=lambda tree: None,
        environ=dict(os.environ), fetch=False)
    code = lint_batch.run_member(spec["repo"], name, spec["branch"], spec["tip"], "origin/main", ["fake"],
                                 runner, timeout=60, poll=0.1, sdir=spec.get("sdir"), max_running=spec.get("max_running"))
    print("RESULT " + json.dumps({"code": code}))


class ForeignSharedTests(unittest.TestCase):
    """Tests [@ANCHOR: lint_batch:foreign_shared_checkout] with real directories and symlinks."""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="foreign_shared_")
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.main_shared = os.path.join(self.root, "main_shared")
        os.makedirs(self.main_shared)
        self.main = os.path.join(self.root, "hams_open")
        self.tree = os.path.join(self.root, "tmp_topic")
        os.makedirs(self.main)
        os.makedirs(self.tree)
        os.symlink(self.main_shared, os.path.join(self.main, "hams_shared"))

    def test_a_link_to_the_main_checkouts_hams_shared_is_not_foreign(self):
        os.symlink(self.main_shared, os.path.join(self.tree, "hams_shared"))
        self.assertFalse(lint_batch.foreign_shared_checkout(self.tree, self.main))

    def test_the_main_checkout_itself_is_not_foreign(self):
        self.assertFalse(lint_batch.foreign_shared_checkout(self.main, self.main))

    def test_a_hams_shared_linked_by_hand_to_another_checkout_is_foreign(self):
        other = os.path.join(self.root, "hs_worktree")
        os.makedirs(other)
        os.symlink(other, os.path.join(self.tree, "hams_shared"))
        self.assertTrue(lint_batch.foreign_shared_checkout(self.tree, self.main))

    def test_a_submodule_checked_out_inside_the_tree_is_foreign(self):
        os.makedirs(os.path.join(self.tree, "hams_shared"))
        self.assertTrue(lint_batch.foreign_shared_checkout(self.tree, self.main))

    def test_a_missing_hams_shared_is_not_foreign(self):
        self.assertFalse(lint_batch.foreign_shared_checkout(self.tree, self.main))


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

    def test_the_lint_batches_that_may_run_are_the_lint_slots_unless_the_batch_variable_says_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {"XDG_CACHE_HOME": tmp}
            self.assertEqual(lint_batch.batches_that_may_run(dict(base, HAMS_LINT_SLOTS="3")), 3)
            self.assertEqual(lint_batch.batches_that_may_run(dict(base, HAMS_LINT_SLOTS="1")), 1)
            self.assertIsNone(lint_batch.batches_that_may_run(dict(base, HAMS_LINT_SLOTS="3", HAMS_BATCH_MAX_RUNNING="2")))
            self.assertEqual(lint_batch.batches_that_may_run(dict(base, HAMS_LINT_SLOTS="bad")), 1)  # a typo is one slot

    def test_the_batch_limit_grows_by_the_burst_slots_only_while_the_machine_has_headroom(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {"XDG_CACHE_HOME": tmp, "HAMS_LINT_SLOTS": "3"}
            self.assertEqual(lint_batch.batch_limit(dict(base, HAMS_LINT_BURST_SLOTS="0")), 3, "no burst slots: the plain number")
            self.assertIsNone(lint_batch.batch_limit(dict(base, HAMS_LINT_BURST_SLOTS="2", HAMS_BATCH_MAX_RUNNING="2")))
            spare = [True]
            clock = [100.0]
            limit = lint_batch.batch_limit(dict(base, HAMS_LINT_BURST_SLOTS="2"), headroom=lambda: spare[0], ttl=5.0, now=lambda: clock[0])
            self.assertEqual(limit(), 5, "idle processors: three base slots and two burst slots")
            spare[0] = False
            self.assertEqual(limit(), 5, "the measurement is kept for the ttl")
            clock[0] += 6
            self.assertEqual(limit(), 3, "busy processors: the base slots only")

    def test_a_hams_shared_branch_changing_the_batching_tools_runs_alone(self):
        self.assertEqual(lint_batch.batching_tools_changed(["docs/x.md", "tools/foo.py"]), [])
        self.assertEqual(lint_batch.batching_tools_changed(["tools/run_linters.py", "x", "tools/batch_queue.py"]),
                         ["tools/batch_queue.py", "tools/run_linters.py"])
        self.assertEqual(lint_batch.batching_tools_changed([]), [])
        self.assertIsNone(self.reason(batching_tools=[]))
        why = self.reason(batching_tools=["tools/batch_queue.py"])
        self.assertIn("changes the batching tools", why)
        self.assertIn("tools/batch_queue.py", why)

    def test_a_foreign_hams_shared_runs_alone(self):
        self.assertIsNone(self.reason(foreign_shared=False))
        self.assertIn("hams_shared is not the main", self.reason(foreign_shared=True))

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

    def test_tokens_include_unique_base_names_for_checkers_that_print_dir_and_file_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = helpers.make_repo(tmp)
            os.makedirs(os.path.join(work, "ham_x"))
            tip = helpers.commit_branch(work, "x", {"ham_x/demo_batch_x.py": "a\n", "ham_x/a.py": "b\n"})
            batch = lint_batch.LintBatch(work, "hams_demo", "origin/main", [], lambda *a: None, fetch=False)
            owners, find = batch.tokens([{"tip": tip, "branch": "x"}])
            self.assertEqual(owners["ham_x/demo_batch_x.py"], [tip])
            self.assertEqual(owners["demo_batch_x.py"], [tip])  # unique and long enough
            self.assertNotIn("a.py", owners)  # too short to be a token of its own
            self.assertEqual(find("File 'demo_batch_x.py' in '/t/ham_x'"), ["demo_batch_x.py"])

    def test_changed_anchors_are_the_ids_on_added_and_removed_lines_only(self):
        tag = lambda name: "[@ANCH" + "OR: %s]" % name  # noqa: E731 - keeps this file's source free of literal tags
        with tempfile.TemporaryDirectory() as tmp:
            work = helpers.make_repo(tmp)
            base = helpers.commit_branch(work, "seed", {"c.py": "# %s\n# %s\nx = 1\n" % (tag("demo:kept"), tag("demo:gone"))})
            helpers.git(work, "push", "-q", "origin", "seed:main")
            helpers.git(work, "fetch", "-q", "origin")
            tip = helpers.commit_branch(work, "x", {
                "c.py": "# %s\n# %s\nx = 2\n" % (tag("demo:kept"), tag("demo:fresh")),
                "t.py": "# Tests %s\n# Tests %s\n" % (tag("demo:fresh"), tag("demo:other_module:x")),
                "notes.txt": "--- a line that starts with dashes and names no anchor\n"})
            self.assertEqual(lint_batch.changed_anchors(work, "origin/main", tip),
                             {"demo:gone", "demo:fresh", "demo:other_module:x"})  # `demo:kept` is on an unchanged line
            self.assertEqual(lint_batch.changed_anchors(work, "no-such", tip), set())
            self.assertTrue(base)

    def test_an_edited_function_body_is_attributed_to_the_branch_through_its_anchor(self):
        tag = lambda name: "[@ANCH" + "OR: %s]" % name  # noqa: E731
        source = "# %s\ndef kept(x):\n    return x + %%d\n\n\n# %s\ndef other(x):\n    return x\n" % (tag("demo:edited"), tag("demo:untouched"))
        with tempfile.TemporaryDirectory() as tmp:
            work = helpers.make_repo(tmp)
            helpers.commit_branch(work, "seed", {"c.py": source % 1, "d.py": "# %s\ndef gone():\n    return 1\n" % tag("demo:vanishing")})
            helpers.git(work, "push", "-q", "origin", "seed:main")
            helpers.git(work, "fetch", "-q", "origin")
            tip = helpers.commit_branch(work, "x", {"c.py": source % 2, "d.py": "x = 1\n",
                                                    "e.py": "# %s\ndef fresh():\n    return 1\n" % tag("demo:added")})
            # The anchor lines did not change, so the line-based reading misses the edited body ...
            self.assertEqual(lint_batch.changed_anchors(work, "origin/main", tip), {"demo:added", "demo:vanishing"})
            # ... the function-based one finds it, a deleted function's anchor and an added one, and not an untouched function.
            self.assertEqual(lint_batch.changed_function_anchors(work, "origin/main", tip), {"demo:edited", "demo:vanishing", "demo:added"})
            self.assertEqual(lint_batch.changed_function_anchors(work, "no-such", tip), set())
            batch = lint_batch.LintBatch(work, "hams_demo", "origin/main", [], lambda *a: None, fetch=False)
            owners, find = batch.tokens([{"tip": tip, "branch": "x"}])
            self.assertEqual(owners["demo:edited"], [tip])
            self.assertEqual(find("c.md: code_hash mismatch for anchor 'demo:edited' -- the function changed"), ["demo:edited"])
            self.assertNotIn("demo:untouched", owners)

    def test_anchor_finder_finds_whole_anchor_ids_in_checker_output(self):
        find = lint_batch.anchor_finder(["m:feature", "m:feature_two"])
        self.assertEqual(find("    - Code Feature 'm:feature' has no active test linkage coverage."), ["m:feature"])
        self.assertEqual(find("Missing Cross-Reference Target: m:feature_two:"), ["m:feature_two"])
        self.assertEqual(find("m:feature_twoo and m:featur and xm:feature"), [])  # whole ids only
        self.assertEqual(find("'m:feature' again 'm:feature'"), ["m:feature"])  # once per line
        self.assertEqual(find("nothing"), [])

    def test_a_failure_that_names_only_an_anchor_is_blamed_on_the_branch_that_changed_it(self):
        import batch_queue
        tag = lambda name: "[@ANCH" + "OR: %s]" % name  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            work = helpers.make_repo(tmp)
            mine = helpers.commit_branch(work, "mine", {"a.txt": "# Tests %s\n" % tag("demo:feat")})
            other = helpers.commit_branch(work, "other", {"b.txt": "b2\n"})
            batch = lint_batch.LintBatch(work, "hams_demo", "origin/main", [], lambda *a: None, fetch=False)
            owners, find = batch.tokens([{"tip": mine, "branch": "mine"}, {"tip": other, "branch": "other"}])
            self.assertEqual(owners["demo:feat"], [mine])
            failures = {"verify_anchors": "    - Code Feature 'demo:feat' has no active test linkage coverage.\n",
                        "unrelated": "something with no token\n"}
            blamed, unattributed = batch_queue.blame_by_tokens(failures, owners, find)
            self.assertEqual(list(blamed), [mine])  # only the branch that touched the anchor
            self.assertTrue(blamed[mine][0].startswith("verify_anchors: "))
            self.assertEqual(unattributed, ["unrelated"])

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
        self.assertNotIn("with=", text)
        self.assertEqual(lint_stamp_gate.parse_stamp(text)[0], "changed")
        self.assertEqual(lint_stamp_gate.parse_stamp(text)[1], 2)
        self.assertIsNone(lint_stamp_gate.refusal(text, ["a.py"]))

    def test_batch_stamp_names_the_tips_it_was_checked_with_and_the_gate_still_accepts_it(self):
        import lint_stamp_gate
        import run_linters
        self.assertEqual(batch_queue.with_field([]), "")
        self.assertEqual(batch_queue.with_field(["a" * 40, "b" * 40]), " with=" + "a" * 12 + "," + "b" * 12)
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("XDG_CACHE_HOME")
            os.environ["XDG_CACHE_HOME"] = tmp
            try:
                run_linters.write_batch_stamp("t1", ["flake8", "check_cargo_clippy[x]"], 7, ["c" * 40, "d" * 40])
                with open(os.path.join(tmp, "hams-lint", "passed", "t1")) as handle:
                    text = handle.read()
            finally:
                if old is None:
                    del os.environ["XDG_CACHE_HOME"]
                else:
                    os.environ["XDG_CACHE_HOME"] = old
        self.assertTrue(text.rstrip().endswith("batch=7 with=" + "c" * 12 + "," + "d" * 12), text)
        self.assertEqual(lint_stamp_gate.parse_stamp(text), ("changed", 2, {"flake8", "check_cargo_clippy"}))
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

    def start(self, branch, tip, **extra):
        spec = {"repo": self.work, "branch": branch, "tip": tip, "stamps": self.stamps, "fake": self.fake,
                "sdir": self.sdir, **extra}
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
        # The stamp of a batched pass names the other tips it was checked with (not its own).
        stamp_a = open(os.path.join(self.stamps, good_a)).read()
        self.assertIn(" with=", stamp_a)
        self.assertIn(good_c[:12], stamp_a)
        self.assertNotIn(good_a[:12], stamp_a)
        # No combined tree is left behind.
        trees = os.path.join(self.tmp, "trees")
        self.assertEqual(os.listdir(trees) if os.path.isdir(trees) else [], [])
        self.assertNotIn("batch_lint", helpers.git(self.work, "worktree", "list"))

    def test_with_two_batches_allowed_a_second_run_starts_beside_the_first_and_the_third_waits(self):
        self.env["FAKE_SLEEP"] = "3"
        first = helpers.commit_branch(self.work, "first", {"f1.txt": "ok\n"})
        second = helpers.commit_branch(self.work, "second", {"s1.txt": "ok\n"})
        third = helpers.commit_branch(self.work, "third", {"t1.txt": "ok\n"})
        p1 = self.start("first", first, max_running=2)
        self.wait_for_calls(1)
        p2 = self.start("second", second, max_running=2)
        self.wait_for_calls(2)  # batch 2 started while batch 1 is still inside its fake linter
        self.assertIsNone(p1.poll())
        p3 = self.start("third", third, max_running=2)
        outs = [self.finish(proc) for proc in (p1, p2, p3)]
        self.assertEqual([code for code, _out in outs], [0, 0, 0], outs)
        self.assertIn("1 of 2 running slots are in use here; starting at once", outs[1][1])
        runs = [json.loads(line) for line in open(self.calls).read().splitlines()]
        self.assertEqual(sorted(runs), [["first"], ["second"], ["third"]], runs)
        for tip in (first, second, third):
            self.assertTrue(os.path.exists(os.path.join(self.stamps, tip)))

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


class SharedBranchEndToEndTests(unittest.TestCase):
    """Branches of hams_shared (a submodule worktree each) coalesce through the queue of the submodule's git directory,
    and the combined tree is a worktree named `SHARED_TREE_NAME` (a directory named hams_shared would be taken for a link)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        for name in ("sup", "sub"):
            os.makedirs(os.path.join(self.tmp, name))
        sup = helpers.make_repo(os.path.join(self.tmp, "sup"))
        sub = helpers.make_repo(os.path.join(self.tmp, "sub"))
        helpers.git(sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "hams_shared")
        self.shared = os.path.join(sup, "hams_shared")
        self.fake = os.path.join(self.tmp, "fake_linter.py")
        with open(self.fake, "w") as handle:
            handle.write(FAKE_LINTER)
        self.calls = os.path.join(self.tmp, "calls.log")
        self.cwd_log = os.path.join(self.tmp, "cwd.log")
        self.stamps = os.path.join(self.tmp, "stamps")
        os.makedirs(self.stamps)
        self.env = dict(os.environ, HAMS_BATCH_TREE_DIR=os.path.join(self.tmp, "trees"), FAKE_CALLS=self.calls,
                        FAKE_CWD_LOG=self.cwd_log, FAKE_SLEEP="2", HAMS_BATCH_DIR=os.path.join(self.tmp, "b"))
        for name in ("HAMS_LINT_LOCK_HELD", lint_batch.RUNNER_ENV):
            self.env.pop(name, None)

    def branch(self, name, files):
        """A branch of hams_shared made in a worktree of its own, as each agent has one. Returns (worktree, tip)."""
        tree = os.path.join(self.tmp, "wt_" + name)
        helpers.git(self.shared, "worktree", "add", "-q", "-B", name, tree, "origin/main")
        for file_name, text in files.items():
            with open(os.path.join(tree, file_name), "w") as handle:
                handle.write(text)
            helpers.git(tree, "add", file_name)
        helpers.git(tree, "commit", "-q", "-m", name)
        return tree, helpers.git(tree, "rev-parse", "HEAD")

    def start(self, tree, name, tip):
        spec = {"repo": tree, "branch": name, "tip": tip, "stamps": self.stamps, "fake": self.fake,
                "name": lint_batch.SHARED_TREE_NAME}
        return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--drive", json.dumps(spec)],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_branches_in_separate_worktrees_share_one_queue_and_one_combined_tree_named_for_the_run(self):
        first_tree, first = self.branch("first", {"f1.txt": "ok\n"})
        a_tree, tip_a = self.branch("shared-a", {"a1.txt": "ok\n"})
        b_tree, tip_b = self.branch("shared-b", {"b1.txt": "BAD\n"})
        p0 = self.start(first_tree, "first", first)
        deadline = time.time() + 30
        while time.time() < deadline and not (os.path.exists(self.calls) and open(self.calls).read().strip()):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(self.calls), "the first run never started")
        pa, pb = self.start(a_tree, "shared-a", tip_a), self.start(b_tree, "shared-b", tip_b)
        outs = {}
        for name, proc in (("first", p0), ("a", pa), ("b", pb)):
            out, _ = proc.communicate(timeout=120)
            result = [line for line in out.splitlines() if line.startswith("RESULT ")]
            self.assertTrue(result, out)
            outs[name] = (json.loads(result[0][7:])["code"], out)
        self.assertEqual((outs["first"][0], outs["a"][0], outs["b"][0]), (0, 0, 1), outs)
        runs = [json.loads(line) for line in open(self.calls).read().splitlines()]
        self.assertEqual(runs, [["first"], ["shared-a", "shared-b"]], runs)  # two runs for three requests: one queue
        self.assertIn("joined batch 2", outs["a"][1])
        self.assertEqual(open(self.cwd_log).read().split(), [lint_batch.SHARED_TREE_NAME] * 2)
        self.assertIn("b1.txt:1: BAD word", outs["b"][1])
        self.assertTrue(os.path.exists(os.path.join(self.stamps, tip_a)))
        self.assertFalse(os.path.exists(os.path.join(self.stamps, tip_b)))
        trees = os.path.join(self.tmp, "trees")
        self.assertEqual(os.listdir(trees) if os.path.isdir(trees) else [], [])
        self.assertNotIn("batch_lint", helpers.git(self.shared, "worktree", "list"))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--drive":
        drive(sys.argv[2:])
    else:
        unittest.main()
