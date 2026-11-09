#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for relay_structural.py, the lint step that runs the relay's fast structural `cargo test` group. A fake `cargo` on PATH answers the version, list and
test commands, so no Rust build is needed; the crate tree is a real throwaway directory of manifests.

Tests [@ANCHOR: relay_structural:watched_dirs]
Tests [@ANCHOR: relay_structural:cargo_command]
Tests [@ANCHOR: relay_structural:listed_tests]
Tests [@ANCHOR: relay_structural:unmatched_filters]
Tests [@ANCHOR: relay_structural:passed_count]
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import relay_structural as rs  # noqa: E402

LISTING = "\n".join("%s: test" % name for name in (
    "exposure_tests::a", "path_inventory_scan_tests::b", "key_material_scan_tests::c", "keying_scan_tests::d", "supervisor_scan_tests::e",
    "certificate_cull_source_scan_tests::f", "demo_lock::tests::g", "relay_manuals::tests::no_manual_is_compiled_into_the_relay",
    "data_dir::tests::h", "other::unrelated")) + "\n\n10 tests, 0 benchmarks\n"


class PureTests(unittest.TestCase):
    def test_the_command_names_the_features_the_binary_and_every_filter(self):
        command = rs.cargo_command()
        self.assertEqual(command[:7], ["cargo", "test", "--locked", "--features", rs.FEATURES, "--bin", rs.BIN])
        self.assertEqual(command[command.index("--") + 1:], list(rs.STRUCTURAL_FILTERS))
        self.assertEqual(rs.cargo_command(("x",), list_only=True)[-2:], ["--list", "x"])

    def test_listing_and_counting_read_cargo_output(self):
        self.assertEqual(rs.listed_tests(LISTING)[0], "exposure_tests::a")
        self.assertEqual(len(rs.listed_tests(LISTING)), 10)
        self.assertEqual(rs.passed_count("test result: ok. 12 passed; 0 failed\ntest result: ok. 3 passed; 0 failed\n"), 15)
        self.assertEqual(rs.passed_count("nothing"), 0)

    def test_a_filter_that_matches_nothing_is_reported_and_the_real_group_matches_the_listing(self):
        tests = rs.listed_tests(LISTING)
        self.assertEqual(rs.unmatched_filters(tests), [])
        self.assertEqual(rs.unmatched_filters(tests[:3]), [f for f in rs.STRUCTURAL_FILTERS if not any(f in t for t in tests[:3])])
        self.assertEqual(rs.unmatched_filters(tests, ("gone::",)), ["gone::"])

    def test_the_group_names_the_tests_that_failed_on_2026_11_08(self):
        names = " ".join(rs.STRUCTURAL_FILTERS)
        for needle in ("demo_lock::", "relay_manuals::", "key_material_scan_tests::", "path_inventory_scan_tests::", "data_dir::", "exposure_tests::"):
            self.assertIn(needle, names)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="relaystruct-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def crate(self, rel, deps=()):
        directory = os.path.join(self.root, "repo", rel)
        os.makedirs(directory, exist_ok=True)
        lines = ['[package]', 'name = "%s"' % os.path.basename(rel), 'version = "0.1.0"', "", "[dependencies]"]
        for dep in deps:
            lines.append('%s = { path = "%s" }' % (os.path.basename(dep), os.path.relpath(os.path.join(self.root, "repo", dep), directory)))
        with open(os.path.join(directory, "Cargo.toml"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return directory


class WatchedDirsTests(Fixture):
    def test_the_relay_its_path_dependencies_and_its_dependents_are_watched(self):
        lib = self.crate("daemons/lib_of_relay")
        relay = self.crate("daemons/hams_local_relay", deps=["daemons/lib_of_relay"])
        console = self.crate("daemons/shack_console", deps=["daemons/hams_local_relay"])
        self.crate("daemons/unrelated")
        found = rs.watched_dirs(os.path.join(self.root, "repo"))
        self.assertEqual(found, sorted([lib, relay, console]))

    def test_a_repository_without_the_relay_watches_nothing(self):
        self.crate("daemons/other")
        self.assertEqual(rs.watched_dirs(os.path.join(self.root, "repo")), [])


class MainTests(Fixture):
    def run_main(self, listing, test_exit=0, with_relay=True):
        bin_dir = os.path.join(self.root, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        with open(os.path.join(self.root, "listing.txt"), "w", encoding="utf-8") as handle:
            handle.write(listing)
        record = os.path.join(self.root, "calls.txt")
        fake = os.path.join(bin_dir, "cargo")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write(
                '#!/bin/sh\n'
                'if [ "$1" = "--version" ]; then echo cargo; exit 0; fi\n'
                'echo "$@ target=$CARGO_TARGET_DIR" >> %s\n'
                'case "$*" in *--list*) cat %s; exit 0;; esac\n'
                'echo "test result: ok. 7 passed; 0 failed"\nexit %d\n' % (record, os.path.join(self.root, "listing.txt"), test_exit))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        if with_relay:
            self.crate("daemons/hams_local_relay")
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"], XDG_CACHE_HOME=os.path.join(self.root, "cache"), HAMS_LINT_SLOT="2")
        done = subprocess.run([sys.executable, os.path.join(HERE, "relay_structural.py"), os.path.join(self.root, "repo")], capture_output=True, text=True, env=env)
        calls = open(record, encoding="utf-8").read().splitlines() if os.path.exists(record) else []
        return done, calls

    def test_a_good_group_lists_then_runs_in_the_slots_cargo_directory(self):
        done, calls = self.run_main(LISTING)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn("--list", calls[0])
        self.assertNotIn("--list", calls[1])
        self.assertIn("relay-structural", calls[1])
        self.assertIn("-s2", calls[1], "one Cargo directory per lint slot")
        self.assertIn("relay structural tests: 7 tests of %d filters passed" % len(rs.STRUCTURAL_FILTERS), done.stdout)

    def test_a_group_that_shrank_fails_before_running_anything(self):
        done, calls = self.run_main("exposure_tests::a: test\n")
        self.assertEqual(done.returncode, 1)
        self.assertIn("match no test", done.stdout)
        self.assertEqual(len(calls), 1, "only the listing ran")

    def test_a_failing_test_fails_the_step_and_shows_the_output(self):
        done, _calls = self.run_main(LISTING, test_exit=101)
        self.assertEqual(done.returncode, 1)
        self.assertIn("relay structural tests failed", done.stdout)

    def test_a_repository_without_the_relay_says_so(self):
        os.makedirs(os.path.join(self.root, "repo"), exist_ok=True)
        done, calls = self.run_main(LISTING, with_relay=False)
        self.assertEqual(done.returncode, 0)
        self.assertIn("no relay crate", done.stdout)
        self.assertEqual(calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
