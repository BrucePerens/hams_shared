#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_dependency_cycles.py.

hams_shared/tools/ has historically had zero automated test coverage (every
change verified by hand, once, and never again). This is the first coverage
for this specific script, following the same pattern already established for
check_burn_list.py: exercise the real functions against real temp-directory
fixtures (actual __manifest__.py files on disk for _build_graph, since that's
the function real bugs would hide in) rather than only unit-testing the pure
graph helpers in isolation.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_dependency_cycles as cdc  # noqa: E402


def _write_manifest(repo_root, module_name, depends=None, depends_cycle=None):
    mod_dir = os.path.join(repo_root, module_name)
    os.makedirs(mod_dir, exist_ok=True)
    lines = ["{"]
    if depends is not None:
        lines.append(f"    'depends': {depends!r},")
    if depends_cycle is not None:
        lines.append(f"    'depends_cycle': {depends_cycle!r},")
    lines.append("}")
    with open(os.path.join(mod_dir, "__manifest__.py"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class BuildGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "hams_open")
        os.makedirs(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_builds_a_graph_from_real_manifest_files_on_disk(self):
        _write_manifest(self.repo, "mod_a", depends=["mod_b"])
        _write_manifest(self.repo, "mod_b", depends=[])
        graph, depends_cycle = cdc._build_graph([self.repo])
        self.assertEqual(graph["mod_a"], ["mod_b"])
        self.assertEqual(graph["mod_b"], [])
        self.assertEqual(depends_cycle, {})

    def test_captures_a_real_depends_cycle_manifest_key(self):
        _write_manifest(self.repo, "mod_a", depends=["mod_b"], depends_cycle=["mod_c"])
        graph, depends_cycle = cdc._build_graph([self.repo])
        self.assertEqual(depends_cycle["mod_a"], ["mod_c"])

    def test_ignores_directories_without_a_manifest(self):
        os.makedirs(os.path.join(self.repo, "not_a_module"))
        with open(os.path.join(self.repo, "not_a_module", "readme.txt"), "w") as f:
            f.write("not a module")
        graph, _ = cdc._build_graph([self.repo])
        self.assertEqual(graph, {})

    def test_skips_a_manifest_with_a_syntax_error_instead_of_crashing(self):
        mod_dir = os.path.join(self.repo, "broken_mod")
        os.makedirs(mod_dir)
        with open(os.path.join(mod_dir, "__manifest__.py"), "w") as f:
            f.write("{ this is not valid python")
        graph, _ = cdc._build_graph([self.repo])
        self.assertEqual(graph, {})

    def test_does_not_descend_into_a_daemons_or_tools_directory(self):
        # Real repos keep a `daemons/` and `tools/` dir at the root that are
        # never Odoo modules; a manifest-shaped file placed under one must
        # not be picked up as if it were a real module.
        _write_manifest(os.path.join(self.repo, "daemons"), "not_really_a_module")
        graph, _ = cdc._build_graph([self.repo])
        self.assertNotIn("not_really_a_module", graph)


class ReachableTests(unittest.TestCase):
    def test_direct_edge_is_reachable(self):
        graph = {"a": ["b"], "b": []}
        self.assertTrue(cdc._reachable(graph, "a", "b"))

    def test_multi_hop_edge_is_reachable(self):
        graph = {"a": ["b"], "b": ["c"], "c": []}
        self.assertTrue(cdc._reachable(graph, "a", "c"))

    def test_unrelated_modules_are_not_reachable(self):
        graph = {"a": ["b"], "b": [], "c": []}
        self.assertFalse(cdc._reachable(graph, "a", "c"))

    def test_a_module_absent_from_the_graph_is_not_reachable_from_anything(self):
        graph = {"a": ["b"]}
        self.assertFalse(cdc._reachable(graph, "a", "does_not_exist"))


class FindCyclesTests(unittest.TestCase):
    def test_an_acyclic_graph_reports_no_cycles(self):
        graph = {"a": ["b"], "b": ["c"], "c": []}
        self.assertEqual(cdc._find_cycles(graph), [])

    def test_a_direct_two_module_cycle_is_detected(self):
        graph = {"a": ["b"], "b": ["a"]}
        cycles = cdc._find_cycles(graph)
        self.assertEqual(len(cycles), 1)
        self.assertIn("a", cycles[0])
        self.assertIn("b", cycles[0])

    def test_a_longer_cycle_is_detected(self):
        graph = {"a": ["b"], "b": ["c"], "c": ["a"]}
        cycles = cdc._find_cycles(graph)
        self.assertEqual(len(cycles), 1)

    def test_a_dependency_outside_the_graph_does_not_crash_or_count_as_a_cycle(self):
        # A 'depends' entry naming a core Odoo module (e.g. 'base', 'mail')
        # never appears as a key in our graph -- must be skipped, not treated
        # as a missing/False-y cycle participant.
        graph = {"a": ["base", "mail"]}
        self.assertEqual(cdc._find_cycles(graph), [])


class CheckDependsCycleEntriesTests(unittest.TestCase):
    def test_a_legitimate_depends_cycle_entry_produces_no_error(self):
        # mod_b really does depend on mod_a (a real cycle would exist if
        # mod_a hard-depended on mod_b), so declaring it via depends_cycle
        # instead of a real 'depends' entry is exactly the documented use.
        graph = {"a": [], "b": ["a"]}
        depends_cycle = {"a": ["b"]}
        errors = cdc._check_depends_cycle_entries(graph, depends_cycle)
        self.assertEqual(errors, [])

    def test_a_redundant_depends_cycle_entry_is_flagged(self):
        graph = {"a": ["b"], "b": []}
        depends_cycle = {"a": ["b"]}
        errors = cdc._check_depends_cycle_entries(graph, depends_cycle)
        self.assertEqual(len(errors), 1)
        self.assertIn("REDUNDANT", errors[0])

    def test_an_unknown_depends_cycle_target_is_flagged(self):
        graph = {"a": []}
        depends_cycle = {"a": ["does_not_exist"]}
        errors = cdc._check_depends_cycle_entries(graph, depends_cycle)
        self.assertEqual(len(errors), 1)
        self.assertIn("UNKNOWN", errors[0])

    def test_an_unjustified_depends_cycle_entry_is_flagged(self):
        # b does not actually depend (even transitively) on a, so a hard
        # 'depends' entry from a to b would never have created a cycle --
        # depends_cycle should not have been used here at all.
        graph = {"a": [], "b": []}
        depends_cycle = {"a": ["b"]}
        errors = cdc._check_depends_cycle_entries(graph, depends_cycle)
        self.assertEqual(len(errors), 1)
        self.assertIn("UNJUSTIFIED", errors[0])


class ResolveRepoRootTests(unittest.TestCase):
    # Regression test for a real bug found the same night check_model_extension_collisions.py's
    # own identical bug was found and fixed: run_linters.py's own dir_path resolves to
    # .../hams_shared, not a real repo root -- confirmed directly, this checker was silently
    # building a 0-node dependency graph via run_linters.py's actual invocation (51 nodes at a
    # real repo root).
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            cdc._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(cdc._resolve_repo_root(fake_repo), fake_repo)


if __name__ == "__main__":
    unittest.main()
