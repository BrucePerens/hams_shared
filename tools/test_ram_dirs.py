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
Tests [@ANCHOR: ram_dirs:prune_lint_cache]
Tests [@ANCHOR: ram_dirs:usage]
Tests [@ANCHOR: ram_dirs:write_usage]
"""

import fcntl
import json
import os
import io
import dataclasses
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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
        with self.free(99), mock.patch.object(ram_dirs, "is_tmpfs", return_value=False):
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
    def test_a_test_tmp_directory_that_cannot_be_listed_is_reported_not_raised(self):
        base = os.path.join(self.root, "hams-test-tmp")
        os.makedirs(base)
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        dead = self.make("hams-cargo-target-dead", pid=finished.pid)
        os.chmod(base, 0)
        self.addCleanup(os.chmod, base, 0o700)
        said = []
        removed = ram_dirs.sweep(environ=self.environ, say=said.append)
        self.assertEqual(removed, ["hams-cargo-target-dead"])
        self.assertFalse(os.path.exists(dead))
        if os.geteuid() != 0:
            self.assertTrue(any("cannot list" in line for line in said), said)

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_a_missing_root_is_nothing_to_do(self):
        self.assertEqual(ram_dirs.sweep(environ=dict(self.environ, HAMS_RAM_ROOT=os.path.join(self.root, "none"))), [])


class LintBudgetTest(RamTestCase):
    def make(self, name, size, age_days):
        path = os.path.join(self.root, "hams-lint-cargo", name)
        os.makedirs(path)
        with open(os.path.join(path, "f"), "wb") as handle:
            handle.write(b"x" * size)
        stamp = os.path.join(path, ".last_used")
        open(stamp, "w").close()
        past = time.time() - age_days * 86400
        os.utime(stamp, (past, past))
        return path

    def pol(self, budget_bytes):
        return ram_dirs.RamPolicy(root=self.root, require_tmpfs=False, lint_budget_gb=budget_bytes / ram_dirs.GB)

    # Tests [@ANCHOR: ram_dirs:prune_lint_cache]
    def test_oldest_unused_directories_go_first_until_the_total_fits(self):
        old = self.make("old", 1000, 5)
        mid = self.make("mid", 1000, 3)
        new = self.make("new", 1000, 1)
        said = []
        removed = ram_dirs.prune_lint_cache(os.path.join(self.root, "hams-lint-cargo"), pol=self.pol(2500), say=said.append)
        self.assertEqual([name for name, _ in removed], ["old"])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(mid) and os.path.exists(new))
        self.assertIn("RAM budget", said[0])

    # Tests [@ANCHOR: ram_dirs:prune_lint_cache]
    def test_a_directory_a_live_run_holds_and_the_one_about_to_be_built_are_never_removed(self):
        busy = self.make("busy", 1000, 9)
        keep = self.make("keep", 1000, 8)
        idle = self.make("idle", 1000, 1)
        with open(busy + ".lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            removed = ram_dirs.prune_lint_cache(os.path.join(self.root, "hams-lint-cargo"), keep=keep, pol=self.pol(1), say=lambda t: None)
        self.assertEqual([name for name, _ in removed], ["idle"])
        self.assertTrue(os.path.exists(busy) and os.path.exists(keep))
        self.assertFalse(os.path.exists(idle))

    # Tests [@ANCHOR: ram_dirs:prune_lint_cache]
    def test_within_the_budget_nothing_is_removed_and_a_missing_base_is_fine(self):
        self.make("a", 1000, 9)
        self.assertEqual(ram_dirs.prune_lint_cache(os.path.join(self.root, "hams-lint-cargo"), pol=self.pol(10 ** 6), say=lambda t: None), [])
        self.assertEqual(ram_dirs.prune_lint_cache(os.path.join(self.root, "none"), pol=self.pol(1)), [])

    # Tests [@ANCHOR: ram_dirs:usage]
    # Tests [@ANCHOR: ram_dirs:write_usage]
    def test_the_usage_file_describes_the_cache_and_the_jobs(self):
        self.make("a", 2000, 1)
        ram_dirs.claim(os.path.join(self.root, "hams-cargo-target-j1"))
        path = ram_dirs.write_usage(self.pol(5 * ram_dirs.GB))
        self.assertEqual(path, os.path.join(self.root, "hams-ram-usage.json"))
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["lint_cache"]["budget_gb"], 5.0)
        self.assertEqual([d["name"] for d in data["lint_cache"]["dirs"]], ["a"])
        self.assertEqual(data["jobs"][0]["name"], "hams-cargo-target-j1")
        self.assertEqual(data["jobs"][0]["owner"], "alive")
        self.assertIn("free_gb", data)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)

    # Tests [@ANCHOR: ram_dirs:write_usage]
    def test_an_unwritable_root_is_not_an_error(self):
        self.assertIsNone(ram_dirs.write_usage(ram_dirs.RamPolicy(root=os.path.join(self.root, "missing"))))

    # Tests [@ANCHOR: ram_dirs:policy]
    def test_the_budget_is_configurable(self):
        self.assertEqual(ram_dirs.policy({}).lint_budget_gb, 24.0)
        self.assertEqual(ram_dirs.policy({"HAMS_RAM_LINT_GB": "10"}).lint_budget_gb, 10.0)


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
    # Tests [@ANCHOR: ram_dirs:leftovers]
    # Tests [@ANCHOR: ram_dirs:make_job_tmp]
    # Tests [@ANCHOR: ram_dirs:leak_mode]
    def test_a_clean_job_is_silent_and_a_job_that_leaves_a_file_is_named_loudly_then_cleaned(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        self.environ["TMPDIR"] = base
        clean, said = self.run_job("import os, tempfile; open(os.path.join(tempfile.gettempdir(), 'gone'), 'w').close(); os.remove(os.path.join(tempfile.gettempdir(), 'gone'))")
        self.assertEqual((clean.returncode, clean.leaked), (0, ()))
        self.assertFalse([line for line in said if "LEAK" in line])
        leaky, said = self.run_job("import os, tempfile; open(os.path.join(tempfile.gettempdir(), 'left_behind.png'), 'w').write('x')")
        self.assertEqual((leaky.returncode, leaky.leaked), (0, ("left_behind.png",)))
        self.assertTrue([line for line in said if "LEAK" in line and "left_behind.png" in line])
        self.assertEqual(os.listdir(base), [], "the private TMPDIR is removed after the report")
        self.assertEqual(ram_dirs.leak_mode({}), "warn")
        self.assertEqual(ram_dirs.leak_mode({"HAMS_LEAK_CHECK": "FAIL"}), "fail")
        self.assertEqual(ram_dirs.leak_mode({"HAMS_LEAK_CHECK": "bogus"}), "warn")

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_fail_mode_fails_a_job_that_succeeded_but_leaked_and_off_mode_does_not_look(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        self.environ["TMPDIR"] = base
        code = "import os; open(os.path.join(os.environ['TMPDIR'], 'x'), 'w').close()"
        self.environ["HAMS_LEAK_CHECK"] = "fail"
        result, _ = self.run_job(code)
        self.assertEqual(result.returncode, ram_dirs.EX_LEAK)
        failing, _ = self.run_job(code + "; raise SystemExit(3)")
        self.assertEqual(failing.returncode, 3, "a job's own failure is not replaced")
        self.environ["HAMS_LEAK_CHECK"] = "off"
        result, said = self.run_job("import os; print(os.environ.get('TMPDIR'))")
        self.assertEqual((result.returncode, result.leaked), (0, ()))
        self.assertEqual(os.listdir(base), [])

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
    def test_an_unclaimable_directory_is_refused_with_an_exit_code_not_a_traceback(self):
        squat = os.path.join(self.root, "hams-cargo-target-t1")
        os.makedirs(squat)
        os.chmod(squat, 0)
        self.addCleanup(os.chmod, squat, 0o700)
        if os.access(squat, os.W_OK):
            self.skipTest("running as a user that ignores directory modes")
        with mock.patch.object(ram_dirs, "owner_state", return_value="dead"), mock.patch.object(ram_dirs, "remove_tree"):
            result, said = self.run_job("raise SystemExit(0)")
        self.assertEqual(result.returncode, ram_dirs.EX_TEMPFAIL)
        self.assertIn("cannot claim", said[-1])

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_two_starts_of_one_job_name_take_turns_at_the_check_and_claim(self):
        target = os.path.join(self.root, "hams-cargo-target-t1")
        entered = []
        release = threading.Event()

        def first():
            with ram_dirs._claim_lock(target):
                entered.append("first")
                release.wait(5)

        thread = threading.Thread(target=first)
        thread.start()
        while not entered:
            time.sleep(0.01)
        def later():
            with ram_dirs._claim_lock(target):
                entered.append("second")

        second = threading.Thread(target=later)
        second.start()
        time.sleep(0.3)
        self.assertEqual(entered, ["first"])  # the second is blocked while the first holds the lock
        release.set()
        thread.join()
        second.join(5)
        self.assertEqual(entered, ["first", "second"])

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
    # Tests [@ANCHOR: ram_dirs:JobResult]
    def test_a_stopped_job_that_traps_the_signal_and_exits_0_is_not_reported_as_success(self):
        environ = dict(self.environ, HAMS_RAM_JOB_MAX_GB="0.00001")
        code = ("import os,signal,sys,time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); t=os.environ['CARGO_TARGET_DIR'];"
                "open(t+'/big','w').write('x'*200000); time.sleep(60)")
        with self.free(60):
            result = ram_dirs.run_job("big", [sys.executable, "-c", code], environ=environ, say=lambda text: None, interval=0.2, grace=5)
        self.assertIn("per-job limit", result.stopped)
        self.assertEqual(result.returncode, ram_dirs.EX_TEMPFAIL)

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_a_signal_after_the_child_was_reaped_is_not_forwarded_to_its_old_group(self):
        installed, killed = {}, []

        class Child:
            pid = 424242

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        real_signal = ram_dirs.signal.signal

        def record(number, handler):
            installed.setdefault(number, handler)  # the first handler is run_job's own forwarder
            return real_signal(number, handler)

        with unittest.mock.patch.object(ram_dirs.signal, "signal", side_effect=record), \
                unittest.mock.patch.object(ram_dirs.os, "killpg", side_effect=lambda pid, sig: killed.append((pid, sig))), self.free(60):
            ram_dirs.run_job("t1", ["true"], environ=self.environ, say=lambda text: None, interval=0.2, popen=lambda *a, **k: Child())
            self.assertIn(signal.SIGTERM, installed)
            killed.clear()
            installed[signal.SIGTERM](signal.SIGTERM, None)  # the run is over and the child reaped: nothing may be signalled
            self.assertEqual(killed, [])

    # Tests [@ANCHOR: ram_dirs:die_with_parent]
    def test_a_runner_that_died_before_the_child_asked_for_the_parent_death_signal_ends_the_child(self):
        killed = []
        with unittest.mock.patch.object(ram_dirs.ctypes, "CDLL"), \
                unittest.mock.patch.object(ram_dirs.os, "getppid", side_effect=[100, 1]), \
                unittest.mock.patch.object(ram_dirs.os, "kill", side_effect=lambda pid, sig: killed.append((pid, sig))):
            ram_dirs._die_with_parent()
        self.assertEqual(killed, [(os.getpid(), signal.SIGTERM)])
        killed.clear()
        with unittest.mock.patch.object(ram_dirs.ctypes, "CDLL"), unittest.mock.patch.object(ram_dirs.os, "getppid", return_value=100), \
                unittest.mock.patch.object(ram_dirs.os, "kill", side_effect=lambda pid, sig: killed.append((pid, sig))):
            ram_dirs._die_with_parent()
        self.assertEqual(killed, [])

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
        target = ram_dirs.claim(os.path.join(self.root, "hams-cargo-target-gone"), pid=finished.pid, keep=True)  # a kept job waits for gc
        self.assertIn("hams-cargo-target-gone", self.cli("status").stdout)
        self.cli("release", "gone")
        self.assertFalse(os.path.exists(target))

    # Tests [@ANCHOR: ram_dirs:cargo_target_dir]
    def test_a_bad_job_name_exits_2(self):
        self.assertEqual(self.cli("path", "../x").returncode, 2)


DEAD_PID = 2 ** 22 - 3  # above the kernel's default pid_max of 4194304 minus a little: no such process


class GarbageCollectionTest(RamTestCase):
    """Fake directory trees, a fake /proc and fake owners: no real process or timing is involved."""

    def make_job(self, name, pid=DEAD_PID, keep=False, age_hours=0.0, files=1, job="t"):
        path = os.path.join(self.root, ram_dirs.CARGO_PREFIX + name)
        ram_dirs.claim(path, pid, keep=keep, job=job)
        os.makedirs(os.path.join(path, "debug", "deps"))
        for index in range(files):
            with open(os.path.join(path, "debug", "deps", f"f{index}"), "w") as handle:
                handle.write("x" * 1000)
        past = time.time() - age_hours * 3600
        for base, dirs, names in os.walk(path):
            for entry in dirs + names:
                os.utime(os.path.join(base, entry), (past, past), follow_symlinks=False)
            os.utime(base, (past, past))
        return path

    def fake_proc(self, **processes):
        """A fake /proc: pid -> {"environ": {...}, "cwd": path, "fds": [paths]}."""
        proc = os.path.join(self._tmp.name, "proc")
        shutil.rmtree(proc, ignore_errors=True)
        os.makedirs(proc)
        for pid, info in processes.items():
            base = os.path.join(proc, str(pid))
            os.makedirs(os.path.join(base, "fd"))
            with open(os.path.join(base, "environ"), "wb") as handle:
                handle.write(b"\0".join(f"{k}={v}".encode() for k, v in info.get("environ", {}).items()) + b"\0")
            if "cwd" in info:
                os.symlink(info["cwd"], os.path.join(base, "cwd"))
            for number, target in enumerate(info.get("fds", [])):
                os.symlink(target, os.path.join(base, "fd", str(number)))
        return proc

    # Tests [@ANCHOR: ram_dirs:claim]
    # Tests [@ANCHOR: ram_dirs:owner_record]
    # Tests [@ANCHOR: ram_dirs:process_name]
    def test_the_marker_records_who_owns_the_directory_and_whether_it_is_kept(self):
        path = self.make_job("a", pid=os.getpid(), keep=True, job="gate")
        record = ram_dirs.owner_record(path)
        self.assertEqual((record["pid"], record["job"], record["keep"]), (os.getpid(), "gate", True))
        self.assertEqual(record["name"], ram_dirs.process_name(os.getpid()))
        self.assertTrue(record["name"])
        self.assertIsNone(ram_dirs.process_name(DEAD_PID))
        self.assertIsNone(ram_dirs.owner_record(os.path.join(self.root, "nothing")))
        with open(os.path.join(path, ram_dirs.OWNER_FILE), "w") as handle:
            json.dump({"pid": 7}, handle)  # a marker written before the extra fields existed
        self.assertEqual(ram_dirs.owner_record(path), {"pid": 7, "start": None, "created": None, "name": None, "job": None, "keep": False})

    # Tests [@ANCHOR: ram_dirs:newest_activity]
    def test_activity_is_the_newest_of_modification_and_access_a_level_or_two_down(self):
        old = self.make_job("old", age_hours=10)
        self.assertGreater(time.time() - ram_dirs.newest_activity(old), 9 * 3600)
        ram_dirs.claim(old, DEAD_PID)  # rewriting the marker changes the directory itself
        self.assertLess(time.time() - ram_dirs.newest_activity(old), 60)
        deep = self.make_job("deep", age_hours=10)
        os.utime(os.path.join(deep, "debug", "deps", "f0"), None)  # a file one level further down was just read or written
        self.assertLess(time.time() - ram_dirs.newest_activity(deep), 60)

    # Tests [@ANCHOR: ram_dirs:newest_activity]
    def test_reading_the_marker_or_listing_a_directory_is_not_activity_but_reading_a_file_is(self):
        """`relatime` refreshes an access time at or before the modification time on the next read, so gc's own reading of the
        marker and listing of directories must not restart the idle clock. The access times are set by hand here, so the result
        does not depend on how the filesystem under the test is mounted."""
        path = self.make_job("quiet", age_hours=10)
        now = time.time()
        for directory in (path, os.path.join(path, "debug"), os.path.join(path, "debug", "deps")):
            os.utime(directory, (now, os.stat(directory).st_mtime))
        marker = os.path.join(path, ram_dirs.OWNER_FILE)
        os.utime(marker, (now, os.stat(marker).st_mtime))
        self.assertGreater(time.time() - ram_dirs.newest_activity(path), 9 * 3600)
        data = os.path.join(path, "debug", "deps", "f0")
        os.utime(data, (now, os.stat(data).st_mtime))  # something read a build file
        self.assertLess(time.time() - ram_dirs.newest_activity(path), 60)

    # Tests [@ANCHOR: ram_dirs:gc]
    def test_a_gc_pass_does_not_restart_the_idle_clock_of_what_it_examines(self):
        """The gc pass reads the marker and lists the tree; on a relatime mount (/dev/shm) that used to look like activity."""
        path = self.make_job("old", keep=True, age_hours=9)
        pol = ram_dirs.policy(self.environ)
        for _ in range(2):
            ram_dirs.gc(pol, say=lambda text: None, proc_root=self.fake_proc(), dry_run=True)
            self.assertGreater(time.time() - ram_dirs.newest_activity(path), 8 * 3600)
        self.assertEqual([name for name, _s, _r in ram_dirs.gc(pol, say=lambda text: None, proc_root=self.fake_proc())], [ram_dirs.CARGO_PREFIX + "old"])

    # Tests [@ANCHOR: ram_dirs:references]
    def test_a_process_refers_to_a_directory_by_its_environment_its_cwd_or_an_open_file(self):
        path = self.make_job("busy")
        other = self.make_job("other")
        proc = self.fake_proc(**{
            "101": {"environ": {"CARGO_TARGET_DIR": path}},
            "102": {"environ": {"HOME": "/x", "TMPDIR": os.path.join(path, "tmp")}},
            "103": {"cwd": os.path.join(path, "debug")},
            "104": {"fds": [os.path.join(path, "debug", "deps", "f0")]},
            "105": {"environ": {"CARGO_TARGET_DIR": other + "-not"}, "cwd": "/"},
            "106": {"environ": {"CARGO_TARGET_DIR": "/somewhere/else"}},
        })
        self.assertEqual(ram_dirs.references(path, proc), [101, 102, 103, 104])
        self.assertEqual(ram_dirs.references(other, proc), [])
        self.assertEqual(ram_dirs.references(path, os.path.join(proc, "missing")), [])

    # Tests [@ANCHOR: ram_dirs:gc]
    # Tests [@ANCHOR: ram_dirs:log_removal]
    # Tests [@ANCHOR: ram_dirs:gc_log_path]
    def test_gc_removes_an_idle_unreferenced_ownerless_directory_and_logs_it(self):
        gone = self.make_job("gone", keep=True, age_hours=4, files=5)
        said = []
        done = ram_dirs.gc(ram_dirs.policy(self.environ), say=said.append, proc_root=self.fake_proc())
        self.assertEqual(done, [])  # a kept job waits 6 hours, not 3
        self.assertTrue(os.path.isdir(gone))
        os.utime(os.path.join(gone, "debug"), (time.time() - 7 * 3600,) * 2)
        for base, dirs, names in os.walk(gone):
            for entry in dirs + names:
                os.utime(os.path.join(base, entry), (time.time() - 7 * 3600,) * 2)
            os.utime(base, (time.time() - 7 * 3600,) * 2)
        done = ram_dirs.gc(ram_dirs.policy(self.environ), say=said.append, proc_root=self.fake_proc())
        self.assertEqual([(name, reason.split(" (")[0]) for name, _size, reason in done], [(ram_dirs.CARGO_PREFIX + "gone", "idle for 7.0 h")])
        self.assertFalse(os.path.exists(gone))
        log = open(ram_dirs.gc_log_path(ram_dirs.policy(self.environ))).read()
        self.assertIn(f"removed {ram_dirs.CARGO_PREFIX}gone", log)
        self.assertIn("idle 7.0 h", log)
        self.assertIn(f"owner pid {DEAD_PID}", log)
        self.assertIn("a kept job", log)
        self.assertTrue([line for line in said if "removed" in line and "hams-ram-gc.log" in line])

    # Tests [@ANCHOR: ram_dirs:gc]
    def test_gc_keeps_a_live_owners_directory_a_referenced_one_a_fresh_one_and_the_lint_cache(self):
        pol = ram_dirs.policy(self.environ)
        live = self.make_job("live", pid=os.getpid(), age_hours=50)
        referenced = self.make_job("referenced", keep=True, age_hours=50)
        fresh_kept = self.make_job("freshkept", keep=True, age_hours=1)
        lint = os.path.join(self.root, ram_dirs.LINT_CARGO_NAME, "d1")
        os.makedirs(lint)
        open(os.path.join(lint, "blob"), "w").write("y" * 5000)
        os.utime(lint, (time.time() - 99 * 3600,) * 2)
        proc = self.fake_proc(**{"77": {"cwd": referenced}})
        said = []
        self.assertEqual(ram_dirs.gc(pol, say=said.append, proc_root=proc), [])
        for path in (live, referenced, fresh_kept, lint):
            self.assertTrue(os.path.exists(path), path)
        self.assertTrue([line for line in said if "process 77 still refers" in line])

    # Tests [@ANCHOR: ram_dirs:gc]
    def test_gc_removes_a_finished_job_at_once_but_not_a_marked_job_that_is_still_running(self):
        pol = ram_dirs.policy(self.environ)
        finished = self.make_job("finished", age_hours=0)
        running = self.make_job("running", pid=os.getpid(), age_hours=0)
        done = ram_dirs.gc(pol, say=lambda text: None, proc_root=self.fake_proc())
        self.assertEqual([name for name, _s, _r in done], [ram_dirs.CARGO_PREFIX + "finished"])
        self.assertFalse(os.path.exists(finished))
        self.assertTrue(os.path.exists(running))

    # Tests [@ANCHOR: ram_dirs:gc]
    def test_gc_treats_an_unmarked_directory_by_its_idle_time_and_dry_run_removes_nothing(self):
        path = os.path.join(self.root, ram_dirs.CARGO_PREFIX + "byhand")
        os.makedirs(os.path.join(path, "debug"))
        past = time.time() - 5 * 3600
        for entry in (os.path.join(path, "debug"), path):
            os.utime(entry, (past, past))
        pol = ram_dirs.policy(dict(self.environ, HAMS_RAM_IDLE_HOURS="3"))
        self.assertEqual(pol.idle_hours, 3.0)
        would = ram_dirs.gc(pol, say=lambda text: None, proc_root=self.fake_proc(), dry_run=True)
        self.assertEqual(len(would), 1)
        self.assertTrue(os.path.isdir(path))
        self.assertFalse(os.path.exists(ram_dirs.gc_log_path(pol)))
        self.assertEqual(len(ram_dirs.gc(pol, say=lambda text: None, proc_root=self.fake_proc())), 1)
        self.assertFalse(os.path.exists(path))
        longer = ram_dirs.policy(dict(self.environ, HAMS_RAM_IDLE_HOURS="9", HAMS_RAM_KEEP_HOURS="bad"))
        self.assertEqual((longer.idle_hours, longer.keep_hours), (9.0, 6.0))

    # Tests [@ANCHOR: ram_dirs:gc]
    def test_gc_trims_the_lint_cache_when_it_is_over_its_budget_oldest_first_and_logs_it(self):
        pol = dataclasses.replace(ram_dirs.policy(self.environ), lint_budget_gb=0.000004)  # 4 KB
        base = os.path.join(self.root, ram_dirs.LINT_CARGO_NAME)
        for name, age in (("old", 5), ("new", 1)):
            os.makedirs(os.path.join(base, name))
            open(os.path.join(base, name, "blob"), "w").write("z" * 3000)
            past = time.time() - age * 3600
            os.utime(os.path.join(base, name), (past, past))
        done = ram_dirs.gc(pol, say=lambda text: None, proc_root=self.fake_proc())
        self.assertEqual([name for name, _s, _r in done], [f"{ram_dirs.LINT_CARGO_NAME}/old"])
        self.assertTrue(os.path.isdir(os.path.join(base, "new")))
        self.assertIn("lint cache budget", open(ram_dirs.gc_log_path(pol)).read())

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_sweep_leaves_a_dead_kept_job_to_gc_and_logs_what_it_removes(self):
        kept = self.make_job("kept", keep=True)
        plain = self.make_job("plain")
        removed = ram_dirs.sweep(ram_dirs.policy(self.environ), say=lambda text: None)
        self.assertEqual(removed, [ram_dirs.CARGO_PREFIX + "plain"])
        self.assertTrue(os.path.isdir(kept))
        self.assertFalse(os.path.exists(plain))
        self.assertIn("its job is gone", open(ram_dirs.gc_log_path(ram_dirs.policy(self.environ))).read())

    # Tests [@ANCHOR: ram_dirs:describe_owner]
    # Tests [@ANCHOR: ram_dirs:status_lines]
    def test_status_names_the_owner_pid_program_job_and_age_instead_of_unmarked(self):
        self.make_job("mine", pid=os.getpid(), job="gate")
        self.make_job("dead", keep=True, job="old", age_hours=1)
        os.makedirs(os.path.join(self.root, ram_dirs.CARGO_PREFIX + "byhand"))
        lines = ram_dirs.status_lines(ram_dirs.policy(self.environ))
        text = "\n".join(lines)
        self.assertIn(f"owner pid {os.getpid()} (", text)
        self.assertIn("alive, started", text)
        self.assertIn("job gate", text)
        self.assertIn(f"owner pid {DEAD_PID}", text)
        self.assertIn("dead", text)
        self.assertIn(", kept", text)
        self.assertRegex(text, r"hams-cargo-target-byhand: [0-9.]+ GB, unmarked, idle")
        self.assertIn("idle limit 3 h, kept jobs 6 h", lines[0])

    # Tests [@ANCHOR: ram_dirs:run_job]
    def test_a_running_job_warns_once_at_eighty_percent_of_its_limit_naming_its_owner(self):
        said = []
        environ = dict(self.environ, HAMS_RAM_JOB_MAX_GB="0.00001")  # about 10 KB
        code = ("import os,time; t=os.environ['CARGO_TARGET_DIR']; open(os.path.join(t,'big'),'w').write('x'*9000); time.sleep(0.6)")
        with self.free(60):
            ram_dirs.run_job("w1", [sys.executable, "-c", code], environ=environ, say=said.append, interval=0.2, keep=True)
        warnings = [line for line in said if line.startswith("WARNING job w1")]
        self.assertEqual(len(warnings), 1, said)
        self.assertIn(f"pid {os.getpid()}", warnings[0])
        record = ram_dirs.owner_record(os.path.join(self.root, ram_dirs.CARGO_PREFIX + "w1"))
        self.assertEqual((record["job"], record["keep"]), ("w1", True))

    # Tests [@ANCHOR: ram_dirs:main]
    def test_the_gc_command_has_a_dry_run_and_status_runs_gc_first(self):
        old = self.make_job("old", keep=True, age_hours=9)
        with mock.patch.dict(os.environ, self.environ), mock.patch.object(ram_dirs, "references", return_value=[]):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(ram_dirs.main(["gc", "--dry-run"]), 0)
            self.assertIn("would remove " + ram_dirs.CARGO_PREFIX + "old", err.getvalue())
            self.assertTrue(os.path.isdir(old))
            with mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(ram_dirs.main(["status"]), 0)
            self.assertFalse(os.path.exists(old))

    # Tests [@ANCHOR: ram_dirs:main]
    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_gc_dry_run_removes_and_modifies_nothing_and_names_what_it_would_remove(self):
        """`gc --dry-run` used to run a real `sweep()` first, which removed dead jobs' directories and stale test TMPDIR entries."""
        dead = self.make_job("dead")  # its job ended
        stale = os.path.join(self.root, ram_dirs.TEST_TMP_NAME, "old-leftover")
        os.makedirs(stale)
        long_ago = time.time() - 40 * 3600
        os.utime(stale, (long_ago, long_ago))
        before = sorted((os.path.join(b, n), os.lstat(os.path.join(b, n)).st_mtime_ns)
                        for b, ds, fs in os.walk(self.root) for n in ds + fs)
        with mock.patch.dict(os.environ, self.environ), mock.patch.object(ram_dirs, "references", return_value=[]):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(ram_dirs.main(["gc", "--dry-run"]), 0)
        self.assertTrue(os.path.isdir(dead))
        self.assertTrue(os.path.isdir(stale))
        after = sorted((os.path.join(b, n), os.lstat(os.path.join(b, n)).st_mtime_ns)
                       for b, ds, fs in os.walk(self.root) for n in ds + fs)
        self.assertEqual(before, after)  # nothing removed, created (no removal log) or touched
        self.assertIn("would remove " + ram_dirs.CARGO_PREFIX + "dead", err.getvalue())
        self.assertEqual(err.getvalue().count("would remove " + ram_dirs.CARGO_PREFIX + "dead"), 1)
        self.assertIn("would remove " + ram_dirs.TEST_TMP_NAME + "/old-leftover", err.getvalue())

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_sweep_dry_run_returns_what_it_would_remove_and_removes_nothing(self):
        dead = self.make_job("dead")
        pol = ram_dirs.policy(self.environ)
        self.assertEqual(ram_dirs.sweep(pol, dry_run=True), [ram_dirs.CARGO_PREFIX + "dead"])
        self.assertTrue(os.path.isdir(dead))
        self.assertFalse(os.path.exists(ram_dirs.gc_log_path(pol)))

    # Tests [@ANCHOR: ram_dirs:sweep]
    def test_sweep_logs_the_idle_time_of_what_it_removes(self):
        self.make_job("dead", age_hours=5)
        pol = ram_dirs.policy(self.environ)
        ram_dirs.sweep(pol, say=lambda text: None)
        with open(ram_dirs.gc_log_path(pol), encoding="utf-8") as handle:
            line = handle.read()
        self.assertIn("idle 5.0 h", line)


class GcUnitFilesTest(unittest.TestCase):
    # Tests [@ANCHOR: ram_dirs:gc]
    def test_the_user_timer_runs_the_gc_command_and_is_only_a_file_until_someone_enables_it(self):
        directory = os.path.join(_HERE, "ram_dirs_units")
        service = open(os.path.join(directory, "hams-ram-dirs-gc.service"), encoding="utf-8").read()
        timer = open(os.path.join(directory, "hams-ram-dirs-gc.timer"), encoding="utf-8").read()
        self.assertIn("ram_dirs.py gc", service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("TimeoutStartSec=", service, "a hung oneshot would silence an OnUnitActiveSec timer for ever")
        self.assertIn("OnUnitActiveSec=30min", timer)
        self.assertNotIn("[Install]", service, "the service is started by the timer, not enabled itself")
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
