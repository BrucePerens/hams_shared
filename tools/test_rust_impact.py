#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for rust_impact.py: which crates a `--changed` clippy or fmt run checks. Real throwaway crate trees (manifests only: no cargo is needed to
read a dependency) and, for the end-to-end test, a fake `cargo` on PATH that records the directories it was started in.

Tests [@ANCHOR: rust_impact:split_argv]
Tests [@ANCHOR: rust_impact:path_dependencies]
Tests [@ANCHOR: rust_impact:select_crates]
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

import rust_impact  # noqa: E402


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="rustimpact-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def crate(self, name, deps=(), extra=""):
        directory = os.path.join(self.root, name)
        os.makedirs(os.path.join(directory, "src"), exist_ok=True)
        lines = ['[package]', 'name = "%s"' % name.replace("/", "_"), 'version = "0.1.0"', "", "[dependencies]"]
        for dep in deps:
            lines.append('%s = { path = "%s" }' % (os.path.basename(dep), os.path.relpath(os.path.join(self.root, dep), directory)))
        with open(os.path.join(directory, "Cargo.toml"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n" + extra)
        with open(os.path.join(directory, "src", "main.rs"), "w", encoding="utf-8") as handle:
            handle.write("fn main() {}\n")
        return directory

    def path(self, *parts):
        return os.path.join(self.root, *parts)


class SelectionTests(Tree):
    def test_only_the_crate_with_a_changed_file_and_its_dependents_are_selected(self):
        lib = self.crate("lib")
        app = self.crate("app", deps=["lib"])
        tool = self.crate("tool", deps=["app"])
        other = self.crate("other")
        crates = [lib, app, tool, other]
        selected, skipped = rust_impact.select_crates(crates, [self.path("other", "src", "main.rs")])
        self.assertEqual(selected, [other])
        self.assertEqual([crate for crate, _why in skipped], [lib, app, tool])
        selected, _ = rust_impact.select_crates(crates, [self.path("lib", "src", "main.rs")])
        self.assertEqual(selected, [lib, app, tool], "dependents follow through a chain")
        selected, _ = rust_impact.select_crates(crates, [self.path("app", "Cargo.lock")])
        self.assertEqual(selected, [app, tool])

    def test_a_dependency_that_is_not_a_listed_crate_still_counts(self):
        shared = os.path.join(self.root, "vendor", "shared")
        os.makedirs(shared)
        app = self.crate("app", deps=["vendor/shared"])
        other = self.crate("other")
        selected, skipped = rust_impact.select_crates([app, other], [os.path.join(shared, "data.txt")])
        self.assertEqual(selected, [app])
        self.assertEqual(rust_impact.select_crates([app, other], [os.path.join(shared, "lib.rs")])[0], [app, other], "a Rust file outside every crate may be include!d: all")
        self.assertEqual(len(skipped), 1)

    def test_unrelated_files_select_nothing_and_a_missing_list_selects_everything(self):
        a, b = self.crate("a"), self.crate("b")
        selected, skipped = rust_impact.select_crates([a, b], [self.path("docs", "x.py")])
        self.assertEqual((selected, [crate for crate, _why in skipped]), ([], [a, b]))
        self.assertEqual(rust_impact.select_crates([a, b], None), ([a, b], []))

    def test_what_it_cannot_judge_checks_every_crate(self):
        a, b = self.crate("a"), self.crate("b")
        for outside in (self.path("shared", "gen.rs"), self.path("rust-toolchain.toml"), self.path(".cargo", "config.toml")):
            self.assertEqual(rust_impact.select_crates([a, b], [outside])[0], [a, b], outside)
        with open(os.path.join(b, "Cargo.toml"), "w", encoding="utf-8") as handle:
            handle.write("this is [not toml\n")
        selected, skipped = rust_impact.select_crates([a, b], [self.path("a", "src", "main.rs")])
        self.assertEqual(selected, [a, b], "a manifest that does not parse is checked")
        self.assertEqual(skipped, [])

    def test_a_dependency_cycle_does_not_loop(self):
        a = self.crate("a", deps=["b"])
        b = self.crate("b", deps=["a"])
        selected, _ = rust_impact.select_crates([a, b], [self.path("a", "src", "main.rs")])
        self.assertEqual(selected, [a, b])


class DependencyTests(Tree):
    def test_every_table_that_can_hold_a_path_is_read(self):
        extra = (
            '[dev-dependencies]\nd = { path = "../d" }\n[build-dependencies]\nb = { path = "../b" }\n'
            '[target.\'cfg(unix)\'.dependencies]\nt = { path = "../t" }\n[patch.crates-io]\np = { path = "../p" }\n'
            '[workspace.dependencies]\nw = { path = "../w" }\n[dependencies.v]\nversion = "1"\n'
        )
        crate = self.crate("c", deps=["a"], extra=extra)
        self.assertEqual(sorted(rust_impact.path_dependencies(crate)), sorted(self.path(name) for name in "abdptw"))
        self.assertIsNone(rust_impact.path_dependencies(self.path("missing")))

    def test_the_command_line_splits_at_the_flag_and_resolves_the_paths(self):
        self.assertEqual(rust_impact.split_argv(["repo"]), (["repo"], None))
        positional, changed = rust_impact.split_argv(["repo", rust_impact.CHANGED_FLAG, "a/../b.rs"])
        self.assertEqual(positional, ["repo"])
        self.assertEqual(changed, [os.path.realpath("b.rs")])
        self.assertEqual(rust_impact.split_argv(["repo", rust_impact.CHANGED_FLAG]), (["repo"], []), "an empty list: nothing changed")


class EndToEndTests(Tree):
    def test_fmt_and_clippy_start_cargo_only_in_the_selected_crates_and_name_the_skipped_ones(self):
        bin_dir = self.path("bin")
        os.makedirs(bin_dir)
        record = self.path("record.txt")
        fake = os.path.join(bin_dir, "cargo")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nif [ "$2" = "--version" ]; then echo ok; exit 0; fi\necho "$1 $(pwd)" >> %s\nexit 0\n' % record)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        repo = self.path("repo")
        os.makedirs(os.path.join(repo, "daemons"))
        for name in ("lib", "app", "other"):
            os.makedirs(os.path.join(repo, "daemons", name, "src"))
        for name, deps in (("lib", ""), ("app", 'lib = { path = "../lib" }\n'), ("other", "")):
            with open(os.path.join(repo, "daemons", name, "Cargo.toml"), "w", encoding="utf-8") as handle:
                handle.write('[package]\nname = "%s"\nversion = "0.1.0"\n[dependencies]\n%s' % (name, deps))
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"], HAMS_LINT_NO_CACHE="1", XDG_CACHE_HOME=self.path("cache"))
        changed = os.path.join(repo, "daemons", "lib", "src", "main.rs")
        outputs = {}
        for script in ("check_cargo_fmt.py", "check_cargo_clippy.py"):
            done = subprocess.run([sys.executable, os.path.join(HERE, script), repo, rust_impact.CHANGED_FLAG, changed],
                                  capture_output=True, text=True, env=env, cwd=self.root)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            outputs[script] = done.stdout
        with open(record, encoding="utf-8") as handle:
            started = handle.read().splitlines()
        self.assertEqual(sorted(started), sorted(["fmt " + os.path.join(repo, "daemons", "app"), "fmt " + os.path.join(repo, "daemons", "lib"),
                                                  "clippy " + os.path.join(repo, "daemons", "app"), "clippy " + os.path.join(repo, "daemons", "lib")]))
        self.assertIn("skipped", outputs["check_cargo_fmt.py"])
        self.assertIn("other", outputs["check_cargo_fmt.py"])
        self.assertIn("2 of 3 crates checked", outputs["check_cargo_clippy.py"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
