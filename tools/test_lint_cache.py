#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_cache: the cargo target budget (LRU pruning by unit, free-space floor, fail-safe clear), the
locking, the managed-directory rule and the per-file result cache (a changed file never reads an old entry)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

import lint_cache


def make_unit(profile, crate, unit_hash, size, age_seconds, incremental=0):
    """A fake compilation unit: deps/, .fingerprint/ and build/ files sharing one hash, last used age_seconds ago."""
    when = time.time() - age_seconds
    paths = [
        os.path.join(profile, "deps", f"lib{crate}-{unit_hash}.rlib"),
        os.path.join(profile, "deps", f"{crate}-{unit_hash}.d"),
        os.path.join(profile, ".fingerprint", f"{crate}-{unit_hash}", "lib-x"),
        os.path.join(profile, "build", f"{crate}-{unit_hash}", "output"),
    ]
    if incremental:
        paths.append(os.path.join(profile, "incremental", f"{crate}-{unit_hash}", "s-1", "dep-graph.bin"))
    for index, path in enumerate(paths):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x" * (size if index == 0 else 10) if index != 4 else b"i" * incremental)
        os.utime(path, (when, when))
    return paths


def locked_free(path):
    """True when nobody holds the exclusive lock beside `path`."""
    with lint_cache.locked(path, wait=False) as held:
        return held


class CargoBudgetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = os.path.join(self._tmp.name, "hams-lint-cargo", "repo-1.0-abcd1234")
        self.profile = os.path.join(self.target, "debug")

    def test_under_budget_nothing_is_removed(self):
        paths = make_unit(self.profile, "a", "0123456789abcdef", 1000, 100)
        before, after, removed = lint_cache.prune_cargo_target(self.target, 10_000)
        self.assertEqual((removed, before, after), (0, before, before))
        self.assertTrue(all(os.path.exists(p) for p in paths))

    def test_prune_removes_least_recently_used_units_whole(self):
        old = make_unit(self.profile, "old", "aaaaaaaaaaaaaaaa", 4000, 5000)
        mid = make_unit(self.profile, "mid", "bbbbbbbbbbbbbbbb", 4000, 3000)
        new = make_unit(self.profile, "new", "cccccccccccccccc", 4000, 10)
        # 12 kB of payload against a 9 kB budget: the goal is 80 % = 7.2 kB, so the two oldest go.
        _before, after, removed = lint_cache.prune_cargo_target(self.target, 9000)
        self.assertEqual(removed, 2)
        self.assertFalse(any(os.path.exists(p) for p in old))
        self.assertFalse(any(os.path.exists(p) for p in mid))
        self.assertTrue(all(os.path.exists(p) for p in new))
        self.assertLessEqual(after, 9000)

    def test_incremental_data_goes_before_any_unit(self):
        unit = make_unit(self.profile, "a", "dddddddddddddddd", 1000, 9000, incremental=5000)
        _b, _a, removed = lint_cache.prune_cargo_target(self.target, 4000)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(unit[4]))
        self.assertTrue(os.path.exists(unit[0]))

    def test_prepare_clears_when_pruning_cannot_reach_the_budget(self):
        make_unit(self.profile, "a", "eeeeeeeeeeeeeeee", 5000, 10)
        with open(os.path.join(self.target, "debug", "deps", "unhashed-blob"), "wb") as handle:
            handle.write(b"z" * 7000)  # not a unit: pruning cannot remove it
        budget = {"cargo_gb": 6000 / lint_cache.GB, "free_floor_gb": 0, "results_mb": 1}
        lines = lint_cache.prepare_cargo_target(self.target, budget=budget, environ={})
        self.assertTrue(any("cleared the directory" in line for line in lines))
        self.assertEqual(lint_cache.dir_bytes(self.target), 0)

    def test_free_space_floor_forces_a_prune(self):
        make_unit(self.profile, "a", "ffffffffffffffff", 1000, 10)
        budget = {"cargo_gb": 1, "free_floor_gb": 10 ** 9, "results_mb": 1}  # no machine has an exabyte free
        lines = lint_cache.prepare_cargo_target(self.target, budget=budget, environ={})
        self.assertTrue(any("pruned" in line for line in lines))
        self.assertTrue(os.path.isdir(self.target))  # fail safe: the build can still start (cold)

    def test_unused_directories_are_dropped_but_not_the_current_one(self):
        base = os.path.dirname(self.target)
        stale = os.path.join(base, "repo-0.9-old")
        os.makedirs(stale)
        os.makedirs(self.target)
        long_ago = time.time() - 30 * 86400
        os.utime(stale, (long_ago, long_ago))
        os.utime(self.target, (long_ago, long_ago))
        self.assertEqual(lint_cache.drop_unused_dirs(base, keep=self.target), ["repo-0.9-old"])
        self.assertTrue(os.path.isdir(self.target))

    def test_prune_waits_for_a_live_build(self):
        os.makedirs(self.target)
        with lint_cache.locked(self.target) as held:
            self.assertTrue(held)
            with lint_cache.locked(self.target, wait=False) as second:
                self.assertFalse(second)  # a second process would block here, not prune under the build

    def test_unmanaged_target_dir_is_left_alone(self):
        environ = {"CARGO_TARGET_DIR": self.target}
        make_unit(self.profile, "a", "0123456789abcdef", 1000, 10)
        with lint_cache.cargo_session(environ) as target:
            self.assertEqual(target, self.target)
        self.assertEqual(lint_cache.dir_bytes(self.target) > 0, True)

    def test_managed_directory_is_named_by_the_executing_host(self):
        cache = self._tmp.name
        shipped = os.path.join(cache, "hams-lint-cargo", "hams_open-1.98.1-deadbeef")  # named by the dispatcher's toolchain
        environ = {"XDG_CACHE_HOME": cache, "CARGO_TARGET_DIR": shipped, "HAMS_LINT_CARGO_MANAGED": shipped,
                   "HAMS_LINT_CARGO_REPO": "hams_open"}
        with lint_cache.cargo_session(environ) as target:
            self.assertEqual(target, lint_cache.cargo_target_dir("hams_open", environ))
            self.assertTrue(os.path.isdir(target))

    def test_no_cache_uses_a_throwaway_directory(self):
        environ = {"CARGO_TARGET_DIR": self.target, "HAMS_LINT_CARGO_MANAGED": self.target, "HAMS_LINT_NO_CACHE": "1"}
        with lint_cache.cargo_session(environ) as target:
            self.assertNotEqual(target, self.target)
            self.assertTrue(os.path.isdir(target))
        self.assertFalse(os.path.exists(target))

    # Tests [@ANCHOR: lint_cache:named_cargo_session]
    def test_named_session_directory_is_under_the_budgeted_cargo_base(self):
        cache = self._tmp.name
        environ = {"XDG_CACHE_HOME": cache}
        with lint_cache.named_cargo_session("relay-export-test", environ) as target:
            self.assertEqual(target, lint_cache.cargo_target_dir("relay-export-test", environ))
            self.assertEqual(os.path.dirname(target), lint_cache.cargo_base(environ))
            self.assertTrue(os.path.isdir(target))
            self.assertFalse(locked_free(target))  # the lock is held for the whole build
        self.assertTrue(locked_free(target))
        self.assertTrue(any("relay-export-test" in line for line in lint_cache.show_lines(environ)))

    # Tests [@ANCHOR: lint_cache:named_cargo_session]
    def test_named_session_prunes_before_use_and_clears_when_pruning_is_not_enough(self):
        cache = self._tmp.name
        base = {"XDG_CACHE_HOME": cache, "HAMS_LINT_CARGO_GB": str(9000 / lint_cache.GB), "HAMS_LINT_FREE_FLOOR_GB": "0"}
        target = lint_cache.cargo_target_dir("relay-export-test", base)
        profile = os.path.join(target, "debug")
        old = make_unit(profile, "old", "aaaaaaaaaaaaaaaa", 4000, 5000)
        new = make_unit(profile, "new", "cccccccccccccccc", 4000, 10)
        make_unit(profile, "mid", "bbbbbbbbbbbbbbbb", 4000, 3000)
        with lint_cache.named_cargo_session("relay-export-test", base):
            self.assertFalse(any(os.path.exists(p) for p in old))  # least recently used went first
            self.assertTrue(all(os.path.exists(p) for p in new))
        tiny = {**base, "HAMS_LINT_CARGO_GB": "0"}
        with lint_cache.named_cargo_session("relay-export-test", tiny) as cleared:
            self.assertEqual(cleared, target)
            self.assertEqual(lint_cache.dir_bytes(cleared), 0)  # cleared, not failed

    # Tests [@ANCHOR: lint_cache:named_cargo_session]
    def test_named_session_leaves_an_explicit_cargo_target_dir_alone(self):
        make_unit(self.profile, "a", "0123456789abcdef", 1000, 10)
        environ = {"XDG_CACHE_HOME": self._tmp.name, "CARGO_TARGET_DIR": self.target, "HAMS_LINT_CARGO_GB": "0"}
        with lint_cache.named_cargo_session("relay-export-test", environ) as target:
            self.assertEqual(target, self.target)
        self.assertGreater(lint_cache.dir_bytes(self.target), 0)

    # Tests [@ANCHOR: lint_cache:named_cargo_session]
    def test_named_session_no_cache_uses_a_throwaway_directory(self):
        environ = {"XDG_CACHE_HOME": self._tmp.name, "HAMS_LINT_NO_CACHE": "1"}
        with lint_cache.named_cargo_session("relay-export-test", environ) as target:
            self.assertTrue(os.path.isdir(target))
            self.assertNotEqual(os.path.dirname(target), lint_cache.cargo_base(environ))
        self.assertFalse(os.path.exists(target))

    # Tests [@ANCHOR: lint_cache:named_cargo_session]
    def test_named_session_name_is_part_of_the_directory(self):
        environ = {"XDG_CACHE_HOME": self._tmp.name}
        with lint_cache.named_cargo_session("other-test-build", environ) as target:
            self.assertTrue(os.path.basename(target).startswith("other-test-build-"))

    # Tests [@ANCHOR: lint_cache:other_caches]
    def test_unbudgeted_caches_are_reported_with_sizes(self):
        home = os.path.join(self._tmp.name, "home")
        for relative, size in (("hams-lint-mirror/repo/file", 3000), (".cache/ms-playwright/chromium/file", 2000)):
            path = os.path.join(home, relative)
            os.makedirs(os.path.dirname(path))
            with open(path, "wb") as handle:
                handle.write(b"x" * size)
        environ = {"HOME": home, "XDG_CACHE_HOME": os.path.join(home, ".cache")}
        paths = {entry[1] for entry in lint_cache.other_caches(environ)}
        self.assertEqual(paths, {os.path.join(home, "hams-lint-mirror"), "/opt/hams-testsrc",
                                 os.path.join(home, ".cache", "ms-playwright"), "/var/lib/machines"})
        text = "\n".join(lint_cache.show_lines(environ))
        self.assertIn(f"other {os.path.join(home, 'hams-lint-mirror')}: 0.00 GB (not budgeted:", text)
        self.assertIn("ms-playwright", text)

    # Tests [@ANCHOR: lint_cache:legacy_cargo_dirs]
    def test_old_export_test_directory_is_reported_and_cleared_with_the_cargo_caches(self):
        cache = self._tmp.name
        environ = {"XDG_CACHE_HOME": cache}
        self.assertEqual([os.path.basename(p) for p in lint_cache.legacy_cargo_dirs(environ)],
                         ["hams-cargo-target-lint", "hams-relay-export-test-target"])
        old = os.path.join(cache, "hams-relay-export-test-target")
        make_unit(os.path.join(old, "debug"), "a", "0123456789abcdef", 1000, 10)
        self.assertTrue(any("hams-relay-export-test-target" in line for line in lint_cache.show_lines(environ)))
        with unittest.mock.patch.dict(os.environ, environ), unittest.mock.patch("builtins.print"):
            self.assertEqual(lint_cache.main(["clear", "cargo"]), 0)
        self.assertFalse(os.path.exists(old))

    def test_legacy_directory_is_adopted_once(self):
        cache = self._tmp.name
        legacy = os.path.join(cache, "hams-cargo-target-lint")
        os.makedirs(os.path.join(legacy, "debug", "incremental", "a-0123456789abcdef"))
        os.makedirs(os.path.join(legacy, "debug", "deps"))
        self.assertEqual(lint_cache.adopt_legacy_dir(self.target, {"XDG_CACHE_HOME": cache}), legacy)
        self.assertTrue(os.path.isdir(os.path.join(self.target, "debug", "deps")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "debug", "incremental")))  # never read again
        self.assertIsNone(lint_cache.adopt_legacy_dir(self.target, {"XDG_CACHE_HOME": cache}))


class BudgetTableTest(unittest.TestCase):
    def test_machine_budget_from_the_inventory_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"cache_budgets": {"default": {"cargo_gb": 7}, "m": {"free_floor_gb": 3}}}, handle)
        self.addCleanup(os.unlink, handle.name)
        got = lint_cache.budgets({"HAMS_LINT_MACHINE": "m"}, machines_file=handle.name)
        self.assertEqual((got["cargo_gb"], got["free_floor_gb"]), (7, 3))
        got = lint_cache.budgets({"HAMS_LINT_MACHINE": "m", "HAMS_LINT_CARGO_GB": "2"}, machines_file=handle.name)
        self.assertEqual(got["cargo_gb"], 2)

    def test_nan_and_negative_overrides_are_ignored(self):
        got = lint_cache.budgets({"HAMS_LINT_MACHINE": "devbox", "HAMS_LINT_CARGO_GB": "nan", "HAMS_LINT_FREE_FLOOR_GB": "-5"})
        self.assertEqual((got["cargo_gb"], got["free_floor_gb"]), (25, 30))  # the devbox row of lint_machines.json

    def test_shipped_inventory_has_a_budget_for_every_runner(self):
        with open(lint_cache.MACHINES_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        for name, machine in data["machines"].items():
            if machine.get("runner", True) and not machine.get("never"):
                self.assertIn(name, data["cache_budgets"], name)


class CheckerVersionTest(unittest.TestCase):
    """The result cache's version is the checker's source, not where the checkout happens to be."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def copy_of(self, text, *parts):
        path = os.path.join(self._tmp.name, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    # Tests [@ANCHOR: lint_cache:checker_version]
    def test_same_source_in_two_checkouts_has_one_version(self):
        first = self.copy_of("print(1)\n", "batch_1", "tools", "chk.py")
        second = self.copy_of("print(1)\n", "batch_2_other_path", "tools", "chk.py")
        self.assertEqual(lint_cache.checker_version(first), lint_cache.checker_version(second))

    # Tests [@ANCHOR: lint_cache:checker_version]
    def test_an_edit_to_the_source_changes_the_version(self):
        first = self.copy_of("print(1)\n", "a", "chk.py")
        second = self.copy_of("print(2)\n", "b", "chk.py")
        self.assertNotEqual(lint_cache.checker_version(first), lint_cache.checker_version(second))

    # Tests [@ANCHOR: lint_cache:checker_version]
    def test_a_different_file_name_with_the_same_text_is_a_different_version(self):
        first = self.copy_of("print(1)\n", "a", "chk_one.py")
        second = self.copy_of("print(1)\n", "a", "chk_two.py")
        self.assertNotEqual(lint_cache.checker_version(first), lint_cache.checker_version(second))

    # Tests [@ANCHOR: lint_cache:checker_version]
    def test_the_order_of_the_paths_does_not_matter(self):
        one = self.copy_of("a\n", "d", "one.py")
        two = self.copy_of("b\n", "d", "two.py")
        self.assertEqual(lint_cache.checker_version(one, two), lint_cache.checker_version(two, one))

    # Tests [@ANCHOR: lint_cache:checker_version]
    def test_a_real_checker_run_from_two_checkouts_shares_one_cache_file(self):
        here = os.path.dirname(os.path.abspath(__file__))
        repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(repo)
        with open(os.path.join(repo, "m.py"), "w", encoding="utf-8") as handle:
            handle.write("x = 1\n")
        cache_root = os.path.join(self._tmp.name, "cache")
        env = {k: v for k, v in os.environ.items() if k != "HAMS_LINT_NO_CACHE"}
        env["XDG_CACHE_HOME"] = cache_root
        for checkout in ("worktree_a", "worktree_b_a_different_path"):
            tools = os.path.join(self._tmp.name, checkout, "tools")
            os.makedirs(tools)
            for name in ("check_float_isfinite.py", "lint_cache.py"):
                shutil.copy(os.path.join(here, name), tools)
            run = subprocess.run([sys.executable, os.path.join(tools, "check_float_isfinite.py"), repo],
                                 env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        stored = os.listdir(os.path.join(cache_root, "hams-lint-results", "check_float_isfinite"))
        self.assertEqual(len([n for n in stored if n.endswith(".json")]), 1, stored)


class ResultCacheTest(unittest.TestCase):
    def test_save_prunes_only_its_own_cache_root(self):
        with tempfile.TemporaryDirectory() as mine, tempfile.TemporaryDirectory() as other:
            stale = os.path.join(other, "hams-lint-results", "old", "v0.json")
            os.makedirs(os.path.dirname(stale))
            open(stale, "w").close()
            long_ago = time.time() - 60 * 86400
            os.utime(stale, (long_ago, long_ago))
            source = os.path.join(mine, "m.py")
            open(source, "w").write("x\n")
            with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": other}):
                cache = lint_cache.ResultCache("chk", "v1", {"XDG_CACHE_HOME": mine})
                cache.put(source, [])
                cache.save()
            self.assertTrue(os.path.exists(stale))  # another cache root is never touched

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.environ = {"XDG_CACHE_HOME": self._tmp.name}
        self.source = os.path.join(self._tmp.name, "mod.py")

    def write(self, text):
        with open(self.source, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_hit_after_put_and_miss_after_the_file_changes(self):
        self.write("x = 1\n")
        cache = lint_cache.ResultCache("chk", "v1", self.environ)
        self.assertIsNone(cache.get(self.source))
        cache.put(self.source, [["finding", 3]])
        cache.save()
        again = lint_cache.ResultCache("chk", "v1", self.environ)
        self.assertEqual(again.get(self.source), [["finding", 3]])
        self.write("x = 2\n")  # same path, same size, new content: the entry must not be used
        self.assertIsNone(lint_cache.ResultCache("chk", "v1", self.environ).get(self.source))
        self.write("x = 1\n")  # back to the old content: the old entry is valid again
        self.assertEqual(lint_cache.ResultCache("chk", "v1", self.environ).get(self.source), [["finding", 3]])

    def test_new_checker_version_starts_empty(self):
        self.write("x = 1\n")
        cache = lint_cache.ResultCache("chk", "v1", self.environ)
        cache.put(self.source, [])
        cache.save()
        self.assertIsNone(lint_cache.ResultCache("chk", "v2", self.environ).get(self.source))

    def test_no_cache_never_reads_or_writes(self):
        self.write("x = 1\n")
        cache = lint_cache.ResultCache("chk", "v1", self.environ)
        cache.put(self.source, [])
        cache.save()
        off = lint_cache.ResultCache("chk", "v1", dict(self.environ, HAMS_LINT_NO_CACHE="1"))
        self.assertIsNone(off.get(self.source))
        off.put(self.source, ["other"])
        off.save()
        self.assertEqual(lint_cache.ResultCache("chk", "v1", self.environ).get(self.source), [])

    def test_two_writers_merge(self):
        self.write("a\n")
        other = os.path.join(self._tmp.name, "o.py")
        with open(other, "w") as handle:
            handle.write("b\n")
        first = lint_cache.ResultCache("chk", "v1", self.environ)
        second = lint_cache.ResultCache("chk", "v1", self.environ)
        first.put(self.source, ["A"])
        second.put(other, ["B"])
        first.save()
        second.save()
        merged = lint_cache.ResultCache("chk", "v1", self.environ)
        self.assertEqual((merged.get(self.source), merged.get(other)), (["A"], ["B"]))

    def test_cap_drops_the_oldest_entries(self):
        environ = dict(self.environ, HAMS_LINT_RESULTS_MB="0.001")  # about 1 kB
        cache = lint_cache.ResultCache("chk", "v1", environ)
        for index in range(40):
            path = os.path.join(self._tmp.name, f"f{index}.py")
            with open(path, "w") as handle:
                handle.write(f"v{index}\n")
            cache.put(path, ["r" * 20])
            cache.entries[lint_cache.file_digest(path)]["t"] = index
            cache.new[lint_cache.file_digest(path)]["t"] = index
        cache.save()
        self.assertLessEqual(os.path.getsize(cache.path), 1100)


if __name__ == "__main__":
    unittest.main()
