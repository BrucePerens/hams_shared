# SPDX-License-Identifier: AGPL-3.0-or-later
"""The lint steps that run on the Rust engine (run_linters.ENGINE_CHECKS): each is defined in the engine's
rule file, keeps its Python checker as the parity reference (no rule lost), keeps its step name (pass stamps,
the lint cache and STEP_META see no change) and gets the checker environment; and a change to the engine's
Rust sources or rule file invalidates cached lint passes."""

import os
import shutil
import tempfile
import tomllib
import unittest

import check_target
import lint_exec
import lint_engine_parity
import run_linters

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


class EngineCheckListTests(unittest.TestCase):
    def setUp(self):
        with open(lint_engine_parity.DEFAULT_RULES, "rb") as handle:
            self.rules = {c["name"]: c for c in tomllib.load(handle)["check"]}

    def test_every_engine_check_is_defined_and_keeps_its_python_reference(self):
        self.assertTrue(run_linters.ENGINE_CHECKS)
        for name in run_linters.ENGINE_CHECKS:
            self.assertIn(name, self.rules, f"{name} runs on the engine but the rule file does not define it")
            self.assertEqual(self.rules[name]["legacy"], name + ".py")
            self.assertTrue(os.path.exists(os.path.join(TOOLS_DIR, name + ".py")), f"{name}.py (parity reference) is gone")
            self.assertTrue(self.rules[name].get("example"), f"{name} has no examples in the rule file")
            self.assertTrue(self.rules[name].get("inject"), f"{name} has no inject lines in the rule file")

    def test_engine_checks_run_through_the_launcher_with_the_same_step_name(self):
        argv = run_linters.checker_argv("/usr/bin/python3", "/repo", "check_shebang.py", ["/repo"])
        self.assertEqual(
            argv, ["/usr/bin/python3", os.path.join("/repo", "tools", "hams_lint_bin.py"), "run", "check", "check_shebang", "/repo"]
        )
        steps = []
        run_linters._per_target_steps(lambda name, argv, **o: steps.append((name, argv)), "/usr/bin/python3", "/repo",
                                      "check_absolute_paths.py", ["/repo"])
        self.assertEqual(steps[0][0], "check_absolute_paths")
        self.assertIn("hams_lint_bin.py", steps[0][1][1])

    def test_other_checkers_keep_their_python_argv(self):
        argv = run_linters.checker_argv("/usr/bin/python3", "/repo", "check_rabbitmq_pool.py", ["/a", "/b"])
        self.assertEqual(argv, ["/usr/bin/python3", os.path.join("/repo", "tools", "check_rabbitmq_pool.py"), "/a", "/b"])

    def test_an_engine_step_gets_the_checker_environment(self):
        argv = run_linters.checker_argv("python3", "/repo", "check_fake_network_listener.py", ["/a", "/b"])
        env = run_linters.checker_env(argv)
        self.assertEqual(env[check_target.ALLOW_EMPTY_ENV], "1")
        self.assertIsNone(run_linters.checker_env(["python3", "/repo/tools/hams_lint_bin.py", "path"]))


class ToolFingerprintTests(unittest.TestCase):
    def test_a_rule_file_or_rust_source_change_invalidates_cached_passes(self):
        tools = tempfile.mkdtemp(prefix="lint_tools_")
        self.addCleanup(shutil.rmtree, tools)
        os.makedirs(os.path.join(tools, "hams_lint", "src"))
        os.makedirs(os.path.join(tools, "hams_lint", "rules"))
        for rel, text in (("hams_lint/src/main.rs", "fn main() {}\n"), ("hams_lint/rules/checks.toml", "version = 1\n"),
                          ("hams_lint/Cargo.lock", "# lock\n"), ("check_x.py", "x = 1\n")):
            with open(os.path.join(tools, rel), "w", encoding="utf-8") as handle:
                handle.write(text)
        step = lint_exec.Step(name="check_x", argv=["python3", "check_x.py"], roots=[], suffixes=None)
        keys = set()
        for rel in ("hams_lint/rules/checks.toml", "hams_lint/src/main.rs", "hams_lint/Cargo.lock"):
            keys.add(lint_exec.InputFingerprints(tools).step_key(step))
            with open(os.path.join(tools, rel), "a", encoding="utf-8") as handle:
                handle.write("# changed, and longer\n")
        keys.add(lint_exec.InputFingerprints(tools).step_key(step))
        self.assertEqual(len(keys), 4)


if __name__ == "__main__":
    unittest.main()
