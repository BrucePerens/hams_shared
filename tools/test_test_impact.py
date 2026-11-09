#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for test_impact.py, the per-PR test selector. Every test builds a small fake module tree in a temporary
directory: no git, no Odoo.

Tests [@ANCHOR: test_impact:Impact]
Tests [@ANCHOR: test_impact:module_dirs]
Tests [@ANCHOR: test_impact:read_depends]
Tests [@ANCHOR: test_impact:reverse_closure]
Tests [@ANCHOR: test_impact:classify_path]
Tests [@ANCHOR: test_impact:test_classes]
Tests [@ANCHOR: test_impact:analyze]
Tests [@ANCHOR: test_impact:build_command]
Tests [@ANCHOR: test_impact:changed_files]
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_impact as ti  # noqa: E402


def make_tree(root):
    """base <- mid <- top ; lonely depends on base only through `web` (not ours); open_mod lives in a second root."""
    layout = {
        "com/base": [], "com/mid": ["base"], "com/top": ["mid", "web"], "com/lonely": ["web"], "open/open_mod": ["base"],
    }
    for key, depends in layout.items():
        path = os.path.join(root, key)
        os.makedirs(os.path.join(path, "tests"))
        with open(os.path.join(path, "__manifest__.py"), "w") as handle:
            handle.write(repr({"name": key, "depends": depends}))
    with open(os.path.join(root, "com/mid/tests/test_a.py"), "w") as handle:
        handle.write("class TestOne(Base):\n    pass\n\nclass TestTwo(Base):\n    pass\n\nclass Helper:\n    pass\n")
    with open(os.path.join(root, "com/top/tests/test_b.py"), "w") as handle:
        handle.write("def not_a_class():\n    pass\n")
    return [os.path.join(root, "com"), os.path.join(root, "open")]


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.roots = make_tree(os.path.realpath(self.tmp.name))
        self.modules = ti.module_dirs(self.roots)
        self.com = self.roots[0]

    def run_for(self, *paths):
        return ti.analyze([(self.com, p) for p in paths], self.modules)

    def test_module_dirs_finds_manifest_dirs_in_all_roots(self):
        self.assertEqual(sorted(self.modules), ["base", "lonely", "mid", "open_mod", "top"])
        self.assertEqual(ti.module_dirs(["/nonexistent"]), {})

    def test_read_depends_tolerates_bad_manifests(self):
        self.assertEqual(ti.read_depends(self.modules["top"]), ["mid", "web"])
        bad = os.path.join(self.tmp.name, "bad")
        os.makedirs(bad)
        with open(os.path.join(bad, "__manifest__.py"), "w") as handle:
            handle.write("not python (")
        self.assertEqual(ti.read_depends(bad), [])
        self.assertEqual(ti.read_depends(os.path.join(self.tmp.name, "missing")), [])

    def test_reverse_closure_is_transitive_and_crosses_repositories(self):
        self.assertEqual(ti.reverse_closure({"base"}, self.modules), ["base", "mid", "open_mod", "top"])
        self.assertEqual(ti.reverse_closure({"top"}, self.modules), ["top"])
        self.assertEqual(ti.reverse_closure({"mid"}, self.modules), ["mid", "top"])

    def test_classify_path_rules(self):
        expect = {
            "__manifest__.py": "full", "security/ir.model.access.csv": "full", "data/x.xml": "full", "views/v.xml": "full",
            "models/m.py": "code", "controllers/c.py": "code", "static/src/a.js": "code", "static/src/t.xml": "code",
            "tests/test_a.py": "test", "tests/common.py": "code", "tests/__init__.py": "code", "wizard/w.xml": "full",
            "README.md": "doc", "docs/x.png": "doc", "i18n/de.po": "full", "weird.bin": "full",
        }
        for rel, category in expect.items():
            self.assertEqual(ti.classify_path(rel)[0], category, rel)

    def test_test_classes_reads_only_classes_with_bases(self):
        self.assertEqual(ti.test_classes(os.path.join(self.modules["mid"], "tests/test_a.py")), ["TestOne", "TestTwo"])
        self.assertEqual(ti.test_classes(os.path.join(self.modules["top"], "tests/test_b.py")), [])
        self.assertEqual(ti.test_classes("/nonexistent/test_x.py"), [])

    def test_code_change_runs_module_and_dependents(self):
        impact = self.run_for("base/models/m.py")
        self.assertEqual((impact.kind, impact.modules), (ti.KIND_TARGETED, ["base", "mid", "open_mod", "top"]))
        self.assertIsNone(impact.test_tags)

    def test_leaf_code_change_runs_only_itself(self):
        self.assertEqual(self.run_for("lonely/models/m.py").modules, ["lonely"])

    def test_only_test_files_narrow_to_classes(self):
        impact = self.run_for("mid/tests/test_a.py")
        self.assertEqual(impact.kind, ti.KIND_TESTS)
        self.assertEqual(impact.modules, ["mid"])
        self.assertEqual(impact.test_tags, ["/mid:TestOne", "/mid:TestTwo"])
        self.assertIn("--test-tags /mid:TestOne,/mid:TestTwo", ti.build_command(impact))

    def test_test_file_without_class_is_not_narrowed(self):
        impact = self.run_for("top/tests/test_b.py")
        self.assertEqual((impact.modules, impact.test_tags), (["top"], None))

    def test_deleted_test_file_runs_the_module_whole(self):
        impact = self.run_for("lonely/tests/test_gone.py")
        self.assertEqual((impact.modules, impact.test_tags), (["lonely"], None))

    def test_code_plus_test_only_module_is_not_narrowed(self):
        impact = self.run_for("lonely/models/m.py", "mid/tests/test_a.py")
        self.assertEqual(impact.kind, ti.KIND_TARGETED)
        self.assertEqual(impact.modules, ["lonely", "mid"])
        self.assertIsNone(impact.test_tags)

    def test_unmappable_changes_run_everything(self):
        everything = sorted(self.modules)
        for path in ("base/__manifest__.py", "base/security/ir.model.access.csv", "base/views/v.xml", "base/data/d.xml",
                     "hams_shared/tools/test.py", "daemons/x/main.py", "base/weird.bin", "hams_shared", "setup.cfg"):
            impact = self.run_for(path)
            self.assertEqual((impact.kind, impact.modules), (ti.KIND_FULL, everything), path)

    def test_full_beats_everything_else(self):
        self.assertEqual(self.run_for("lonely/models/m.py", "base/__manifest__.py").kind, ti.KIND_FULL)

    def test_documentation_only_means_nothing_to_run(self):
        impact = self.run_for("docs/adrs/0001.md", "night_shift_history/x.md", "README.md")
        self.assertEqual(impact.kind, ti.KIND_NONE)
        self.assertTrue(ti.build_command(impact).startswith("#"))

    def test_module_documentation_runs_that_module_whole(self):
        impact = self.run_for("mid/README.md")
        self.assertEqual((impact.kind, impact.modules, impact.test_tags), (ti.KIND_TESTS, ["mid"], None))

    def test_no_changes_means_nothing_to_run(self):
        self.assertEqual(self.run_for().kind, ti.KIND_NONE)

    def test_build_command_for_full_lists_every_module(self):
        command = ti.build_command(self.run_for("base/__manifest__.py"))
        self.assertEqual(command, "python3 hams_shared/tools/test.py --wait -u base,lonely,mid,open_mod,top")

    def test_changed_files_reads_git(self):
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(repo)
        def git(*a):
            subprocess.run(["git", "-C", repo, "-c", "user.email=a@b", "-c", "user.name=t", *a], check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        with open(os.path.join(repo, "a.txt"), "w") as handle:
            handle.write("1")
        git("add", "a.txt")
        git("commit", "-q", "-m", "one")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("switch", "-q", "-c", "work")
        with open(os.path.join(repo, "b.txt"), "w") as handle:
            handle.write("2")
        git("add", "b.txt")
        git("commit", "-q", "-m", "two")
        with open(os.path.join(repo, "c.txt"), "w") as handle:
            handle.write("3")
        self.assertEqual(ti.changed_files(repo), ["b.txt", "c.txt"])
        with self.assertRaises(RuntimeError):
            ti.changed_files(repo, "no/such-ref")


if __name__ == "__main__":
    unittest.main()
