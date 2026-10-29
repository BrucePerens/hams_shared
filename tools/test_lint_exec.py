#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_exec.py (the parallel step runner behind run_linters.py).

Real subprocesses throughout (python3 -c ...), no mocks: the runner's contract is about what
real child processes print and return, in what order, and how long it all takes.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lint_exec  # noqa: E402

PY = sys.executable


def py_step(name, code, **kwargs):
    return lint_exec.Step(name=name, argv=[PY, "-c", code], **kwargs)


class OutputOrderTests(unittest.TestCase):
    def test_output_is_in_declaration_order_whatever_finishes_first(self):
        steps = [
            py_step("slow", "import time; time.sleep(0.6); print('A-slow')"),
            py_step("fast", "print('B-fast')"),
            py_step("mid", "import time; time.sleep(0.2); print('C-mid')"),
        ]
        out = io.StringIO()
        lint_exec.execute(steps, jobs=3, out=out)
        self.assertEqual(out.getvalue(), "A-slow\nB-fast\nC-mid\n")

    def test_parallel_and_sequential_print_the_same_text(self):
        steps = [
            py_step(f"s{i}", f"print('out{i}'); import sys; sys.exit({i % 2})")
            for i in range(6)
        ]
        seq = io.StringIO()
        par = io.StringIO()
        lint_exec.execute(steps, jobs=1, out=seq)
        lint_exec.execute(steps, jobs=6, out=par)
        self.assertEqual(seq.getvalue(), par.getvalue())

    def test_jobs_actually_overlap(self):
        steps = [py_step(f"s{i}", "import time; time.sleep(0.5)") for i in range(4)]
        started = time.monotonic()
        lint_exec.execute(steps, jobs=4, out=io.StringIO())
        self.assertLess(time.monotonic() - started, 1.5)

    def test_one_job_runs_one_step_at_a_time(self):
        steps = [py_step(f"s{i}", "import time; time.sleep(0.3)") for i in range(3)]
        started = time.monotonic()
        lint_exec.execute(steps, jobs=1, out=io.StringIO())
        self.assertGreaterEqual(time.monotonic() - started, 0.85)


class ExitCodeSemanticsTests(unittest.TestCase):
    def test_failure_in_one_parallel_step_is_reported_and_others_still_run(self):
        steps = [
            py_step("ok1", "print('fine1')"),
            py_step("bad", "import sys; print('violation'); sys.stderr.write('trace\\n'); sys.exit(3)"),
            py_step("ok2", "print('fine2')"),
        ]
        out = io.StringIO()
        results = lint_exec.execute(steps, jobs=3, out=out)
        self.assertEqual([r.returncode for r in results], [0, 3, 0])
        self.assertIn("fine1", out.getvalue())
        self.assertIn("fine2", out.getvalue())
        self.assertIn("violation", out.getvalue())
        self.assertIn("trace", out.getvalue())

    def test_failed_run_prints_header_then_stdout_then_stderr(self):
        step = py_step(
            "bad",
            "import sys; print('OUT'); sys.stderr.write('ERR\\n'); sys.exit(1)",
            fail_header="HEADER",
        )
        out = io.StringIO()
        lint_exec.execute([step], jobs=1, out=out)
        text = out.getvalue()
        self.assertLess(text.index("HEADER"), text.index("OUT"))
        self.assertLess(text.index("OUT"), text.index("ERR"))

    def test_passing_run_prints_stdout_only_when_print_ok(self):
        quiet = py_step("quiet", "print('chatter')", print_ok=False)
        loud = py_step("loud", "print('chatter')")
        empty = py_step("empty", "pass")
        out = io.StringIO()
        lint_exec.execute([quiet, loud, empty], jobs=2, out=out)
        self.assertEqual(out.getvalue(), "chatter\n")

    def test_header_is_not_printed_for_a_passing_run(self):
        step = py_step("ok", "print('x')", fail_header="HEADER")
        out = io.StringIO()
        lint_exec.execute([step], jobs=1, out=out)
        self.assertNotIn("HEADER", out.getvalue())

    def test_missing_executable_fails_with_its_message(self):
        step = lint_exec.Step(
            name="nope",
            argv=["/nonexistent/linter", "x"],
            missing_msg="linter not found",
        )
        out = io.StringIO()
        results = lint_exec.execute([step], jobs=1, out=out)
        self.assertEqual(results[0].returncode, 1)
        self.assertIn("linter not found", out.getvalue())

    def test_static_failure_step_fails_without_running_anything(self):
        step = lint_exec.Step(name="static", static_output="module missing")
        out = io.StringIO()
        results = lint_exec.execute([step], jobs=2, out=out)
        self.assertEqual(results[0].returncode, 1)
        self.assertEqual(out.getvalue(), "module missing\n")


class LockTests(unittest.TestCase):
    def test_steps_sharing_a_lock_never_overlap(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        marker = os.path.join(tmp, "busy")
        code = (
            "import os, sys, time\n"
            f"m = {marker!r}\n"
            "if os.path.exists(m): sys.exit(9)\n"
            "open(m, 'w').close(); time.sleep(0.3); os.remove(m)\n"
        )
        steps = [py_step(f"c{i}", code, lock="cargo") for i in range(4)]
        results = lint_exec.execute(steps, jobs=4, out=io.StringIO())
        self.assertEqual([r.returncode for r in results], [0, 0, 0, 0])

    def test_steps_without_a_lock_do_overlap(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        def overlapping(me, other):
            code = (
                "import os, sys, time\n"
                f"open({os.path.join(tmp, me)!r}, 'w').close()\n"
                "time.sleep(0.8)\n"
                f"sys.exit(0 if os.path.exists({os.path.join(tmp, other)!r}) else 9)\n"
            )
            return py_step(me, code)

        steps = [overlapping("one", "two"), overlapping("two", "one")]
        results = lint_exec.execute(steps, jobs=2, out=io.StringIO())
        self.assertEqual([r.returncode for r in results], [0, 0])


class TimingTableTests(unittest.TestCase):
    def test_table_lists_every_step_slowest_first_with_status(self):
        results = [
            lint_exec.Result("quick", 0, "", 0.2),
            lint_exec.Result("slow", 1, "", 9.5),
            lint_exec.Result("hit", 0, "", 0.0, cached=True),
        ]
        table = lint_exec.timing_table(results, skipped=[("x", "y")], wall=10.0)
        lines = table.splitlines()
        self.assertIn("slow", lines[1])
        self.assertIn("FAILED", lines[1])
        self.assertIn("cached", table)
        self.assertIn("1 skipped", table)
        self.assertIn("wall clock 10.0s", table)

    def test_every_result_has_a_measured_duration(self):
        results = lint_exec.execute(
            [py_step("nap", "import time; time.sleep(0.25)")], jobs=1, out=io.StringIO()
        )
        self.assertGreaterEqual(results[0].seconds, 0.2)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.tools = os.path.join(self.tmp, "tools")
        os.makedirs(self.repo)
        os.makedirs(self.tools)
        with open(os.path.join(self.repo, "a.py"), "w") as handle:
            handle.write("x = 1\n")
        with open(os.path.join(self.tools, "check.py"), "w") as handle:
            handle.write("# checker\n")
        self.cache_path = os.path.join(self.tmp, "cache.json")
        self.counter = os.path.join(self.tmp, "runs")

    def _step(self):
        code = (
            f"open({self.counter!r}, 'a').write('x'); print('checked')"
        )
        return py_step(
            "cached-step",
            code,
            roots=(self.repo,),
            suffixes=frozenset({".py"}),
        )

    def _run(self, step):
        cache = lint_exec.Cache(self.cache_path)
        prints = lint_exec.InputFingerprints(self.tools)
        out = io.StringIO()
        results = lint_exec.execute([step], 1, cache=cache, fingerprints=prints, out=out)
        return results[0], out.getvalue()

    def _runs(self):
        try:
            with open(self.counter) as handle:
                return len(handle.read())
        except OSError:
            return 0

    def test_second_run_with_identical_inputs_is_a_cache_hit_with_same_output(self):
        first, first_out = self._run(self._step())
        second, second_out = self._run(self._step())
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(first_out, second_out)
        self.assertEqual(self._runs(), 1)

    def test_a_changed_input_file_invalidates(self):
        self._run(self._step())
        time.sleep(0.01)
        with open(os.path.join(self.repo, "a.py"), "w") as handle:
            handle.write("x = 22222\n")
        second, _ = self._run(self._step())
        self.assertFalse(second.cached)
        self.assertEqual(self._runs(), 2)

    def test_a_new_input_file_invalidates(self):
        self._run(self._step())
        with open(os.path.join(self.repo, "b.py"), "w") as handle:
            handle.write("y = 1\n")
        second, _ = self._run(self._step())
        self.assertFalse(second.cached)

    def test_a_file_of_another_suffix_does_not_invalidate(self):
        self._run(self._step())
        with open(os.path.join(self.repo, "notes.txt"), "w") as handle:
            handle.write("hello\n")
        second, _ = self._run(self._step())
        self.assertTrue(second.cached)

    def test_a_changed_checker_script_invalidates(self):
        self._run(self._step())
        time.sleep(0.01)
        with open(os.path.join(self.tools, "check.py"), "w") as handle:
            handle.write("# checker v2, much longer\n")
        second, _ = self._run(self._step())
        self.assertFalse(second.cached)

    def test_a_failing_run_is_never_cached(self):
        step = py_step(
            "failing",
            "import sys; sys.exit(1)",
            roots=(self.repo,),
            suffixes=frozenset({".py"}),
        )
        self._run(step)
        second, _ = self._run(step)
        self.assertFalse(second.cached)
        self.assertEqual(second.returncode, 1)

    def test_an_uncacheable_step_always_runs(self):
        step = self._step()
        step.cacheable = False
        self._run(step)
        second, _ = self._run(step)
        self.assertFalse(second.cached)
        self.assertEqual(self._runs(), 2)

    def test_a_different_argv_is_a_different_key(self):
        self._run(self._step())
        other = self._step()
        other.argv = other.argv + ["extra"]
        second, _ = self._run(other)
        self.assertFalse(second.cached)

    def test_corrupt_cache_file_is_ignored(self):
        with open(self.cache_path, "w") as handle:
            handle.write("{not json")
        result, _ = self._run(self._step())
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.cached)


class ChangedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = "/ws/hams_open"
        self.flake = lint_exec.Step(
            name="flake8",
            argv=["x"],
            roots=(self.root,),
            suffixes=frozenset({".py"}),
        )
        self.scss = lint_exec.Step(
            name="scss",
            argv=["x"],
            roots=(self.root,),
            suffixes=frozenset({".scss"}),
        )
        self.anything = lint_exec.Step(name="any", argv=["x"], roots=(self.root,))
        self.other_root = lint_exec.Step(
            name="com-only",
            argv=["x"],
            roots=("/ws/hams_com",),
            suffixes=frozenset({".py"}),
        )
        self.always = lint_exec.Step(name="always", argv=["x"], always=True)
        self.static = lint_exec.Step(name="static", static_output="bad")
        self.steps = [
            self.flake,
            self.scss,
            self.anything,
            self.other_root,
            self.always,
            self.static,
        ]

    def names(self, steps):
        return [s.name for s in steps]

    def test_only_steps_whose_inputs_changed_run(self):
        run, skipped = lint_exec.select_for_changed(
            self.steps, {"/ws/hams_open/m/a.py"}, run_all=False
        )
        self.assertEqual(self.names(run), ["flake8", "any", "always", "static"])
        self.assertEqual(
            [s.name for s, _ in skipped], ["scss", "com-only"]
        )

    def test_a_static_failure_always_survives_selection(self):
        run, _ = lint_exec.select_for_changed(self.steps, set(), run_all=False)
        self.assertIn("static", self.names(run))

    def test_checker_tooling_change_runs_everything(self):
        run, skipped = lint_exec.select_for_changed(self.steps, set(), run_all=True)
        self.assertEqual(self.names(run), self.names(self.steps))
        self.assertEqual(skipped, [])

    def test_a_sibling_prefix_directory_is_not_a_match(self):
        run, _ = lint_exec.select_for_changed(
            self.steps, {"/ws/hams_open_extra/a.py"}, run_all=False
        )
        self.assertNotIn("flake8", self.names(run))

    def test_file_root_matches_exactly_that_file(self):
        step = lint_exec.Step(
            name="one", argv=["x"], roots=("/ws/hams_open/daemons/hams_config.py",)
        )
        run, _ = lint_exec.select_for_changed(
            [step], {"/ws/hams_open/daemons/hams_config.py"}, run_all=False
        )
        self.assertEqual(self.names(run), ["one"])


class PassStampTests(unittest.TestCase):
    def setUp(self):
        import run_linters

        self.rl = run_linters
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "r")
        os.makedirs(self.repo)
        for cmd in (
            ["init", "-q"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
        ):
            subprocess.run(["git", "-C", self.repo, *cmd], check=True)
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp, "cache")
        self.addCleanup(os.environ.pop, "XDG_CACHE_HOME", None)

    def _head(self):
        return subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    def test_clean_tree_is_stamped_with_its_commit(self):
        self.rl._write_pass_stamps([self.repo], "full")
        self.assertTrue(os.path.exists(os.path.join(self.rl.pass_stamp_dir(), self._head())))

    def test_dirty_tree_is_not_stamped(self):
        with open(os.path.join(self.repo, "new.txt"), "w") as handle:
            handle.write("x")
        self.rl._write_pass_stamps([self.repo], "full")
        self.assertFalse(os.path.exists(os.path.join(self.rl.pass_stamp_dir(), self._head())))


class WalkInputsTests(unittest.TestCase):
    def test_pruned_directories_and_other_suffixes_are_not_inputs(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in ("a.py", "pkg/b.py", "node_modules/c.py", "target/d.py", "e.txt"):
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write("x")
        found = [os.path.relpath(p, tmp) for p, _, _ in lint_exec.walk_inputs(tmp, {".py"})]
        self.assertEqual(found, ["a.py", os.path.join("pkg", "b.py")])

    def test_a_single_file_root_is_fingerprinted(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "only.py")
        with open(path, "w") as handle:
            handle.write("x")
        found = [p for p, _, _ in lint_exec.walk_inputs(path, None)]
        self.assertEqual(found, [path])


class RunLintersCommandLineTests(unittest.TestCase):
    """The real run_linters.py, run for real from a scratch repository whose `tools` entry is
    a symlink to this tools directory (one of the layouts run_linters.py documents)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        tools = os.path.dirname(os.path.abspath(__file__))
        os.symlink(tools, os.path.join(self.tmp, "tools"))
        module = os.path.join(self.tmp, "mod_a")
        os.makedirs(module)
        with open(os.path.join(module, "__manifest__.py"), "w") as handle:
            handle.write("{'name': 'a', 'depends': []}\n")

    def _run(self, *args):
        script = os.path.join(self.tmp, "tools", "run_linters.py")
        return subprocess.run(
            [PY, script, *args], capture_output=True, text=True, timeout=300
        )

    def test_list_names_the_steps_and_exits_zero(self):
        res = self._run("--list")
        self.assertEqual(res.returncode, 0, res.stderr)
        names = res.stdout.split("\n")
        self.assertIn("check_burn_list", names)
        self.assertIn("pre_flight_check[mod_a]", names)

    def test_unknown_option_is_rejected(self):
        res = self._run("--no-such-option")
        self.assertEqual(res.returncode, 2)

    def test_only_selects_a_step_and_a_missing_one_selects_nothing(self):
        res = self._run("--list", "--only", "check_shebang")
        self.assertEqual(res.stdout.split("\n")[0], "check_shebang")
        none = self._run("--list", "--only", "no_such_step_anywhere")
        self.assertEqual(none.stdout.strip(), "")

    def test_skip_removes_a_step(self):
        res = self._run("--list", "--skip", "check_burn_list")
        self.assertNotIn("check_burn_list", res.stdout.split("\n"))

    def test_a_failing_step_fails_the_run_with_exit_one_and_the_halt_banner(self):
        # A module with a Python syntax error: flake8 (E9) reports it, the run exits 1,
        # and the other selected step still ran and printed.
        with open(os.path.join(self.tmp, "mod_a", "broken.py"), "w") as handle:
            handle.write("def broken(:\n")
        res = self._run("mod_a", "--only", "flake8", "-j", "2", "--no-timing")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("Flake8 Violations", res.stdout)
        self.assertIn("Halting due to linter violations", res.stdout)

    def test_a_clean_selection_exits_zero_and_prints_the_timing_table(self):
        with open(os.path.join(self.tmp, "mod_a", "fine.py"), "w") as handle:
            handle.write("X = 1\n")
        res = self._run("mod_a", "--only", "flake8", "-j", "2")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("step", res.stdout)
        self.assertIn("flake8", res.stdout)
        self.assertIn("wall clock", res.stdout)

    def test_parallel_and_sequential_runs_print_identical_diagnostics(self):
        with open(os.path.join(self.tmp, "mod_a", "broken.py"), "w") as handle:
            handle.write("import os\n")
        seq = self._run("mod_a", "--only", "flake8,check_shebang", "-j", "1", "--no-timing")
        par = self._run("mod_a", "--only", "flake8,check_shebang", "-j", "4", "--no-timing")
        self.assertEqual(seq.returncode, par.returncode)
        self.assertEqual(seq.stdout, par.stdout)


if __name__ == "__main__":
    unittest.main()
