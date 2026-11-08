#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for ram_dirs.py: scratch directories in RAM with a loud fallback, an owner marker, a sweep of dead jobs'
directories and a per-job guard (Bruce, 2026-11-08). The jobs here are real subprocesses; the "RAM root" is a
temporary directory named by HAMS_RAM_ROOT (the module's own switch for another tmpfs), and free space and memory
size are the only things replaced, because a test cannot fill a real /dev/shm.

Tests [@ANCHOR: ram_dirs:RamPolicy]
Tests [@ANCHOR: ram_dirs:policy]
Tests [@ANCHOR: ram_dirs:Placement]
Tests [@ANCHOR: ram_dirs:place]
Tests [@ANCHOR: ram_dirs:cargo_target_dir]
Tests [@ANCHOR: ram_dirs:job_directories]
Tests [@ANCHOR: ram_dirs:test_tmp_base]
Tests [@ANCHOR: ram_dirs:lint_cargo_base]
Tests [@ANCHOR: ram_dirs:claim]
Tests [@ANCHOR: ram_dirs:owner_state]
Tests [@ANCHOR: ram_dirs:sweep]
Tests [@ANCHOR: ram_dirs:guard_verdict]
Tests [@ANCHOR: ram_dirs:JobResult]
Tests [@ANCHOR: ram_dirs:run_job]
Tests [@ANCHOR: ram_dirs:total_ram_gb]
Tests [@ANCHOR: ram_dirs:is_tmpfs]
Tests [@ANCHOR: ram_dirs:free_gb]
Tests [@ANCHOR: ram_dirs:warn_once]
Tests [@ANCHOR: ram_dirs:under_ram_root]
Tests [@ANCHOR: ram_dirs:check_job_name]
Tests [@ANCHOR: ram_dirs:start_ticks]
Tests [@ANCHOR: ram_dirs:newest_mtime]
Tests [@ANCHOR: ram_dirs:remove_tree]
Tests [@ANCHOR: ram_dirs:tree_bytes]
Tests [@ANCHOR: ram_dirs:stop_group]
Tests [@ANCHOR: ram_dirs:status_lines]
Tests [@ANCHOR: ram_dirs:main]
Tests [@ANCHOR: ram_dirs:die_with_parent]
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import ram_dirs  # noqa: E402

SCRIPT = os.path.join(_HERE, "ram_dirs.py")


class RamTestCase(unittest.TestCase):
    def setUp(self):
        ram_dirs._warned.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "shm")
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.root)
        os.makedirs(self.home)
        self.environ = {"HAMS_RAM_ROOT": self.root, "HAMS_RAM_DIRS": "1", "XDG_CACHE_HOME": os.path.join(self.home, ".cache")}
        self.warnings = []

    def warn(self, key, text):
        self.warnings.append(text)

    def free(self, gb):
        return mock.patch.object(ram_dirs, "free_gb", return_value=gb)


class HelperTest(RamTestCase):
    # Tests [@ANCHOR: ram_dirs:total_ram_gb]
    def test_total_memory_is_read_from_proc(self):
        self.assertGreater(ram_dirs.total_ram_gb(), 0.1)

    # Tests [@ANCHOR: ram_dirs:is_tmpfs]
    def test_dev_shm_is_a_tmpfs_and_the_home_directory_is_not(self):
        self.assertTrue(ram_dirs.is_tmpfs("/dev/shm"))
        self.assertFalse(ram_dirs.is_tmpfs(_HERE))

    # Tests [@ANCHOR: ram_dirs:free_gb]
    def test_free_space_of_a_missing_path_is_that_of_its_nearest_parent(self):
        self.assertAlmostEqual(ram_dirs.free_gb(os.path.join(self.root, "a", "b")), ram_dirs.free_gb(self.root), delta=1.0)
        self.assertIsNone(ram_dirs.free_gb(""))

    # Tests [@ANCHOR: ram_dirs:warn_once]
    def test_a_warning_is_printed_once_per_key(self):
        lines = []

        class Sink:
            def write(self, text):
                lines.append(text)

            def flush(self):
                pass
        ram_dirs._warn("k", "first", out=Sink())
        ram_dirs._warn("k", "second", out=Sink())
        self.assertEqual("".join(lines).count("first"), 1)
        self.assertNotIn("second", "".join(lines))

    # Tests [@ANCHOR: ram_dirs:under_ram_root]
    def test_a_path_is_under_the_root_by_name(self):
        pol = ram_dirs.RamPolicy(root="/dev/shm")
        self.assertTrue(ram_dirs.under_ram_root("/dev/shm/hams-lint-cargo/x", pol))
        self.assertFalse(ram_dirs.under_ram_root("/dev/shmx/y", pol))
        self.assertFalse(ram_dirs.under_ram_root("/var/cache/z", pol))

    # Tests [@ANCHOR: ram_dirs:check_job_name]
    def test_a_good_name_is_returned_unchanged(self):
        self.assertEqual(ram_dirs._check_job_name("relay-clippy_2.x"), "relay-clippy_2.x")

    # Tests [@ANCHOR: ram_dirs:start_ticks]
    def test_start_ticks_of_this_process_is_a_number_and_of_a_gone_process_none(self):
        self.assertIsInstance(ram_dirs._start_ticks(os.getpid()), int)
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIsNone(ram_dirs._start_ticks(child.pid))

    # Tests [@ANCHOR: ram_dirs:newest_mtime]
    def test_the_newest_modification_time_counts_nested_files(self):
        os.makedirs(os.path.join(self.root, "d", "e"))
        path = os.path.join(self.root, "d", "e", "f")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.utime(path, (5000, 5000))
        for directory in (os.path.join(self.root, "d", "e"), os.path.join(self.root, "d")):
            os.utime(directory, (1000, 1000))
        self.assertEqual(ram_dirs.newest_mtime(os.path.join(self.root, "d")), 5000)
        self.assertEqual(ram_dirs.newest_mtime(os.path.join(self.root, "d"), depth=0), 1000)
        self.assertEqual(ram_dirs.newest_mtime(os.path.join(self.root, "missing")), 0.0)

    # Tests [@ANCHOR: ram_dirs:remove_tree]
    def test_a_tree_goes_and_a_link_is_only_unlinked(self):
        tree = os.path.join(self.root, "t")
        os.makedirs(os.path.join(tree, "x"))
        link = os.path.join(self.root, "l")
        os.symlink(tree, link)
        self.assertTrue(ram_dirs.remove_tree(link))
        self.assertTrue(os.path.isdir(tree))
        self.assertTrue(ram_dirs.remove_tree(tree))
        self.assertTrue(ram_dirs.remove_tree(tree))

    # Tests [@ANCHOR: ram_dirs:tree_bytes]
    def test_file_sizes_are_summed(self):
        os.makedirs(os.path.join(self.root, "s", "q"))
        for name, size in (("a", 10), ("q/b", 32)):
            with open(os.path.join(self.root, "s", name), "wb") as handle:
                handle.write(b"x" * size)
        self.assertEqual(ram_dirs.tree_bytes(os.path.join(self.root, "s")), 42)
        self.assertEqual(ram_dirs.tree_bytes(os.path.join(self.root, "none")), 0)

    # Tests [@ANCHOR: ram_dirs:stop_group]
    def test_a_process_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"],
                                 start_new_session=True, stdout=subprocess.PIPE, text=True)
        child.stdout.readline()
        ram_dirs._stop_group(child, 0.5)
        self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)

    # Tests [@ANCHOR: ram_dirs:status_lines]
    # Tests [@ANCHOR: ram_dirs:main]
    def test_status_lists_the_directories_with_their_owner_state(self):
        ram_dirs.claim(os.path.join(self.root, "hams-cargo-target-s1"))
        lines = ram_dirs.status_lines(environ=self.environ)
        self.assertIn("mode on", lines[0])
        self.assertTrue(any("hams-cargo-target-s1" in line and "alive" in line for line in lines), lines)
        out = subprocess.run([sys.executable, SCRIPT, "sweep"], env=dict(os.environ, **self.environ), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


class PolicyTest(RamTestCase):
    # Tests [@ANCHOR: ram_dirs:policy]
    def test_defaults(self):
        pol = ram_dirs.policy({})
        self.assertEqual((pol.mode, pol.root, pol.min_free_gb, pol.job_max_gb, pol.hard_free_gb, pol.require_tmpfs),
                         ("auto", "/dev/shm", 20.0, 30.0, 5.0, True))

    # Tests [@ANCHOR: ram_dirs:policy]
    def test_environment_overrides_and_bad_numbers_keep_the_default(self):
        pol = ram_dirs.policy({"HAMS_RAM_DIRS": "0", "HAMS_RAM_ROOT": "/x", "HAMS_RAM_MIN_FREE_GB": "7.5",
                               "HAMS_RAM_JOB_MAX_GB": "nan", "HAMS_RAM_HARD_FREE_GB": "-1"})
        self.assertEqual((pol.mode, pol.root, pol.min_free_gb, pol.job_max_gb, pol.hard_free_gb, pol.require_tmpfs),
                         ("off", "/x", 7.5, 30.0, 5.0, False))
        for word, mode in (("1", "on"), ("YES", "on"), ("off", "off"), ("", "auto"), ("maybe", "auto")):
            self.assertEqual(ram_dirs.policy({"HAMS_RAM_DIRS": word}).mode, mode)


class PlaceTest(RamTestCase):
    # Tests [@ANCHOR: ram_dirs:place]
    # Tests [@ANCHOR: ram_dirs:Placement]
    def test_room_in_ram_gives_the_ram_path_without_a_warning(self):
        with self.free(60):
            placed = ram_dirs.place("hams-cargo-target-a", "/ssd/a", environ=self.environ, warn=self.warn)
        self.assertEqual((placed.path, placed.in_ram), (os.path.join(self.root, "hams-cargo-target-a"), True))
        self.assertEqual(self.warnings, [])

    # Tests [@ANCHOR: ram_dirs:place]
    def test_under_the_minimum_falls_back_to_the_disk_and_says_so(self):
        with self.free(12.3):
            placed = ram_dirs.place("n", "/ssd/n", environ=self.environ, warn=self.warn)
        self.assertEqual((placed.path, placed.in_ram), ("/ssd/n", False))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("12.3 GB free", self.warnings[0])
        self.assertIn("20 GB minimum", self.warnings[0])
        self.assertIn("/ssd/n", self.warnings[0])

    # Tests [@ANCHOR: ram_dirs:place]
    def test_the_minimum_is_configurable_and_exact(self):
        environ = dict(self.environ, HAMS_RAM_MIN_FREE_GB="10")
        with self.free(10):
            self.assertTrue(ram_dirs.place("n", "/s", environ=environ, warn=self.warn).in_ram)
        with self.free(9.99):
            self.assertFalse(ram_dirs.place("n", "/s", environ=environ, warn=self.warn).in_ram)

    # Tests [@ANCHOR: ram_dirs:place]
    def test_a_missing_root_and_a_root_that_is_not_a_tmpfs_fall_back_loudly(self):
        missing = ram_dirs.place("n", "/s", pol=ram_dirs.RamPolicy(mode="on", root=os.path.join(self.root, "nope")), warn=self.warn)
        self.assertFalse(missing.in_ram)
        with self.free(99):
            disk = ram_dirs.place("n", "/s", pol=ram_dirs.RamPolicy(mode="on", root=self.root, require_tmpfs=True), warn=self.warn)
        self.assertFalse(disk.in_ram)
        self.assertIn("does not exist", self.warnings[0])
        self.assertIn("is not a tmpfs", self.warnings[1])

    # Tests [@ANCHOR: ram_dirs:place]
    def test_off_and_a_small_machine_keep_the_disk_quietly(self):
        with self.free(99):
            off = ram_dirs.place("n", "/s", environ=dict(self.environ, HAMS_RAM_DIRS="0"), warn=self.warn)
            with mock.patch.object(ram_dirs, "total_ram_gb", return_value=11.0):
                small = ram_dirs.place("n", "/s", environ=dict(self.environ, HAMS_RAM_DIRS="auto"), warn=self.warn)
                forced = ram_dirs.place("n", "/s", environ=self.environ, warn=self.warn)
        self.assertEqual((off.in_ram, small.in_ram, forced.in_ram), (False, False, True))
        self.assertEqual(self.warnings, [])

    # Tests [@ANCHOR: ram_dirs:place]
    def test_the_default_warning_function_prints_once_per_reason_on_standard_error(self):
        with self.free(1), mock.patch("sys.stderr") as err:
            ram_dirs.place("n", "/s", environ=self.environ)
            ram_dirs.place("n", "/s", environ=self.environ)
        printed = "".join(call.args[0] for call in err.write.call_args_list if call.args)
        self.assertEqual(printed.count("WARNING"), 1)


class CargoTargetDirTest(RamTestCase):
    # Tests [@ANCHOR: ram_dirs:cargo_target_dir]
    def test_ram_path_and_disk_fallback_names(self):
        with self.free(50):
            ram = ram_dirs.cargo_target_dir("relay-clippy", environ=self.environ)
        self.assertEqual(ram.path, os.path.join(self.root, "hams-cargo-target-relay-clippy"))
        with self.free(1):
            disk = ram_dirs.cargo_target_dir("relay-clippy", environ=self.environ)
        self.assertEqual(disk.path, os.path.join(self.home, ".cache", "hams-cargo-target-relay-clippy"))
        self.assertFalse(disk.in_ram)

    # Tests [@ANCHOR: ram_dirs:cargo_target_dir]
    def test_bad_job_names_are_refused(self):
        for bad in ("", ".hidden", "a/b", "a b", "x" * 65, "../up", "-rf"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ram_dirs.cargo_target_dir(bad, environ=self.environ)

    # Tests [@ANCHOR: ram_dirs:job_directories]
    def test_both_candidate_places_are_listed(self):
        self.assertEqual(ram_dirs.job_directories("j", environ=self.environ),
                         [os.path.join(self.root, "hams-cargo-target-j"), os.path.join(self.home, ".cache", "hams-cargo-target-j")])


class TestTmpBaseTest(RamTestCase):
    # Tests [@ANCHOR: ram_dirs:test_tmp_base]
    def test_created_world_writable_with_the_sticky_bit(self):
        with self.free(50):
            base = ram_dirs.test_tmp_base(self.environ)
        self.assertEqual(base, os.path.join(self.root, "hams-test-tmp"))
        self.assertEqual(os.stat(base).st_mode & 0o7777, 0o1777)

    # Tests [@ANCHOR: ram_dirs:test_tmp_base]
    def test_none_in_the_private_namespace_and_when_ram_is_refused(self):
        with self.free(50):
            self.assertIsNone(ram_dirs.test_tmp_base(dict(self.environ, HAMS_ISOLATED_NS="1", HAMS_TEST_LOCK_HELD="1")))
            self.assertIsNotNone(ram_dirs.test_tmp_base(dict(self.environ, HAMS_ISOLATED_NS="1")))
        with self.free(2), mock.patch("sys.stderr"):
            self.assertIsNone(ram_dirs.test_tmp_base(self.environ))

    # Tests [@ANCHOR: ram_dirs:lint_cargo_base]
    def test_lint_base_only_without_an_explicit_cache_location(self):
        environ = {k: v for k, v in self.environ.items() if k != "XDG_CACHE_HOME"}
        with self.free(50):
            self.assertEqual(ram_dirs.lint_cargo_base(environ), os.path.join(self.root, "hams-lint-cargo"))
            self.assertIsNone(ram_dirs.lint_cargo_base(self.environ))
        with self.free(1), mock.patch("sys.stderr"):
            self.assertIsNone(ram_dirs.lint_cargo_base(environ))


class OwnerTest(RamTestCase):
    def finished_child_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid

    # Tests [@ANCHOR: ram_dirs:claim]
    # Tests [@ANCHOR: ram_dirs:owner_state]
    def test_a_claim_by_a_live_process_is_alive_and_a_finished_one_is_dead(self):
        mine = ram_dirs.claim(os.path.join(self.root, "a"))
        self.assertEqual(ram_dirs.owner_state(mine), "alive")
        gone = ram_dirs.claim(os.path.join(self.root, "b"), pid=self.finished_child_pid())
        self.assertEqual(ram_dirs.owner_state(gone), "dead")

    # Tests [@ANCHOR: ram_dirs:owner_state]
    def test_a_reused_pid_is_not_the_owner(self):
        path = ram_dirs.claim(os.path.join(self.root, "a"))
        with open(os.path.join(path, ram_dirs.OWNER_FILE), "w", encoding="utf-8") as handle:
            handle.write('{"pid": %d, "start": 1, "created": 0}' % os.getpid())
        self.assertEqual(ram_dirs.owner_state(path), "dead")

    # Tests [@ANCHOR: ram_dirs:owner_state]
    def test_no_or_damaged_marker_is_unmarked(self):
        path = os.path.join(self.root, "a")
        os.makedirs(path)
        self.assertEqual(ram_dirs.owner_state(path), "unmarked")
        with open(os.path.join(path, ram_dirs.OWNER_FILE), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(ram_dirs.owner_state(path), "unmarked")

    # Tests [@ANCHOR: ram_dirs:claim]
    def test_the_directory_is_private(self):
        path = ram_dirs.claim(os.path.join(self.root, "a"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)


class SweepTest(RamTestCase):
    def make(self, name, pid=None, age_hours=0.0, marked=True, files=1):
        path = os.path.join(self.root, name)
        if marked:
            ram_dirs.claim(path, pid=pid)
        else:
            os.makedirs(path)
        for number in range(files):
            with open(os.path.join(path, f"f{number}"), "w", encoding="utf-8") as handle:
                handle.write("x" * 100)
        past = time.time() - age_hours * 3600
        for root, dirs, names in os.walk(path):
            for entry in dirs + names:
                os.utime(os.path.join(root, entry), (past, past))
            os.utime(root, (past, past))
        return path

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_removes_dead_jobs_and_keeps_live_ones_and_foreign_names(self):
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        dead = self.make("hams-cargo-target-dead", pid=finished.pid)
        live = self.make("hams-cargo-target-live")
        other = self.make("PostgreSQL.123", marked=False)
        lint = self.make("hams-lint-cargo", marked=False, age_hours=99)
        said = []
        removed = ram_dirs.sweep(environ=self.environ, say=said.append)
        self.assertEqual(removed, ["hams-cargo-target-dead"])
        self.assertFalse(os.path.exists(dead))
        for kept in (live, other, lint):
            self.assertTrue(os.path.exists(kept), kept)
        self.assertEqual(len(said), 1)
        self.assertIn("its job is gone", said[0])

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_an_unmarked_directory_goes_only_when_idle_for_a_day(self):
        young = self.make("hams-cargo-target-young", marked=False, age_hours=1)
        old = self.make("hams-cargo-target-old", marked=False, age_hours=25)
        self.assertEqual(ram_dirs.sweep(environ=self.environ, say=lambda text: None), ["hams-cargo-target-old"])
        self.assertTrue(os.path.exists(young))
        self.assertFalse(os.path.exists(old))

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_an_unmarked_directory_with_a_fresh_file_deep_inside_is_kept(self):
        path = self.make("hams-cargo-target-busy", marked=False, age_hours=48)
        deps = os.path.join(path, "debug", "deps")
        os.makedirs(deps)
        with open(os.path.join(deps, "fresh.rlib"), "w", encoding="utf-8") as handle:
            handle.write("x")
        os.utime(path, (time.time() - 48 * 3600,) * 2)
        self.assertEqual(ram_dirs.sweep(environ=self.environ, say=lambda text: None), [])

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_test_tmp_keeps_slot_directories_and_fresh_files_and_removes_stale_ones(self):
        base = os.path.join(self.root, "hams-test-tmp")
        os.makedirs(os.path.join(base, "slot-2"))
        for name, age in (("abc_chrome_odoo", 13), ("fresh_chrome_odoo", 1)):
            os.makedirs(os.path.join(base, name))
            past = time.time() - age * 3600
            os.utime(os.path.join(base, name), (past, past))
        past = time.time() - 99 * 3600
        os.utime(os.path.join(base, "slot-2"), (past, past))
        self.assertEqual(ram_dirs.sweep(environ=self.environ, say=lambda text: None), ["hams-test-tmp/abc_chrome_odoo"])
        self.assertTrue(os.path.isdir(os.path.join(base, "slot-2")))
        self.assertTrue(os.path.isdir(os.path.join(base, "fresh_chrome_odoo")))

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_a_directory_that_cannot_be_removed_is_reported_not_raised(self):
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        self.make("hams-cargo-target-stuck", pid=finished.pid)
        said = []
        removed = ram_dirs.sweep(environ=self.environ, say=said.append, remove=lambda path: False)
        self.assertEqual(removed, [])
        self.assertIn("sudo rm -rf", said[0])

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_a_missing_root_is_nothing_to_do(self):
        self.assertEqual(ram_dirs.sweep(environ=dict(self.environ, HAMS_RAM_ROOT=os.path.join(self.root, "none"))), [])


class GuardTest(RamTestCase):
    # Tests [@ANCHOR: ram_dirs:guard_verdict]
    def test_size_and_free_space_limits_apply_to_ram_jobs_only(self):
        pol = ram_dirs.RamPolicy(job_max_gb=10, hard_free_gb=5)
        gb = ram_dirs.GB
        self.assertIsNone(ram_dirs.guard_verdict(10 * gb, 6, pol, True))
        self.assertIn("per-job limit", ram_dirs.guard_verdict(10 * gb + 1, 60, pol, True))
        self.assertIn("hard floor", ram_dirs.guard_verdict(1, 4.9, pol, True))
        self.assertIsNone(ram_dirs.guard_verdict(99 * gb, 0.1, pol, False))
        self.assertIsNone(ram_dirs.guard_verdict(1, None, pol, True))


class RunJobTest(RamTestCase):
    def run_job(self, code, **kwargs):
        said = []
        with self.free(60):
            result = ram_dirs.run_job("t1", [sys.executable, "-c", code], environ=self.environ, say=said.append, interval=0.2, **kwargs)
        return result, said

    # Tests [@ANCHOR: ram_dirs:run_job]
    # Tests [@ANCHOR: ram_dirs:JobResult]
    def test_the_command_sees_its_directory_and_the_directory_is_removed_afterwards(self):
        code = ("import os,sys; t=os.environ['CARGO_TARGET_DIR']; open(os.path.join(t,'x'),'w').write('hi');"
                "print(t); print(open(os.path.join(t,'.hams-ram-owner')).read()[:6]); sys.exit(3)")
        result, said = self.run_job(code)
        self.assertEqual(result.returncode, 3)
        self.assertTrue(result.in_ram)
        self.assertEqual(result.target, os.path.join(self.root, "hams-cargo-target-t1"))
        self.assertFalse(os.path.exists(result.target))
        self.assertIn("removed", said[-1])
        self.assertIn("RAM", said[0])

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_keep_leaves_the_directory(self):
        result, _ = self.run_job("pass", keep=True)
        self.assertTrue(os.path.isdir(result.target))

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_a_signal_death_becomes_128_plus_the_signal(self):
        result, _ = self.run_job("import os,signal; os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual(result.returncode, 128 + signal.SIGTERM)

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_a_directory_of_a_live_job_is_refused(self):
        ram_dirs.claim(os.path.join(self.root, "hams-cargo-target-t1"))
        result, said = self.run_job("raise SystemExit(0)")
        self.assertEqual(result.returncode, ram_dirs.EX_TEMPFAIL)
        self.assertIn("belongs to a running job", said[-1])
        self.assertTrue(os.path.isdir(result.target))

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_the_directory_of_a_dead_job_is_replaced_and_the_run_sweeps_others(self):
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        old = ram_dirs.claim(os.path.join(self.root, "hams-cargo-target-t1"), pid=finished.pid)
        with open(os.path.join(old, "stale"), "w", encoding="utf-8") as handle:
            handle.write("x")
        other = ram_dirs.claim(os.path.join(self.root, "hams-cargo-target-other"), pid=finished.pid)
        result, _ = self.run_job("import os; assert not os.path.exists(os.environ['CARGO_TARGET_DIR']+'/stale')")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(other))

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_the_guard_stops_a_job_whose_directory_outgrows_the_limit(self):
        environ = dict(self.environ, HAMS_RAM_JOB_MAX_GB="0.00001")
        code = ("import os,time; t=os.environ['CARGO_TARGET_DIR']; open(t+'/big','w').write('x'*200000); time.sleep(60)")
        said = []
        began = time.monotonic()
        with self.free(60):
            result = ram_dirs.run_job("big", [sys.executable, "-c", code], environ=environ, say=said.append, interval=0.2, grace=5)
        self.assertLess(time.monotonic() - began, 30)
        self.assertIn("per-job limit", result.stopped)
        self.assertEqual(result.returncode, 128 + signal.SIGTERM)
        self.assertGreaterEqual(result.peak_bytes, 200000)
        self.assertFalse(os.path.exists(result.target))
        self.assertTrue(any("STOPPING" in line for line in said))

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_the_guard_stops_a_job_when_the_free_space_falls_under_the_hard_floor(self):
        said = []
        with mock.patch.object(ram_dirs, "free_gb", side_effect=[60, 60, 3, 3, 3, 3]):
            result = ram_dirs.run_job("low", [sys.executable, "-c", "import time; time.sleep(60)"], environ=self.environ,
                                      say=said.append, interval=0.2, grace=5)
        self.assertIn("hard floor", result.stopped)

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_a_job_on_the_disk_is_not_guarded_and_is_still_removed(self):
        environ = dict(self.environ, HAMS_RAM_JOB_MAX_GB="0.00001")
        said = []
        result = ram_dirs.run_job("disk", [sys.executable, "-c", "import os,time; open(os.environ['CARGO_TARGET_DIR']+'/b','w').write('x'*200000); time.sleep(1)"],
                                  force_ssd=True, environ=environ, say=said.append, interval=0.2)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.in_ram)
        self.assertIsNone(result.stopped)
        self.assertEqual(result.target, os.path.join(self.home, ".cache", "hams-cargo-target-disk"))
        self.assertFalse(os.path.exists(result.target))

    # Tests [@ANCHOR: ram_dirs:run_job]
    # Tests [@ANCHOR: ram_dirs:die_with_parent]
    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_a_killed_runner_leaves_a_directory_that_the_next_sweep_removes(self):
        """The crash case: `run` itself is SIGKILLed (no trap can run). Its directory stays, and the sweep finds it dead."""
        environ = dict(os.environ, HAMS_RAM_MIN_FREE_GB="0", **self.environ)
        code = "import os,time; open(os.environ['CARGO_TARGET_DIR']+'/pid','w').write(str(os.getpid())); time.sleep(60)"
        runner = subprocess.Popen([sys.executable, SCRIPT, "run", "crash", "--", sys.executable, "-c", code],
                                  env=environ, stderr=subprocess.PIPE, text=True)
        target = os.path.join(self.root, "hams-cargo-target-crash")
        for _ in range(100):
            if os.path.exists(os.path.join(target, "pid")):
                break
            time.sleep(0.1)
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(ram_dirs.owner_state(target), "alive")
        with open(os.path.join(target, "pid"), encoding="utf-8") as handle:
            orphan = int(handle.read())
        runner.kill()
        runner.wait()
        for _ in range(100):  # the command dies with its runner (PR_SET_PDEATHSIG)
            if ram_dirs._start_ticks(orphan) is None:
                break
            time.sleep(0.1)
        self.assertIsNone(ram_dirs._start_ticks(orphan))
        self.assertEqual(ram_dirs.owner_state(target), "dead")
        self.assertEqual(ram_dirs.sweep(environ=self.environ, say=lambda text: None), ["hams-cargo-target-crash"])
        self.assertFalse(os.path.exists(target))

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_sigterm_to_the_runner_ends_the_command_and_removes_the_directory(self):
        environ = dict(os.environ, HAMS_RAM_MIN_FREE_GB="0", **self.environ)
        code = "import os,time; open(os.environ['CARGO_TARGET_DIR']+'/pid','w').write('1'); time.sleep(60)"
        runner = subprocess.Popen([sys.executable, SCRIPT, "run", "term", "--", sys.executable, "-c", code],
                                  env=environ, stderr=subprocess.PIPE, text=True)
        target = os.path.join(self.root, "hams-cargo-target-term")
        for _ in range(100):
            if os.path.exists(os.path.join(target, "pid")):
                break
            time.sleep(0.1)
        runner.send_signal(signal.SIGTERM)
        self.assertEqual(runner.wait(timeout=30), 128 + signal.SIGTERM)
        self.assertFalse(os.path.exists(target))


class CommandLineTest(RamTestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], env=dict(os.environ, HAMS_RAM_MIN_FREE_GB="0", **self.environ), capture_output=True, text=True, timeout=60)

    # Tests [@ANCHOR: ram_dirs:claim]
    # Tests [@ANCHOR: ram_dirs:job_directories]
    def test_path_prints_a_claimed_directory_and_release_removes_it(self):
        out = self.cli("path", "shell1", "--owner-pid", str(os.getpid()))
        target = os.path.join(self.root, "hams-cargo-target-shell1")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, target), out.stderr)
        self.assertEqual(ram_dirs.owner_state(target), "alive")
        again = self.cli("path", "shell1", "--owner-pid", str(os.getpid()))
        self.assertEqual(again.returncode, ram_dirs.EX_TEMPFAIL)
        self.assertEqual(self.cli("release", "shell1").returncode, 0)
        self.assertFalse(os.path.exists(target))

    # Tests [@ANCHOR: ram_dirs:job_directories]
    def test_release_removes_a_dead_jobs_directory_and_status_lists_it(self):
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        target = ram_dirs.claim(os.path.join(self.root, "hams-cargo-target-gone"), pid=finished.pid)
        self.assertIn("hams-cargo-target-gone", self.cli("status").stdout)
        self.cli("release", "gone")
        self.assertFalse(os.path.exists(target))

    # Tests [@ANCHOR: ram_dirs:cargo_target_dir]
    def test_a_bad_job_name_exits_2(self):
        self.assertEqual(self.cli("path", "../x").returncode, 2)


if __name__ == "__main__":
    unittest.main()
