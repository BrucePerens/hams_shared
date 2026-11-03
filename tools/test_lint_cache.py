#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_cache: the cargo target budget (LRU pruning by unit, free-space floor, fail-safe clear), the
locking, the managed-directory rule and the per-file result cache (a changed file never reads an old entry)."""

import json
import os
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

    def test_legacy_directory_is_adopted_once(self):
        cache = self._tmp.name
        legacy = os.path.join(cache, "hams-cargo-target-lint")
        os.makedirs(os.path.join(legacy, "debug"))
        self.assertEqual(lint_cache.adopt_legacy_dir(self.target, {"XDG_CACHE_HOME": cache}), legacy)
        self.assertTrue(os.path.isdir(os.path.join(self.target, "debug")))
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

    def test_shipped_inventory_has_a_budget_for_every_runner(self):
        with open(lint_cache.MACHINES_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        for name, machine in data["machines"].items():
            if machine.get("runner", True) and not machine.get("never"):
                self.assertIn(name, data["cache_budgets"], name)


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
