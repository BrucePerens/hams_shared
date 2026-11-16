"""lint_autofix.py: F401/F541 fixes, crate and changed-file discovery, scoped listing and the explicit claim confirmation."""
import os
import subprocess
import tempfile
import unittest

import lint_autofix


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@localhost", *args], check=True, capture_output=True)


def make_repo():
    tmp = tempfile.TemporaryDirectory()
    git(tmp.name, "init", "-q", "-b", "main")
    with open(os.path.join(tmp.name, "seed.txt"), "w") as handle:
        handle.write("seed\n")
    git(tmp.name, "add", ".")
    git(tmp.name, "commit", "-q", "-m", "seed")
    git(tmp.name, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp


class LintAutofixTests(unittest.TestCase):
    # [@ANCHOR: test_unused_imports_are_removed_whole_or_by_name]
    def test_unused_imports_are_removed_whole_or_by_name(self):
        source = "import os\nimport sys, json\nfrom a.b import c, d as e\nfrom . import f\nprint(sys, c)\n"
        fixed = lint_autofix.remove_unused_imports(source, {1: {"os"}, 2: {"json"}, 3: {"a.b.d as e"}, 4: {".f"}})
        self.assertEqual(fixed, "import sys\nfrom a.b import c\nprint(sys, c)\n")

    # [@ANCHOR: test_f_prefix_is_dropped_only_where_flake8_says_so]
    def test_f_prefix_is_dropped_only_where_flake8_says_so(self):
        source = 'a = f"plain"\nb = f"{a}"\nc = rf"x"\n'
        self.assertEqual(lint_autofix.strip_f_prefix(source, [(1, 4), (3, 4)]), 'a = "plain"\nb = f"{a}"\nc = r"x"\n')

    # [@ANCHOR: test_python_files_are_fixed_and_unparsable_results_are_reverted]
    def test_python_files_are_fixed_and_unparsable_results_are_reverted(self):
        with tempfile.TemporaryDirectory() as tree:
            good = os.path.join(tree, "good.py")
            with open(good, "w") as handle:
                handle.write('import os\nimport sys\nprint(sys, f"hello")\n')
            bad = os.path.join(tree, "bad.py")
            with open(bad, "w") as handle:
                handle.write("try:\n    import os\nexcept ImportError:\n    pass\n")
            with open(os.path.join(tree, "__init__.py"), "w") as handle:
                handle.write("import os\n")
            messages = []
            changed = lint_autofix.fix_python(tree, ["good.py", "bad.py", "__init__.py"], say=messages.append)
            with open(good) as handle:
                self.assertEqual(handle.read(), 'import sys\nprint(sys, "hello")\n')
            with open(bad) as handle:
                self.assertIn("import os", handle.read())
            self.assertEqual(changed, ["good.py"])
            self.assertTrue(any("bad.py" in m for m in messages))
            with open(os.path.join(tree, "__init__.py")) as handle:
                self.assertEqual(handle.read(), "import os\n")

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tree:
            path = os.path.join(tree, "a.py")
            with open(path, "w") as handle:
                handle.write("import os\n")
            self.assertEqual(lint_autofix.fix_python(tree, ["a.py"], dry_run=True, say=lambda m: None), ["a.py"])
            with open(path) as handle:
                self.assertEqual(handle.read(), "import os\n")

    # [@ANCHOR: test_changed_files_lists_committed_and_uncommitted_work]
    def test_changed_files_lists_committed_and_uncommitted_work(self):
        with make_repo() as tree:
            with open(os.path.join(tree, "committed.py"), "w") as handle:
                handle.write("x = 1\n")
            git(tree, "add", ".")
            git(tree, "commit", "-q", "-m", "c")
            with open(os.path.join(tree, "untracked.py"), "w") as handle:
                handle.write("y = 1\n")
            with open(os.path.join(tree, "seed.txt"), "w") as handle:
                handle.write("edited\n")
            self.assertEqual(lint_autofix.changed_files(tree), ["committed.py", "seed.txt", "untracked.py"])

    # [@ANCHOR: test_touched_crates_are_the_nearest_cargo_toml_directories]
    def test_touched_crates_are_the_nearest_cargo_toml_directories(self):
        with tempfile.TemporaryDirectory() as tree:
            os.makedirs(os.path.join(tree, "daemons", "a", "src"))
            with open(os.path.join(tree, "daemons", "a", "Cargo.toml"), "w") as handle:
                handle.write("[package]\n")
            self.assertEqual(lint_autofix.touched_crates(tree, ["daemons/a/src/main.rs", "daemons/a/src/x.rs", "doc.md"]), ["daemons/a"])

    # [@ANCHOR: test_needles_hold_changed_paths_and_their_anchors]
    def test_needles_hold_changed_paths_and_their_anchors(self):
        with tempfile.TemporaryDirectory() as tree:
            with open(os.path.join(tree, "m.py"), "w") as handle:
                handle.write("# [@ANCHOR: my_anchor]\ndef f(): pass\n")
            self.assertEqual(lint_autofix.needles_for(tree, ["m.py", "gone.py"]), {"m.py", "gone.py", "my_anchor"})

    # [@ANCHOR: test_scoped_lines_keep_only_lines_naming_a_needle]
    def test_scoped_lines_keep_only_lines_naming_a_needle(self):
        self.assertEqual(lint_autofix.scoped_lines("a mine.py x\nother.py y\n", {"mine.py"}), ["a mine.py x"])

    # [@ANCHOR: test_confirm_claim_rewrites_only_the_hash_lines]
    def test_confirm_claim_rewrites_only_the_hash_lines(self):
        with make_repo() as tree:
            os.makedirs(os.path.join(tree, "mod", "claims"))
            with open(os.path.join(tree, "mod", "m.py"), "w") as handle:
                handle.write("# [@ANCHOR: mod:thing]\ndef thing():\n    return 2\n")
            claim = os.path.join(tree, "mod", "claims", "thing.md")
            with open(claim, "w") as handle:
                handle.write("---\nanchor: mod:thing\ncode_hash: sha256:" + "0" * 64 + "\nreview_tier: 1\n---\n\nThe function SHALL return 2.\n")
            git(tree, "add", ".")
            self.assertTrue(lint_autofix.confirm_claim(tree, "mod/claims/thing.md", say=lambda m: None))
            with open(claim) as handle:
                text = handle.read()
            self.assertNotIn("0" * 64, text)
            self.assertIn("hash_algorithm: v2\nreview_tier: 1", text)
            self.assertTrue(text.endswith("The function SHALL return 2.\n"))
            self.assertFalse(lint_autofix.confirm_claim(tree, "mod/claims/thing.md", say=lambda m: None))


if __name__ == "__main__":
    unittest.main()
