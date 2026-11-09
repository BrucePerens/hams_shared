# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for hams_lint_bin.py (the engine binary, built on demand and keyed by its sources) and, through
it, for the Rust engine itself: its unit tests, its rule-file examples and its refusal of a bad rule file."""

import os
import shutil
import subprocess
import tempfile
import unittest

import hams_lint_bin
import lint_cache


def _copy_crate(dest):
    for name in ("Cargo.toml", "Cargo.lock"):
        shutil.copy2(os.path.join(hams_lint_bin.CRATE_DIR, name), dest)
    for sub in ("src", "rules"):
        shutil.copytree(os.path.join(hams_lint_bin.CRATE_DIR, sub), os.path.join(dest, sub))


class SourceHashTests(unittest.TestCase):
    def setUp(self):
        self.crate = tempfile.mkdtemp(prefix="hams_lint_crate_")
        self.addCleanup(shutil.rmtree, self.crate)
        _copy_crate(self.crate)

    def _edit(self, rel, extra):
        with open(os.path.join(self.crate, rel), "a", encoding="utf-8") as handle:
            handle.write(extra)

    def test_every_source_kind_changes_the_hash(self):
        seen = {hams_lint_bin.source_hash(self.crate)}
        for rel, extra in (
            ("rules/checks.toml", "\n# a rule edit\n"),
            ("src/main.rs", "\n// a code edit\n"),
            ("Cargo.toml", "\n# a manifest edit\n"),
            ("Cargo.lock", "\n# a lock edit\n"),
        ):
            self._edit(rel, extra)
            digest = hams_lint_bin.source_hash(self.crate)
            self.assertNotIn(digest, seen, rel)
            seen.add(digest)
        with open(os.path.join(self.crate, "src", "new_module.rs"), "w", encoding="utf-8") as handle:
            handle.write("// new\n")
        self.assertNotIn(hams_lint_bin.source_hash(self.crate), seen)

    def test_build_output_does_not_change_the_hash(self):
        before = hams_lint_bin.source_hash(self.crate)
        os.makedirs(os.path.join(self.crate, "target", "release"))
        with open(os.path.join(self.crate, "target", "release", "hams_lint"), "w", encoding="utf-8") as handle:
            handle.write("binary")
        self.assertEqual(hams_lint_bin.source_hash(self.crate), before)

    def test_binary_path_is_in_the_cache_and_named_by_the_hash(self):
        environ = {"XDG_CACHE_HOME": self.crate}
        path = hams_lint_bin.binary_path(self.crate, environ)
        expected_dir = os.path.join(self.crate, "hams-lint", "engine-bin", hams_lint_bin.source_hash(self.crate)[:32])
        self.assertEqual(path, os.path.join(expected_dir, "hams_lint"))

    def test_a_failed_build_raises_with_cargo_output_and_main_exits_2(self):
        broken = tempfile.mkdtemp(prefix="hams_lint_broken_")
        self.addCleanup(shutil.rmtree, broken)
        os.makedirs(os.path.join(broken, "src"))
        environ = {"XDG_CACHE_HOME": broken, "HOME": broken, "PATH": os.environ.get("PATH", "")}
        with self.assertRaises(RuntimeError) as caught:
            hams_lint_bin.ensure_built(broken, environ)
        self.assertIn("cargo build", str(caught.exception))
        self.assertIn("Cargo.toml", str(caught.exception))
        self.assertFalse(os.path.exists(hams_lint_bin.binary_path(broken, environ)))


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = hams_lint_bin.ensure_built()

    def test_the_binary_is_the_one_for_the_current_sources(self):
        self.assertEqual(self.binary, hams_lint_bin.binary_path())
        self.assertTrue(os.access(self.binary, os.X_OK))

    def test_rust_unit_tests_pass(self):
        target_dir = lint_cache.cargo_target_dir("hams_lint")
        with lint_cache.locked(os.path.join(target_dir, "hams_lint_build")):
            done = subprocess.run(
                ["cargo", "test", "--release", "--quiet", "--target-dir", target_dir],
                cwd=hams_lint_bin.CRATE_DIR,
                capture_output=True,
                text=True,
            )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("test result: ok", done.stdout)

    def test_every_rule_file_example_reports_its_declared_lines(self):
        done = subprocess.run([self.binary, "selftest"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"selftest: [1-9][0-9]* examples, 0 failed")

    def test_a_bad_rule_file_is_exit_2_naming_the_check(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(
                "version = 1\n[[check]]\nname = 'broken_check'\nlegacy = 'x.py'\nroots = 'single'\nreport = 'list'\n"
                "usage = 'u'\nwhat = 'w'\nread = 'lines_strict'\nfiles = { suffixes = ['.py'] }\n"
                "[[check.rule]]\nid = 'r'\nregex = '('\nmessage = 'm'\n"
            )
        self.addCleanup(os.unlink, handle.name)
        done = subprocess.run([self.binary, "--rules", handle.name, "list"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("broken_check", done.stderr)

    def test_main_path_prints_the_binary(self):
        done = subprocess.run(
            ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hams_lint_bin.py"), "path"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), self.binary)


if __name__ == "__main__":
    unittest.main()
