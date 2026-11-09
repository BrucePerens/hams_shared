# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_engine_parity.py, the differential harness that holds each `hams_lint` check to its
Python checker. Includes negative controls: a rule file that diverges from its Python checker must be
reported as a difference, and injection must never write outside its work directory."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

import lint_engine_parity as parity

GIT_ID = ["-c", "user.email=parity@example.invalid", "-c", "user.name=parity"]
HOME_DIR = "/" + "home" + "/"  # spelled in two parts: this file is itself scanned by check_absolute_paths


def _make_repo(base):
    repo = os.path.join(base, "repo")
    files = {
        "run.sh": "#!/bin/sh\necho ok\n",
        "pkg/tool.py": "import os\n",
        "pkg/data.json": "{}\n",
        "static/tests/a.test.js": "const x = 1;\n",
        "src/lib.rs": "pub fn f() {}\n",
        "pkg/tests/test_net.py": "x = 1\n",
        "docs/notes.md": "plain\n",
        "Makefile": "all:\n\ttrue\n",
    }
    for rel, text in files.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", *GIT_ID, "-C", repo, "commit", "-qm", "init"], check=True)
    return repo


def _digest_tree(root):
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            path = os.path.join(base, name)
            digest.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as handle:
                digest.update(handle.read())
    return digest.hexdigest()


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="parity_test_")
        self.addCleanup(shutil.rmtree, self.base)
        self.output = []

    def _main(self, argv):
        return parity.main(argv)

    def test_the_rule_file_examples_agree_with_the_python_checkers(self):
        self.assertEqual(self._main(["examples"]), 0)

    def test_negative_control_a_diverging_rule_is_a_difference(self):
        with open(parity.DEFAULT_RULES, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('literal = "' + HOME_DIR + '"', text)
        diverged = os.path.join(self.base, "diverged.toml")
        with open(diverged, "w", encoding="utf-8") as handle:
            handle.write(text.replace('literal = "' + HOME_DIR + '"', 'literal = "/hone/"'))
        code = self._main(["examples", "--rules", diverged, "--check", "check_absolute_paths"])
        self.assertEqual(code, parity.EXIT_DIFF)

    def test_tree_mode_on_a_repository(self):
        repo = _make_repo(self.base)
        self.assertEqual(self._main(["tree", repo]), 0)

    def test_inject_agrees_reports_the_injections_and_leaves_the_original_alone(self):
        repo = _make_repo(self.base)
        before = _digest_tree(repo)
        work = os.path.join(self.base, "work")
        runner = parity.Runner(parity.hams_lint_bin.ensure_built(), parity.DEFAULT_RULES)
        checks = parity.load_checks(parity.DEFAULT_RULES)
        os.makedirs(work)
        self.assertTrue(parity.cmd_inject(runner, checks, [repo], work, 50, 1, out=self.output.append))
        self.assertEqual(_digest_tree(repo), before)
        self.assertEqual(os.listdir(work), [])
        same = [line for line in self.output if line.startswith("SAME ")]
        self.assertEqual(len(same), len(checks))
        for check in checks:
            reported = [line for line in self.output if line.startswith(f"[*] {check['name']} [injected")]
            self.assertTrue(reported, check["name"])
            self.assertRegex(reported[0], r"python reported [1-9][0-9]* of ")

    def test_inject_file_refuses_a_path_that_leaves_the_work_directory(self):
        outside = os.path.join(self.base, "outside.py")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("x = 1\n")
        work = os.path.join(self.base, "work")
        os.makedirs(work)
        link = os.path.join(work, "link.py")
        os.symlink(outside, link)
        self.assertFalse(parity.inject_file(link, ["y = 2"], work))
        with open(outside, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "x = 1\n")

    def test_inject_file_appends_on_a_new_line(self):
        work = os.path.join(self.base, "work")
        os.makedirs(work)
        path = os.path.join(work, "a.sh")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("echo no newline")
        self.assertTrue(parity.inject_file(path, ["#!/bin/sh injected"], work))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "echo no newline\n#!/bin/sh injected\n")

    def test_reported_lines_reads_both_output_forms(self):
        text = "❌ Shebang Violations:\n  - pkg/a.sh:3 Contains shebang\n❌ pkg/a.sh:7: `FakeSocket` looks like\n"
        self.assertEqual(parity.reported_lines(text, "pkg/a.sh"), [3, 7])

    def test_an_unknown_check_cannot_be_compared(self):
        self.assertEqual(self._main(["examples", "--check", "no_such_check"]), parity.EXIT_NOT_COMPARED)


if __name__ == "__main__":
    unittest.main()
