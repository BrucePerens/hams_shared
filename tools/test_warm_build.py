# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for warm_build.py: overlay stacks, promotion, atomic publish, flatten and the starvation guard (real unprivileged overlay mounts; skipped where refused)."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warm_build  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.parent = tempfile.mkdtemp(dir="/dev/shm" if os.access("/dev/shm", os.W_OK) else None)  # the RAM file system the lint cache uses
        self.addCleanup(shutil.rmtree, self.parent, True)
        self.base = os.path.join(self.parent, "hams-lint-cargo")
        os.makedirs(self.base)
        if not warm_build.available(self.base):
            self.skipTest("unprivileged overlay mounts are not available here")

    def sh(self, run, script):
        done = subprocess.run(run.argv(["sh", "-c", script.replace("$M", run.mount)]), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def build(self, script, promote, key="k"):
        with warm_build.Run(self.base, key, promote) as run:
            out = self.sh(run, script)
            run.passed = True
        return out

    def layers(self, key="k"):
        return warm_build.current_stack(self.base, key)


class StackTests(Base):
    # Tests [@ANCHOR: warm_build_run]
    def test_a_run_sees_what_a_promoted_run_left_and_a_pr_run_leaves_nothing(self):
        self.build("mkdir $M/t; echo one > $M/t/a", promote=True)
        self.assertEqual(len(self.layers()), 1)
        self.assertEqual(self.build("cat $M/t/a; echo pr > $M/t/b", promote=False).strip(), "one")
        self.assertEqual(len(self.layers()), 1)  # a pull-request run is never promoted
        self.assertEqual(self.build("ls $M/t", promote=False).split(), ["a"])  # and its writes are gone
        self.build("echo two > $M/t/c", promote=True)
        self.assertEqual(len(self.layers()), 2)
        self.assertEqual(self.build("ls $M/t", promote=False).split(), ["a", "c"])

    def test_a_populated_directory_of_a_lower_layer_can_be_removed(self):
        layer = os.path.join(self.base, "warm-k-1-1")  # made with plain file calls, as a merged (flattened) layer is
        os.makedirs(os.path.join(layer, "d", "sub"))
        for name in ("a", os.path.join("sub", "b")):
            open(os.path.join(layer, "d", name), "w").close()
        warm_build.publish(self.base, "k", ["warm-k-1-1"])
        out = self.build("set -e; rm -r $M/d; mkdir -p $M/d/new; echo 3 > $M/d/new/c; find $M/d -type f", promote=False)
        self.assertEqual(out.split("/")[-2:], ["new", "c\n"])

    def test_a_run_that_is_not_main_is_never_the_base_even_with_no_stack(self):
        self.build("echo x > $M/f", promote=False)
        self.assertEqual(self.layers(), [])

    def test_a_failed_run_is_not_promoted_and_leaves_no_directory(self):
        with warm_build.Run(self.base, "k", True) as run:
            self.sh(run, "echo x > $M/f")
        self.assertEqual(self.layers(), [])
        self.assertEqual([n for n in os.listdir(self.base) if n.startswith("warm-")], [])

    def test_runs_of_one_key_mount_at_the_same_time(self):
        self.build("echo base > $M/f", promote=True)
        with warm_build.Run(self.base, "k", False) as first, warm_build.Run(self.base, "k", False) as second:
            self.assertEqual(first.mount, second.mount)
            self.sh(first, "echo 1 > $M/mine")
            self.assertEqual(self.sh(second, "ls $M").split(), ["f"])

    def test_keys_are_checked(self):
        with self.assertRaises(ValueError):
            warm_build.Run(self.base, "a-b", False)

    def test_a_pruned_layer_makes_the_stack_cold_instead_of_broken(self):
        self.build("echo x > $M/f", promote=True)
        shutil.rmtree(os.path.join(self.base, self.layers()[0]))
        self.assertEqual(self.layers(), [])
        self.assertEqual(self.build("ls $M", promote=False).split(), [])

    def test_publishing_never_shows_a_partial_stack(self):
        warm_build.publish(self.base, "k", ["a"])
        seen, stop = set(), threading.Event()

        def swap():
            while not stop.is_set():
                warm_build.publish(self.base, "k", ["a"])
                warm_build.publish(self.base, "k", ["b", "a"])

        thread = threading.Thread(target=swap)
        thread.start()
        try:
            for _ in range(3000):
                seen.add(os.readlink(os.path.join(self.base, "warm-k-current")))
        finally:
            stop.set()
            thread.join()
        self.assertLessEqual(seen, {"a", "b:a"})


class FlattenTests(Base):
    def setUp(self):
        super().setUp()
        self.limit = warm_build.WARM_MAX_LAYERS
        self.addCleanup(setattr, warm_build, "WARM_MAX_LAYERS", self.limit)

    def test_flatten_merges_the_layers_with_whiteouts_and_opaque_directories(self):
        self.build("mkdir $M/d $M/e; echo 1 > $M/d/x; echo 1 > $M/e/y; echo 1 > $M/gone; echo 1 > $M/over", promote=True)
        self.build("rm $M/gone; echo 2 > $M/over; rm -r $M/e; mkdir $M/e; echo 2 > $M/e/z", promote=True)
        self.assertEqual(len(self.layers()), 2)
        self.assertTrue(warm_build.flatten(self.base, "k"))
        stack = self.layers()
        self.assertEqual(len(stack), 1)
        root = os.path.join(self.base, stack[0])
        self.assertFalse(os.path.exists(os.path.join(root, "gone")))
        self.assertEqual(open(os.path.join(root, "over")).read(), "2\n")
        self.assertEqual(os.listdir(os.path.join(root, "e")), ["z"])
        self.assertEqual(os.listdir(os.path.join(root, "d")), ["x"])
        self.assertEqual(sorted(n for n in os.listdir(self.base) if n.startswith("warm-k-") and not n.endswith((".lock", "-current"))), stack)

    def test_a_flatten_with_a_run_mounted_changes_nothing_and_the_run_keeps_its_layers(self):
        self.build("echo 1 > $M/a", promote=True)
        self.build("echo 2 > $M/b", promote=True)
        before = self.layers()
        with warm_build.Run(self.base, "k", False) as run:
            self.assertFalse(warm_build.flatten(self.base, "k"))
            self.assertEqual(self.layers(), before)
            self.assertEqual(self.sh(run, "ls $M").split(), ["a", "b"])
        self.assertTrue(warm_build.flatten(self.base, "k"))

    def test_a_publish_while_a_run_is_mounted_leaves_that_runs_view_intact(self):
        self.build("echo 1 > $M/a", promote=True)
        with warm_build.Run(self.base, "k", False) as run:
            self.build("echo 2 > $M/b", promote=True)  # a second, parallel run publishes a new top layer
            self.assertEqual(self.sh(run, "ls $M").split(), ["a"])

    def test_the_stack_is_flattened_when_a_run_ends_at_the_cap(self):
        warm_build.WARM_MAX_LAYERS = 3
        for index in range(5):
            self.build("echo %d > $M/f%d" % (index, index), promote=True)
        self.assertLessEqual(len(self.layers()), 3)
        self.assertEqual(self.build("ls $M", promote=False).split(), ["f0", "f1", "f2", "f3", "f4"])

    def test_a_draining_flag_makes_new_runs_wait_until_it_clears_or_goes_stale(self):
        flag = os.path.join(self.base, "k.draining")
        open(flag, "w").close()
        sleeps = []

        def clear_on_third_sleep(seconds):  # drives the wait explicitly: no real sleeping, no threads, no global patch
            sleeps.append(seconds)
            if len(sleeps) == 3:
                os.unlink(flag)

        self.addCleanup(setattr, warm_build, "_sleep", warm_build._sleep)
        warm_build._sleep = clear_on_third_sleep
        self.build("echo x > $M/f", promote=True)
        self.assertEqual(len(sleeps), 3)
        sleeps.clear()
        warm_build._sleep = sleeps.append  # a stale flag must not make the run wait at all
        open(flag, "w").close()
        os.utime(flag, (time.time() - 10_000, time.time() - 10_000))
        self.build("echo y > $M/g", promote=False)
        self.assertEqual(sleeps, [])
        # a flag that never clears and is not stale ends after WARM_DRAIN_WAIT: the wait is bounded
        open(flag, "w").close()
        self.addCleanup(setattr, warm_build, "WARM_DRAIN_WAIT", warm_build.WARM_DRAIN_WAIT)
        warm_build.WARM_DRAIN_WAIT = 0.05
        sleeps.clear()
        warm_build._sleep = lambda seconds: (sleeps.append(seconds), time.sleep(0.02))
        self.build("echo z > $M/h", promote=False)
        self.assertTrue(0 < len(sleeps) <= 10, sleeps)  # bounded by the sleep count, not by elapsed time

    def test_apply_layer_reads_a_real_whiteout(self):
        lower, upper = os.path.join(self.parent, "l"), os.path.join(self.parent, "u")
        os.makedirs(lower)
        os.makedirs(upper)
        open(os.path.join(lower, "x"), "w").close()
        try:
            os.mknod(os.path.join(upper, "x"), stat.S_IFCHR, os.makedev(0, 0))
        except PermissionError:
            self.skipTest("cannot make a whiteout device outside a user namespace")
        warm_build.apply_layer(lower, upper)
        self.assertFalse(os.path.exists(os.path.join(lower, "x")))


if __name__ == "__main__":
    unittest.main()
