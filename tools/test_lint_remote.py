#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_remote.py: routing, sync, running through a (fake) ssh, stamps and fallback."""

import io
import json
import re
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import lint_exec  # noqa: E402
import lint_remote as lr  # noqa: E402
import run_linters  # noqa: E402

FULL = lr.Probe("m", True, cores=8, load1=0.5, caps=frozenset({"python3", "pytest", "flake8", "node", "cargo", "chrome", "odoo"}))


def git(repo, *args):
    subprocess.run(
        ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com", *args], check=True, capture_output=True
    )


def write(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_repo(path, files):
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for name, text in files.items():
        write(os.path.join(path, name), text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")


INVENTORY = {
    "devbox": {"local": True},
    "big": {"ssh": {"target": "big"}, "jobs": 4, "max_load_ratio": 0.8},
    "small": {"ssh": {"target": "small"}, "jobs": 2, "max_load_ratio": 0.8},
    "winbox": {"runner": False, "reason": "Windows PowerShell host"},
    "hams1": {"runner": False, "never": True, "reason": "PRODUCTION"},
}
DURATIONS = {"pytest daemons/a": 300.0, "pytest daemons/b": 300.0, "check_x": 5.0, "eslint": 200.0, "check_summation_bias": 400.0}


class CargoJobsShellTests(unittest.TestCase):
    """cargo_jobs_shell picks CARGO_BUILD_JOBS on the remote machine from its measured MemAvailable."""

    def jobs(self, avail_kb, cores, share=1, preset=None):
        with tempfile.TemporaryDirectory() as tmp:
            meminfo = os.path.join(tmp, "meminfo")
            with open(meminfo, "w") as handle:
                handle.write("MemTotal: 99999999 kB\nMemAvailable: %d kB\n" % avail_kb)
            env = {"PATH": os.environ["PATH"], "CARGO_JOBS_MEMINFO": meminfo, "CARGO_JOBS_CORES": str(cores)}
            if preset:
                env["CARGO_BUILD_JOBS"] = preset
            out = subprocess.run(["bash", "-c", lr.cargo_jobs_shell(share) + ' && echo "jobs=${CARGO_BUILD_JOBS:-unset}"'],
                                 env=env, capture_output=True, text=True, check=True)
            return out.stdout.strip()

    def test_the_mac1_vm_gets_three_jobs_not_ten(self):
        self.assertEqual(self.jobs(9749 * 1024, 10), "jobs=3")

    def test_a_big_host_is_capped_at_its_cores(self):
        self.assertEqual(self.jobs(60 * 1024 * 1024, 8), "jobs=8")

    def test_a_starved_host_still_gets_one_job(self):
        self.assertEqual(self.jobs(1024 * 1024, 10), "jobs=1")
        self.assertEqual(self.jobs(100, 10), "jobs=1")

    def test_concurrent_steps_share_the_budget(self):
        self.assertEqual(self.jobs(14 * 1024 * 1024, 8, share=1), "jobs=5")
        self.assertEqual(self.jobs(14 * 1024 * 1024, 8, share=4), "jobs=1")

    def test_a_job_count_the_caller_set_is_kept(self):
        self.assertEqual(self.jobs(9749 * 1024, 10, preset="7"), "jobs=7")

    def test_without_meminfo_nothing_is_set(self):
        out = subprocess.run(["bash", "-c", lr.cargo_jobs_shell(1) + ' && echo "jobs=${CARGO_BUILD_JOBS:-unset}"'],
                             env={"PATH": os.environ["PATH"], "CARGO_JOBS_MEMINFO": "/nonexistent/meminfo"},
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "jobs=unset")


class RequirementTests(unittest.TestCase):
    def test_requirements(self):
        self.assertEqual(lr.requirements_for("check_summation_bias"), {"local"})
        self.assertEqual(lr.requirements_for("check_pip_audit"), {"local"})
        self.assertEqual(lr.requirements_for("pytest daemons/relay_ca"), {"python3", "pytest"})
        self.assertIn("node", lr.requirements_for("eslint"))
        self.assertIn("cargo", lr.requirements_for("check_cargo_clippy"))
        self.assertIn("clippy", lr.requirements_for("check_cargo_clippy"))
        self.assertEqual(lr.requirements_for("pytest tools/[host-bound]"), {"local"})
        self.assertEqual(lr.requirements_for("pytest tools/[2of4]"), {"python3", "pytest"})
        self.assertNotIn("clippy", lr.requirements_for("check_rust_function_test_anchors"))
        self.assertEqual(lr.requirements_for("check_float_isfinite"), {"python3"})
        self.assertIn("chrome", lr.requirements_for("check_owl_templates"))

    def test_probe_parsing_measures_capabilities_and_rejects_old_python(self):
        text = "os=Linux\ncores=8\nload1=1.50\npyver=3.13\nhas_git=1\nhas_tar=1\nhas_flake8=1\nqueued=2\n"
        probe = lr.parse_probe("m", text)
        self.assertEqual((probe.cores, probe.load1, probe.queued), (8, 1.5, 2))
        self.assertEqual(probe.caps, {"git", "tar", "flake8", "python3"})
        self.assertEqual(lr.parse_probe("m", "os=Linux\nload1=nan\ncores=4\n").load1, 0.0)
        old = lr.parse_probe("m", "os=Darwin\ncores=12\npyver=3.9\nhas_git=1\nhas_tar=1\n")
        self.assertNotIn("python3", old.caps)
        self.assertFalse(lr.parse_probe("m", "garbage").reachable)


class PlanTests(unittest.TestCase):
    def plan(self, mode="auto", probes=None, names=None, local_load=0.0):
        names = names or ["pytest daemons/a", "pytest daemons/b", "check_x", "eslint", "check_summation_bias"]
        probes = probes if probes is not None else {"big": FULL, "small": FULL}
        return lr.make_plan(names, set(), DURATIONS, INVENTORY, probes, mode, 16, local_load)

    def test_local_mode_keeps_everything(self):
        plan = self.plan("local")
        self.assertEqual(len(plan.local), 5)
        self.assertEqual(plan.remote, {})

    def test_auto_sends_heavy_capable_steps_away_and_keeps_cheap_and_local_only_steps(self):
        plan = self.plan(local_load=40.0)
        self.assertIn("check_x", plan.local)
        self.assertIn("check_summation_bias", plan.local)
        sent = {n for names in plan.remote.values() for n in names}
        self.assertTrue({"pytest daemons/a", "pytest daemons/b", "eslint"} & sent)
        self.assertEqual(sorted(plan.local + [n for names in plan.remote.values() for n in names]), sorted(DURATIONS))

    def test_every_step_is_in_exactly_one_place(self):
        plan = self.plan(local_load=40.0)
        everything = plan.local + [n for names in plan.remote.values() for n in names]
        self.assertEqual(len(everything), len(set(everything)))

    def test_least_loaded_machine_gets_the_work(self):
        busy = lr.Probe("big", True, cores=8, load1=6.0, caps=FULL.caps)
        idle = lr.Probe("small", True, cores=8, load1=0.0, caps=FULL.caps)
        # The dev box itself is loaded (this is why --remote exists), so the step leaves it, for the idle machine.
        plan = self.plan(probes={"big": busy, "small": idle}, names=["pytest daemons/a"], local_load=40.0)
        self.assertEqual(list(plan.remote), ["small"])
        calm = self.plan(probes={"big": busy, "small": idle}, names=["pytest daemons/a"], local_load=0.0)
        self.assertEqual(list(calm.remote), ["small"], "a heavy step leaves an idle dev box too: it is shared")
        slow = lr.Probe("small", True, cores=8, load1=4.0, queued=3, caps=FULL.caps)
        stay = self.plan(probes={"small": slow}, names=["pytest daemons/a"], local_load=0.0)
        self.assertEqual(stay.remote, {}, "a machine that would finish much later than the dev box does not get it")

    def test_a_machine_above_its_load_ratio_or_unreachable_is_skipped_with_a_reason(self):
        overloaded = lr.Probe("big", True, cores=8, load1=7.5, caps=FULL.caps)
        down = lr.Probe("small", False, error="timed out")
        plan = self.plan(probes={"big": overloaded, "small": down})
        self.assertEqual(plan.remote, {})
        text = " ".join(plan.notes)
        self.assertIn("busy", text)
        self.assertIn("unreachable", text)

    def test_a_machine_without_the_tools_gets_nothing_it_cannot_run(self):
        bare = lr.Probe("big", True, cores=8, load1=0.0, caps=frozenset({"python3", "git", "tar"}))
        plan = self.plan(probes={"big": bare}, names=["pytest daemons/a", "check_x", "eslint"])
        self.assertEqual(plan.remote, {})

    def test_named_machine_and_refusals(self):
        plan = self.plan("big", probes={"big": FULL}, names=["pytest daemons/a", "check_x", "check_summation_bias"])
        self.assertEqual(plan.remote["big"], ["pytest daemons/a", "check_x"])  # a named machine takes cheap steps too
        self.assertEqual(plan.local, ["check_summation_bias"])
        for refused in ("hams1", "winbox"):
            with self.assertRaises(SystemExit) as caught:
                self.plan(refused)
            self.assertIn("not a lint runner", str(caught.exception))

    def test_runners_never_include_production_or_windows(self):
        self.assertEqual(set(lr.runners(INVENTORY)), {"big", "small"})
        real = lr.load_inventory()
        self.assertNotIn("hams1", lr.runners(real))
        self.assertNotIn("Bruce_Thinkpad", lr.runners(real))
        self.assertNotIn("bruce-yoga-win", lr.runners(real))
        self.assertTrue(real["hams1"]["never"])

    def test_where_output_names_the_hosts_the_steps_and_the_machines_that_are_not_runners(self):
        plan = self.plan()
        text = lr.format_plan(plan, INVENTORY, {"big": FULL, "small": FULL})
        self.assertIn("devbox", text)
        self.assertIn("not a runner: hams1: PRODUCTION", text)
        self.assertIn("not a runner: winbox", text)


WFULL = lr.Probe("m", True, cores=8, load1=0.5, caps=FULL.caps | {"eslint"})


class WholeRunPlanTests(unittest.TestCase):
    """2026-11-03: a run that would queue for the dev box's lock ships whole to an idle machine."""

    NAMES = ["pytest daemons/a", "pytest daemons/b", "check_x", "eslint", "check_summation_bias"]

    def plan(self, probes, names=None, whole=True, exclude=(), inventory=None):
        return lr.make_plan(names or self.NAMES, set(), DURATIONS, inventory or INVENTORY, probes, "auto", 16, 40.0,
                            whole=whole, exclude=exclude)

    def test_lock_busy_and_a_machine_idle_ships_every_runnable_step_even_a_short_one(self):
        plan = self.plan({"big": WFULL, "small": WFULL})
        self.assertEqual(len(plan.remote), 1, "one machine takes the run, not both")
        (host, names), = plan.remote.items()
        self.assertEqual(names, ["pytest daemons/a", "pytest daemons/b", "check_x", "eslint"])
        self.assertEqual(plan.local, ["check_summation_bias"], "only the step that needs this checkout stays")
        self.assertIn("whole run", " ".join(plan.notes))

    def test_spreading_still_keeps_short_steps_here_when_the_lock_is_free(self):
        plan = self.plan({"big": WFULL, "small": WFULL}, whole=False)
        self.assertIn("check_x", plan.local)

    def test_the_less_loaded_machine_with_the_same_tools_is_chosen(self):
        busy = lr.Probe("big", True, cores=8, load1=3.0, caps=WFULL.caps)
        idle = lr.Probe("small", True, cores=8, load1=0.2, caps=WFULL.caps)
        self.assertEqual(list(self.plan({"big": busy, "small": idle}).remote), ["small"])

    def test_the_machine_that_can_run_the_most_wins_over_the_idler_one(self):
        no_node = lr.Probe("big", True, cores=8, load1=0.0, caps=frozenset({"python3", "pytest", "flake8"}))
        full = lr.Probe("small", True, cores=8, load1=2.0, caps=WFULL.caps)
        plan = self.plan({"big": no_node, "small": full})
        self.assertEqual(list(plan.remote), ["small"])

    def test_a_machine_with_a_run_in_progress_or_in_line_is_not_used_so_all_busy_queues_as_before(self):
        held = lr.Probe("big", True, cores=8, load1=1.0, caps=WFULL.caps, held=True)
        queued = lr.Probe("small", True, cores=8, load1=0.1, queued=1, caps=WFULL.caps)
        plan = self.plan({"big": held, "small": queued})
        self.assertEqual(plan.remote, {})
        self.assertEqual(plan.local, self.NAMES, "every step stays here: the run queues for the lock as it always did")
        self.assertIn("busy", " ".join(plan.notes))

    def test_a_claimed_machine_is_skipped_and_the_other_one_is_used(self):
        plan = self.plan({"big": WFULL, "small": WFULL}, exclude={"big"})
        self.assertEqual(list(plan.remote), ["small"])
        none = self.plan({"big": WFULL, "small": WFULL}, exclude={"big", "small"})
        self.assertEqual(none.remote, {})
        self.assertIn("claimed", " ".join(none.notes))

    def test_unreachable_machines_leave_everything_here(self):
        down = lr.Probe("big", False, error="timed out")
        plan = self.plan({"big": down, "small": lr.Probe("small", False, error="no route")})
        self.assertEqual((plan.remote, plan.local), ({}, self.NAMES))

    def test_steps_the_chosen_machine_cannot_run_go_to_the_other_only_when_they_add_up(self):
        py_only = lr.Probe("big", True, cores=8, load1=0.0, caps=frozenset({"python3", "pytest", "flake8"}))
        js_only = lr.Probe("small", True, cores=8, load1=0.0, caps=frozenset({"python3", "node", "eslint"}))
        plan = self.plan({"big": py_only, "small": js_only}, names=["pytest daemons/a", "pytest daemons/b", "eslint"])
        self.assertEqual(plan.remote, {"big": ["pytest daemons/a", "pytest daemons/b"], "small": ["eslint"]})
        # a few seconds of node work do not justify a second machine and a second sync
        durations = dict(DURATIONS, check_js_syntax=3.0)
        short = lr.make_plan(["pytest daemons/a", "pytest daemons/b", "check_js_syntax"], set(), durations, INVENTORY,
                             {"big": py_only, "small": js_only}, "auto", 16, 40.0, whole=True)
        self.assertEqual(list(short.remote), ["big"])
        self.assertEqual(short.local, ["check_js_syntax"])

    def test_every_step_is_placed_exactly_once(self):
        plan = self.plan({"big": WFULL, "small": WFULL})
        everything = plan.local + [n for names in plan.remote.values() for n in names]
        self.assertEqual(sorted(everything), sorted(self.NAMES))

    def test_where_shows_a_lock_held_on_a_machine(self):
        held = lr.Probe("big", True, cores=8, load1=1.0, caps=WFULL.caps, held=True)
        text = lr.format_plan(self.plan({"big": held}), INVENTORY, {"big": held})
        self.assertIn("run lock held", text)

    def test_the_probe_reads_held_and_counts_only_live_tickets(self):
        probe = lr.parse_probe("m", "os=Linux\ncores=4\nload1=0.1\npyver=3.13\nhas_git=1\nhas_tar=1\nqueued=2\nheld=1\n")
        self.assertTrue(probe.held)
        self.assertEqual(probe.queued, 2)
        self.assertFalse(lr.parse_probe("m", "os=Linux\nheld=0\n").held)

    def test_the_probe_script_counts_tickets_of_live_processes_and_the_flock(self):
        # Run the real probe fragment against a fake home: a dead ticket does not count, a held flock does.
        home = tempfile.mkdtemp(prefix="probe-home-")
        self.addCleanup(shutil.rmtree, home, True)
        queue = os.path.join(home, ".cache", "hams-lint", "run.lock.queue.d")
        os.makedirs(queue)
        write(os.path.join(queue, "0000000001-%d-5" % os.getpid()), "{}")
        write(os.path.join(queue, "0000000002-999999999-5"), "{}")
        write(os.path.join(queue, "holder-%d-5" % os.getpid()), "{}")
        lock = os.path.join(home, ".cache", "hams-lint", "run.lock")
        write(lock, "")

        def probe():
            res = subprocess.run(["bash", "-c", lr.PROBE_SCRIPT], capture_output=True, text=True, env=dict(os.environ, HOME=home))
            return lr.parse_probe("m", res.stdout)

        free = probe()
        self.assertEqual((free.queued, free.held), (1, False))
        holder = subprocess.Popen(["flock", lock, "sleep", "30"])
        self.addCleanup(holder.kill)
        deadline = time.time() + 10
        while time.time() < deadline and not probe().held:
            time.sleep(0.1)
        self.assertTrue(probe().held)


class DispatchWholeRunTests(unittest.TestCase):
    """dispatch(): the decision with claims, fallbacks and the unchanged old path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lintdispatch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        open_repo = os.path.join(self.tmp, "hams_open")
        os.makedirs(open_repo)
        self.roots = {"hams_open": open_repo, "hams_com": os.path.join(self.tmp, "hams_com"), "hams_shared": os.path.join(self.tmp, "shared")}
        self.steps = [lint_exec.Step(name=n, argv=["/usr/bin/true"]) for n in ("pytest daemons/a", "check_x", "check_summation_bias")]
        self.dicts = {s.name: {"name": s.name, "argv": s.argv, "cwd": None, "env_delta": {}} for s in self.steps}
        self.started = []
        self.out = []
        self.patch = mock.patch.object(lr.RemoteJob, "start", lambda job: (self.started.append(job.host), job)[1])
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def dispatch(self, probes, whole, claim=None, claimed=None):
        transports = {n: object() for n in ("big", "small")}
        return lr.dispatch(
            "auto", self.steps, [s.name for s in self.steps], DURATIONS, self.dicts, self.roots, "hams_open", 16, 40.0,
            inventory=INVENTORY, transports=transports, probes=probes, out=self.out.append, whole=whole,
            claim=claim, claimed=claimed,
        )

    def test_lock_busy_and_a_machine_idle_sends_the_run_there_and_keeps_only_local_steps(self):
        claims = []
        local, jobs = self.dispatch({"big": WFULL, "small": WFULL}, True, claim=lambda host: claims.append(host) or io.StringIO(),
                                    claimed=lambda hosts: set())
        self.assertEqual([s.name for s in local], ["check_summation_bias"])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(claims, [jobs[0].host])
        self.assertIsNotNone(jobs[0].claim)

    def test_a_machine_claimed_between_probe_and_choice_is_replaced_by_the_other(self):
        tried = []

        def claim(host):
            tried.append(host)
            return None if len(tried) == 1 else io.StringIO()

        local, jobs = self.dispatch({"big": WFULL, "small": WFULL}, True, claim=claim, claimed=lambda hosts: set())
        self.assertEqual(len(tried), 2)
        self.assertNotEqual(tried[0], tried[1])
        self.assertEqual([j.host for j in jobs], [tried[1]])

    def test_everything_claimed_or_busy_queues_here_as_before(self):
        local, jobs = self.dispatch({"big": WFULL, "small": WFULL}, True, claim=lambda host: None, claimed=lambda hosts: set(hosts))
        self.assertEqual((len(local), jobs), (3, []))
        self.assertTrue(any("queues here" in line or "claimed" in line for line in self.out), self.out)

    def test_unreachable_machines_fall_back_to_a_local_run(self):
        down = lr.Probe("x", False, error="timed out")
        local, jobs = self.dispatch({"big": down, "small": down}, True, claim=lambda host: io.StringIO(), claimed=lambda hosts: set())
        self.assertEqual((len(local), jobs), (3, []))

    def test_without_whole_the_old_spreading_plan_is_used_and_a_lost_claim_does_not_stop_it(self):
        local, jobs = self.dispatch({"big": WFULL, "small": WFULL}, False, claim=lambda host: None)
        self.assertEqual([s.name for s in local], ["check_x", "check_summation_bias"])
        self.assertEqual([j.names for j in jobs], [["pytest daemons/a"]])
        self.assertIsNone(jobs[0].claim)

    def test_a_job_releases_its_claim_when_it_ends(self):
        handle = io.StringIO()
        bundle = {"bundle_id": "b", "steps": [{"name": "x"}]}
        job = lr.RemoteJob("big", {}, object(), {}, "hams_open", bundle, lambda _l: None, threading.Lock(), claim=handle)
        with mock.patch.object(lr, "run_on_machine", return_value=("ok", [], {})):
            job._run()
        self.assertTrue(handle.closed)
        self.assertEqual(job.status, "ok")


class UnfitTests(unittest.TestCase):
    def test_a_step_that_failed_on_a_machine_and_passed_here_is_not_sent_there_again(self):
        path = os.path.join(tempfile.mkdtemp(prefix="unfit-"), "u.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        lr.record_unfit("big", ["pytest daemons/a"], path=path, now=1000.0)
        self.assertEqual(lr.load_unfit(path, now=1500.0), {"big": {"pytest daemons/a"}})
        self.assertEqual(lr.load_unfit(path, now=1000.0 + lr.UNFIT_TTL_SECONDS + 1), {}, "entries expire")
        self.assertEqual(lr.load_unfit(os.path.join(os.path.dirname(path), "none.json")), {})
        plan = lr.make_plan(["pytest daemons/a", "pytest daemons/b"], set(), DURATIONS, INVENTORY, {"big": FULL, "small": FULL},
                            "auto", 16, 40.0, whole=True, unfit={"big": {"pytest daemons/a"}, "small": {"pytest daemons/a"}})
        self.assertNotIn("pytest daemons/a", [n for ns in plan.remote.values() for n in ns])
        self.assertIn("pytest daemons/a", plan.local)

    def test_collect_reports_steps_confirmed_here_to_the_callback(self):
        class Job:
            host, status, names, problems, seconds = "m", "failed", ["a", "b"], ["step failed: a", "step failed: b"], {}

            def join(self):
                pass

        steps = [lint_exec.Step(name=n, argv=["/usr/bin/true"]) for n in ("a", "b")]
        seen = []
        lr.collect([Job()], steps, lambda wanted: [lint_exec.Result(s.name, 1 if s.name == "b" else 0, "", 0.1) for s in wanted],
                   lint_exec.Result, out=lambda _l: None, on_unfit=lambda host, names: seen.append((host, names)))
        self.assertEqual(seen, [("m", ["a"])], "b fails here too: a real failure, not the machine's fault")

    def test_a_system_program_missing_on_the_machine_is_found_on_its_path(self):
        bundle = {"bundle_id": "x", "steps": [{"name": "f", "argv": ["/usr/bin/definitely-not-here-python3", "-V"]},
                                              {"name": "g", "argv": ["/usr/bin/python3", "-V"]}]}
        steps = lr.import_bundle(bundle, {})
        self.assertEqual(steps[0].argv[0], "/usr/bin/definitely-not-here-python3", "unknown programs are left alone")
        elsewhere = os.path.join(tempfile.gettempdir(), "userbin", "definitely-not-here-python3")
        with mock.patch.object(lr.shutil, "which", return_value=elsewhere):
            self.assertEqual(lr.import_bundle(bundle, {})[0].argv[0], elsewhere)
        self.assertEqual(steps[1].argv[0], "/usr/bin/python3")


class ExportTests(unittest.TestCase):
    def test_paths_become_placeholders_and_back(self):
        roots = {"hams_com": "/w/hams_com", "hams_open": "/w/hams_open", "hams_shared": "/w/hams_open/hams_shared"}
        step = {"name": "s", "argv": ["/usr/bin/python3", "/w/hams_open/hams_shared/tools/c.py", "/w/hams_com"], "cwd": "/w/hams_com/daemons/x",
                "env_delta": {"PYTHONPATH": "/w/hams_com/daemons:/export/u/x"}, "lock": None, "print_ok": True}
        item = lr.export_step(step, roots, home="/export/u")
        self.assertEqual(item["argv"][1], "@HAMS_SHARED@/tools/c.py")
        self.assertEqual(item["cwd"], "@HAMS_COM@/daemons/x")
        self.assertEqual(item["env_delta"]["PYTHONPATH"], "@HAMS_COM@/daemons:@HOME@/x")
        steps = lr.import_bundle({"steps": [item]}, {"hams_com": "/r/hams_com", "hams_open": "/r/hams_open", "hams_shared": "/r/hams_open/hams_shared"}, home="/h")
        self.assertEqual(steps[0].argv[1], "/r/hams_open/hams_shared/tools/c.py")
        self.assertEqual(steps[0].cwd, "/r/hams_com/daemons/x")
        self.assertEqual(steps[0].env["PYTHONPATH"], "/r/hams_com/daemons:/h/x")

    def test_a_step_that_still_names_a_local_path_or_local_interpreter_stays_here(self):
        roots = {"hams_com": "/w/hams_com", "hams_open": "/w/hams_open", "hams_shared": "/w/hams_open/hams_shared"}
        leaking = {"name": "s", "argv": ["/usr/bin/python3", "/mnt/workspace/other_checkout/x.py"], "cwd": None, "env_delta": {}}
        self.assertIsNone(lr.export_step(leaking, roots, home="/export/u"))
        in_home = {"name": "s", "argv": ["/usr/bin/python3", "/export/u/.cache/thing"], "cwd": None, "env_delta": {}}
        self.assertEqual(lr.export_step(in_home, roots, home="/export/u")["argv"][1], "@HOME@/.cache/thing")
        venv = {"name": "s", "argv": ["/w/hams_com/daemons/b/.venv/bin/python3", "x"], "cwd": None, "env_delta": {}}
        self.assertIsNone(lr.export_step(venv, roots, home="/export/u"))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lintremote-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.dest = os.path.join(self.tmp, "remote", "repo")
        os.makedirs(os.path.dirname(self.dest))
        make_repo(self.repo, {"a.txt": "one\n", "d/b.txt": "two\n", "keep.txt": "keep\n"})
        self.transport = lr.ShellTransport("fake", home=self.tmp)
        self.lines = []

    def sync(self):
        return lr.sync_repo(self.transport, self.repo, self.dest, log=self.lines.append)

    def read(self, name):
        with open(os.path.join(self.dest, name), encoding="utf-8") as handle:
            return handle.read()

    def test_full_then_incremental_sync_matches_the_commit(self):
        info = self.sync()
        self.assertFalse(info["dirty"])
        self.assertEqual(self.read("d/b.txt"), "two\n")
        write(os.path.join(self.repo, "a.txt"), "one-changed\n")
        os.unlink(os.path.join(self.repo, "keep.txt"))
        write(os.path.join(self.repo, "new.txt"), "new\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "second")
        self.lines.clear()
        info = self.sync()
        self.assertTrue(any("2 changed, 1 removed" in line for line in self.lines), self.lines)
        self.assertEqual(self.read("a.txt"), "one-changed\n")
        self.assertEqual(self.read("new.txt"), "new\n")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "keep.txt")))
        self.assertEqual(info["sha"], lr.head_sha(self.repo))
        manifest = json.loads(self.read(".hams_lint_sync"))
        self.assertEqual(manifest["sha"], info["sha"])

    def test_uncommitted_files_are_shipped_flagged_and_reverted_on_the_next_sync(self):
        self.sync()
        write(os.path.join(self.repo, "a.txt"), "dirty\n")
        write(os.path.join(self.repo, "untracked.txt"), "u\n")
        info = self.sync()
        self.assertTrue(info["dirty"])
        self.assertEqual(self.read("a.txt"), "dirty\n")
        self.assertEqual(self.read("untracked.txt"), "u\n")
        git(self.repo, "checkout", "-q", "--", "a.txt")
        os.unlink(os.path.join(self.repo, "untracked.txt"))
        info = self.sync()
        self.assertFalse(info["dirty"])
        self.assertEqual(self.read("a.txt"), "one\n")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "untracked.txt")))

    def test_files_marked_export_ignore_are_still_mirrored(self):
        # git archive drops them; 300+ tracked hams_local_relay files (claims/, fuzz/, a test suite) went missing
        write(os.path.join(self.repo, ".gitattributes"), "private.txt export-ignore\nsub/** export-ignore\n")
        write(os.path.join(self.repo, "private.txt"), "tracked but export-ignored\n")
        write(os.path.join(self.repo, "sub", "inner.txt"), "inner\n")
        git(self.repo, "add", ".gitattributes", "private.txt", "sub/inner.txt")
        git(self.repo, "commit", "-q", "-m", "export-ignore")
        self.sync()
        self.assertEqual(self.read("private.txt"), "tracked but export-ignored\n")
        self.assertEqual(self.read("sub/inner.txt"), "inner\n")
        write(os.path.join(self.repo, "sub", "inner2.txt"), "inner2\n")
        git(self.repo, "add", "sub/inner2.txt")
        git(self.repo, "commit", "-q", "-m", "more")
        self.sync()
        self.assertEqual(self.read("sub/inner2.txt"), "inner2\n")

    def test_secrets_symlinks_and_build_output_are_never_shipped(self):
        self.sync()
        write(os.path.join(self.repo, ".env"), "SECRET=1\n")
        write(os.path.join(self.repo, "deploy.key"), "k\n")
        write(os.path.join(self.repo, "target", "debug", "x"), "bin\n")
        write(os.path.join(self.repo, ".secrets", "tok"), "t\n")
        os.symlink("/etc/passwd", os.path.join(self.repo, "link"))
        write(os.path.join(self.repo, "fine.txt"), "ok\n")
        self.sync()
        for name in (".env", "deploy.key", "target/debug/x", ".secrets/tok", "link"):
            self.assertFalse(os.path.lexists(os.path.join(self.dest, name)), name)
        self.assertEqual(self.read("fine.txt"), "ok\n")

    def test_the_mirror_gets_a_git_repository_for_checkers_that_list_files(self):
        self.sync()
        lr.commit_mirror(self.transport, self.dest)
        listed = subprocess.run(["git", "-C", self.dest, "ls-files"], capture_output=True, text=True).stdout.split()
        self.assertIn("d/b.txt", listed)
        self.assertNotIn(".hams_lint_sync", listed)

    def test_a_nested_repository_is_a_gitlink_of_the_outer_mirror_not_a_pile_of_its_files(self):
        self.sync()
        inner = os.path.join(self.dest, "shared")
        os.makedirs(inner)
        write(os.path.join(inner, "lib.rs"), "fn main() {}\n")
        # An older run committed the inner files into the outer repository.
        lr.commit_mirror(self.transport, self.dest)
        self.assertIn("shared/lib.rs", subprocess.run(["git", "-C", self.dest, "ls-files"], capture_output=True, text=True).stdout.split())
        lr.commit_mirror(self.transport, inner)
        lr.commit_mirror(self.transport, self.dest, nested=("shared",))
        listed = subprocess.run(["git", "-C", self.dest, "ls-files", "-s"], capture_output=True, text=True).stdout
        self.assertNotIn("shared/lib.rs", listed)
        self.assertIn("160000", listed)
        self.assertIn("d/b.txt", listed)
        self.assertEqual(subprocess.run(["git", "-C", inner, "ls-files"], capture_output=True, text=True).stdout.split(), ["lib.rs"])


class VerifyTests(unittest.TestCase):
    def test_results_must_cover_every_step_pass_and_match_the_run(self):
        good = json.dumps({"bundle_id": "b1", "steps": [{"name": "a", "returncode": 0}, {"name": "b", "returncode": 0}]})
        self.assertEqual(lr.verify_results(good, ["a", "b"], "b1"), (True, []))
        self.assertFalse(lr.verify_results(good, ["a", "b", "c"], "b1")[0])
        self.assertFalse(lr.verify_results(good, ["a"], "other")[0])
        failed = json.dumps({"bundle_id": "b1", "steps": [{"name": "a", "returncode": 1}]})
        self.assertEqual(lr.verify_results(failed, ["a"], "b1"), (False, ["step failed: a"]))
        self.assertFalse(lr.verify_results("", ["a"], "b1")[0])
        self.assertFalse(lr.verify_results("{not json", ["a"], "b1")[0])


FAKE_SSH = textwrap.dedent(
    """\
    #!/bin/sh
    # Stands in for ssh: runs the last argument (the remote command) in a shell with HOME set to a
    # directory that plays the remote machine's home. Refuses to run if BatchMode was not requested.
    case "$*" in *BatchMode=yes*) ;; *) echo "ssh without BatchMode" >&2; exit 97;; esac
    for last; do :; done
    HOME="$FAKE_REMOTE_HOME" exec sh -c "$last"
    """
)


class EndToEndTests(unittest.TestCase):
    """The whole path through the real SshTransport, with a fake `ssh` that runs the command locally in
    a separate HOME: sync of three repositories, bundle, remote run_linters --run-bundle, results."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lintremote-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        fake = os.path.join(bin_dir, "ssh")
        write(fake, FAKE_SSH)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        self.remote_home = os.path.join(self.tmp, "remote_home")
        os.makedirs(self.remote_home)
        self.env_patch = {"PATH": bin_dir + os.pathsep + os.environ["PATH"], "FAKE_REMOTE_HOME": self.remote_home}
        self.saved = {k: os.environ.get(k) for k in self.env_patch}
        os.environ.update(self.env_patch)
        self.addCleanup(self.restore)
        self.com = os.path.join(self.tmp, "local", "hams_com")
        self.open = os.path.join(self.tmp, "local", "hams_open")
        self.shared = os.path.join(self.tmp, "local", "shared")
        make_repo(self.com, {"c.txt": "c\n"})
        make_repo(self.open, {"zero_sudo/__manifest__.py": "{}\n", "o.txt": "o\n"})
        make_repo(self.shared, {"README": "shared\n"})
        os.makedirs(os.path.join(self.shared, "tools"))
        for name in ("run_linters.py", "lint_exec.py", "lint_lock.py", "lock_queue.py", "lint_remote.py", "lint_cache.py", "lint_test_durations.py",
                     "lint_batch.py", "batch_queue.py"):
            shutil.copy(os.path.join(TOOLS, name), os.path.join(self.shared, "tools", name))
        git(self.shared, "add", "-A")
        git(self.shared, "commit", "-q", "-m", "tools")
        self.roots = {"hams_com": self.com, "hams_open": self.open, "hams_shared": self.shared}
        self.machine = {"ssh": {"target": "fake"}, "workdir": "~/mirror", "jobs": 2, "nice": 10}
        self.transport = lr.SshTransport("fake", self.machine)
        self.lines = []

    def restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def bundle(self, *steps):
        items = []
        for name, argv in steps:
            items.append({"name": name, "argv": argv, "cwd": None, "env_delta": {}, "lock": None, "print_ok": True})
        return {"bundle_id": "e2e-1", "steps": items}

    def run_remote(self, bundle):
        env_keep = {k: os.environ.pop(k, None) for k in ("XDG_CACHE_HOME", "HAMS_LINT_LOAD_LIMIT")}
        os.environ["HAMS_SKIP_CI_LOAD_GATE"] = "1"
        try:
            return lr.run_on_machine(
                "fake", self.machine, self.transport, self.roots, "hams_com", bundle, self.lines.append, log=self.lines.append
            )
        finally:
            os.environ.pop("HAMS_SKIP_CI_LOAD_GATE", None)
            for key, value in env_keep.items():
                if value is not None:
                    os.environ[key] = value

    def test_a_passing_run_syncs_runs_on_the_other_machine_and_is_verified(self):
        status, problems, seconds = self.run_remote(
            self.bundle(("say", ["/usr/bin/python3", "-c", "print('hello from', __import__('os').getcwd() and 'remote')"]))
        )
        self.assertEqual((status, problems), ("ok", []), "\n".join(self.lines))
        self.assertIn("say", seconds)
        self.assertTrue(any("hello from remote" in line for line in self.lines), self.lines)
        self.assertTrue(os.path.isfile(os.path.join(self.remote_home, "mirror", "hams_open", "hams_shared", "tools", "run_linters.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.remote_home, "mirror", "hams_com", "c.txt")))
        manifest = json.load(open(os.path.join(self.remote_home, "mirror", "hams_com", ".hams_lint_sync"), encoding="utf-8"))
        self.assertEqual(manifest["sha"], lr.head_sha(self.com))

    def test_a_machine_whose_run_lock_is_stuck_does_not_hang_the_run_it_falls_back(self):
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, %r); import lint_lock; "
             "l = lint_lock.RunLock({'XDG_CACHE_HOME': %r}); assert l.acquire('stuck'); print('held', flush=True); time.sleep(120)"
             % (TOOLS, os.path.join(self.remote_home, ".cache"))],
            stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        os.environ["HAMS_LINT_REMOTE_WAIT"] = "2"
        self.addCleanup(os.environ.pop, "HAMS_LINT_REMOTE_WAIT", None)
        # when this suite itself runs under a lint run, the "remote" run would inherit the parent's lock marker
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("HAMS_LINT_LOCK_HELD", None)
        started = time.monotonic()
        status, problems, _ = self.run_remote(self.bundle(("say", ["/usr/bin/python3", "-c", "print(1)"])))
        self.assertEqual(status, "infra", "\n".join(self.lines))
        self.assertLess(time.monotonic() - started, 60)
        self.assertTrue(any("gave up waiting for the lint run lock" in line for line in self.lines), self.lines)

    def test_remote_wait_arguments(self):
        self.assertEqual(lr.remote_wait_args({}), "--lock-timeout 120 --load-timeout 120")
        self.assertEqual(lr.remote_wait_args({"HAMS_LINT_REMOTE_WAIT": "30"}), "--lock-timeout 30 --load-timeout 30")
        self.assertEqual(lr.remote_wait_args({"HAMS_LINT_REMOTE_WAIT": "nan"}), "--lock-timeout 120 --load-timeout 120")

    def test_a_failing_step_is_a_real_failure_not_a_fallback(self):
        status, problems, _seconds = self.run_remote(
            self.bundle(("good", ["/usr/bin/python3", "-c", "pass"]), ("bad", ["/usr/bin/python3", "-c", "import sys; print('boom'); sys.exit(3)"]))
        )
        self.assertEqual(status, "failed", "\n".join(self.lines))
        self.assertEqual(problems, ["step failed: bad"])
        self.assertTrue(any("boom" in line for line in self.lines))

    def test_a_machine_that_cannot_be_reached_is_an_infrastructure_problem_so_steps_run_here(self):
        os.environ["PATH"] = "/nonexistent-bin:" + os.environ["PATH"].split(os.pathsep, 1)[1]  # no fake ssh first
        broken = lr.SshTransport("down", {"ssh": {"target": "nowhere.invalid", "args": ["-o", "ConnectTimeout=2"]}}, timeout=3)
        status, problems, _ = lr.run_on_machine(
            "down", self.machine, broken, self.roots, "hams_com", self.bundle(("x", ["/usr/bin/true"])), self.lines.append, log=self.lines.append
        )
        self.assertEqual(status, "infra")
        self.assertTrue(problems)

    def test_collect_runs_infra_steps_here_and_credits_only_machines_that_did_the_work(self):
        class Job:
            def __init__(self, host, status, names, problems=()):
                self.host, self.status, self.names, self.problems, self.seconds = host, status, names, list(problems), {}

            def join(self):
                pass

        steps = [lint_exec.Step(name=n, argv=["/usr/bin/true"]) for n in ("a", "b", "c", "d")]
        ran_here = []

        def execute_here(wanted):
            ran_here.extend(s.name for s in wanted)
            return [lint_exec.Result(s.name, 1 if s.name == "d" else 0, "", 0.1) for s in wanted]

        jobs = [Job("m1", "ok", ["a"]), Job("m2", "infra", ["b"], ["sync failed"]), Job("m3", "failed", ["c", "d"], ["step failed: c", "step failed: d"])]
        results, hosts = lr.collect(jobs, steps, execute_here, lint_exec.Result, out=lambda _l: None)
        # b: the machine could not work; c and d failed there and are run again here (the reference environment)
        self.assertEqual(ran_here, ["b", "c", "d"])
        self.assertEqual(hosts, ["m1"])
        by_name = {r.name: r.returncode for r in results}
        self.assertEqual(by_name, {"a": 0, "b": 0, "c": 0, "d": 1})

    def test_stamp_metadata_names_the_host_and_the_gate_still_parses_it(self):
        import lint_stamp_gate

        text = run_linters.stamp_text("changed", ["flake8", "pytest tools/[1of4]"], ["mac1-vm", "bruce-yoga"])
        self.assertIn("hosts=bruce-yoga,mac1-vm", text)
        mode, count, names = lint_stamp_gate.parse_stamp(text)
        self.assertEqual((mode, count), ("changed", 2))
        self.assertEqual(names, {"flake8", "pytest"})
        self.assertNotIn("hosts=", run_linters.stamp_text("full", ["flake8"]))


class ProvisionTests(unittest.TestCase):
    def test_the_non_interactive_ssh_command_puts_user_level_toolchains_on_the_path(self):
        transport = lr.SshTransport("m", {"ssh": {"target": "ai@h", "args": []}})
        command = transport.argv("cargo --version")[-1]
        self.assertTrue(command.startswith('export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"; '))
        self.assertTrue(command.endswith("cargo --version"))

    def test_the_rust_pin_is_read_from_the_toolchain_files(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, lr.TOOLCHAIN_FILES[0])
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('[toolchain]\nchannel = "9.9.9"\n')
            self.assertEqual(lr.rust_pin([root]), "9.9.9")
            self.assertIsNone(lr.rust_pin([root + "/none"]))

    def test_the_provision_script_plans_without_changing_anything_and_pins_every_requirement(self):
        res = subprocess.run(["bash", lr.PROVISION_SCRIPT, "plan"], capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("apt packages to install", res.stdout)
        self.assertIn("undo:", res.stdout)
        bad = subprocess.run(["bash", lr.PROVISION_SCRIPT, "bogus"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        with open(lr.PROVISION_REQUIREMENTS, encoding="utf-8") as handle:
            text = handle.read()
        pins = [line for line in text.splitlines() if "==" in line and not line.startswith("#")]
        self.assertGreaterEqual(len(pins), 10)
        # every pin carries at least one hash (a compiled package carries one per architecture) and nothing is unpinned
        blocks = re.findall(r"^\S+==\S+ \\\n((?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", text, re.M)
        self.assertEqual(len(blocks), len(pins))
        self.assertIn("pytest-xdist==", text)


class StepCostEstimateTests(unittest.TestCase):
    """The router's cost of check_cargo_clippy is the measured warm figure, not 600 s and not the cold-dominated median."""

    def test_clippy_is_estimated_at_the_warm_figure_whatever_the_cold_history_says(self):
        self.assertEqual(lr._est("check_cargo_clippy", {"check_cargo_clippy": 900.0}), lr.WARM_COST_SECONDS["check_cargo_clippy"])
        self.assertEqual(lr._est("check_cargo_clippy", {}), lr.WARM_COST_SECONDS["check_cargo_clippy"])
        self.assertLessEqual(lr.WARM_COST_SECONDS["check_cargo_clippy"], 60.0)

    def test_other_steps_use_their_recorded_time_or_the_unknown_default(self):
        self.assertEqual(lr._est("flake8", {"flake8": 12.0}), 12.0)
        self.assertEqual(lr._est("check_cargo_fmt", {"check_cargo_fmt": 3.0}), 3.0)
        self.assertEqual(lr._est("never_measured", {}), lr.UNKNOWN_SECONDS)


if __name__ == "__main__":
    unittest.main()
