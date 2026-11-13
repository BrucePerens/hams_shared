#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_doc_references.py: each kind of broken reference is found, each kind of good one is not."""

import json
import os
import subprocess
import tempfile
import unittest

import check_doc_references as cdr  # noqa: E402
import test_check_target as shared  # noqa: E402

SCRIPT = """import argparse
p = argparse.ArgumentParser()
p.add_argument("target")
p.add_argument("--known", action="store_true")
p.add_argument("--long-name")
"""


class Tree:
    """A throwaway repository on disk: `files` maps a relative path to its text."""

    def __init__(self, test, files):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        self.root = os.path.realpath(self.dir.name)
        for rel, text in files.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        paths = [os.path.join(self.root, rel) for rel in files]
        self.know = cdr.Knowledge(paths, [self.root])

    def check(self, markdown, doc="docs/doc.md"):
        return [(kind, ref) for _, kind, ref in cdr.check_markdown(markdown, os.path.join(self.root, doc), self.know)]


BASE = {
    "tools/run.py": SCRIPT,
    "docs/adrs/0090_ratchet.md": "# ADR 0090\n",
    "docs/other.md": "# Other\n\n## Second part\n",
    "units/hams-x.service": "[Service]\n",
    "src/code.py": "HAMS_REAL_SWITCH = 1\ndef real_function():\n    pass\nCONFIG = 'hams_thing.some_key'\n",
    "systemd/deploy.sh": "systemctl enable hams-y.timer\n",
}


class PathTests(unittest.TestCase):
    def setUp(self):
        self.tree = Tree(self, BASE)

    def test_an_existing_path_in_backticks_is_fine_from_root_document_or_tail(self):
        for ref in ("tools/run.py", "docs/other.md", "other.md", "run.py", "units/", "systemd/deploy.sh", "tools/run.py:12"):
            self.assertEqual(self.tree.check(f"See `{ref}` here.\n"), [], ref)

    def test_a_missing_path_in_backticks_is_found(self):
        self.assertEqual(self.tree.check("See `tools/gone.py`.\n"), [("path", "tools/gone.py")])
        self.assertEqual(self.tree.check("See `gone.py`.\n"), [("path", "gone.py")])

    def test_placeholders_urls_globs_and_site_paths_are_not_paths(self):
        text = "`/etc/x.conf` `~/a.py` `https://x.y/z.py` `tools/*.py` `<dir>/a.py` `x.py` `--flag.py` `notes.txt`\n"
        self.assertEqual(self.tree.check(text), [])

    def test_a_link_is_resolved_against_the_document(self):
        self.assertEqual(self.tree.check("[o](other.md) [o](../tools/run.py) [x](https://x.y) [s](/site/page)\n"), [])
        self.assertEqual(self.tree.check("[o](missing.md)\n"), [("path", "missing.md")])

    def test_a_site_style_link_without_extension_finds_the_page(self):
        self.assertEqual(self.tree.check("[o](other)\n"), [])

    def test_paths_inside_code_fences_are_not_checked(self):
        self.assertEqual(self.tree.check("```\ncat tools/gone.py\n```\n"), [])


class FlagTests(unittest.TestCase):
    def setUp(self):
        self.tree = Tree(self, BASE)

    def test_a_defined_flag_is_fine_in_backticks_and_in_a_fence(self):
        self.assertEqual(self.tree.check("Run `tools/run.py --known x --long-name=3`.\n"), [])
        self.assertEqual(self.tree.check("```\npython3 tools/run.py --known\n```\n"), [])

    def test_an_undefined_flag_is_found_in_backticks_and_in_a_fence(self):
        self.assertEqual(self.tree.check("Run `run.py --gone`.\n"), [("flag", "run.py --gone")])
        self.assertEqual(self.tree.check("```\npython3 tools/run.py --known --gone\n```\n"), [("flag", "tools/run.py --gone")])

    def test_flags_of_a_following_command_after_double_dash_or_pipe_are_not_the_scripts(self):
        self.assertEqual(self.tree.check("`run.py -- cargo --gone`\n"), [])
        self.assertEqual(self.tree.check("`run.py --known | grep --gone`\n"), [])

    def test_a_script_without_argparse_or_with_dynamic_flags_is_skipped(self):
        tree = Tree(self, {"a.py": "import sys\n", "b.py": "p.add_argument(NAME)\n"})
        self.assertEqual(tree.check("`a.py --x` `b.py --y`\n"), [])

    def test_a_script_named_as_an_argument_of_another_command_is_not_run(self):
        self.assertEqual(self.tree.check("`tool wait --cmd-contains run.py --gone`\n"), [])
        self.assertEqual(self.tree.check("`python3 tools/run.py --gone`\n"), [("flag", "tools/run.py --gone")])

    def test_a_no_form_and_an_abbreviation_are_accepted(self):
        self.assertEqual(self.tree.check("`run.py --no-known --long`\n"), [])


class LayoutTests(unittest.TestCase):
    def test_a_hams_shared_document_links_into_hams_open_as_in_the_canonical_layout(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = os.path.realpath(tmp.name)
        shared, opened = os.path.join(base, "wt", "hams_shared"), os.path.join(base, "hams_open")
        files = {os.path.join(shared, "docs", "modules", "m.md"): "", os.path.join(opened, "mod", "docs", "s.md"): ""}
        for path in files:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w", encoding="utf-8").close()
        know = cdr.Knowledge(list(files), [shared, opened])
        doc = os.path.join(shared, "docs", "modules", "m.md")
        self.assertEqual(cdr.check_markdown("[s](../../../mod/docs/s.md) [g](../../../mod/docs/gone.md)\n", doc, know),
                         [(1, "path", "../../../mod/docs/gone.md")])


class AdrAnchorUnitConfigFunctionTests(unittest.TestCase):
    def setUp(self):
        self.tree = Tree(self, BASE)

    def test_adr_numbers(self):
        self.assertEqual(self.tree.check("See ADR 0090 and ADR-90.\n"), [])
        self.assertEqual(self.tree.check("See ADR 0555.\n"), [("adr", "ADR 0555")])

    def test_heading_links_in_this_and_other_documents(self):
        self.assertEqual(self.tree.check("# Top\n[a](#top) [b](other.md#second-part)\n"), [])
        self.assertEqual(self.tree.check("# Top\n[a](#nope)\n"), [("anchor", "#nope")])
        self.assertEqual(self.tree.check("[b](other.md#nope)\n"), [("anchor", "other.md#nope")])

    def test_unit_names_must_exist_as_a_file_or_in_a_script(self):
        self.assertEqual(self.tree.check("`hams-x.service` `hams-y.timer` `hams-z@3.service`\n"), [("unit", "hams-z@3.service")])

    def test_template_unit_instances_match_the_template(self):
        tree = Tree(self, {"u/hams-radiod@.service": ""})
        self.assertEqual(tree.check("`hams-radiod@2.service`\n"), [])

    def test_env_vars_and_config_parameters(self):
        self.assertEqual(self.tree.check("`HAMS_REAL_SWITCH` `HAMS_GONE_SWITCH`\n"), [("config", "HAMS_GONE_SWITCH")])
        self.assertEqual(self.tree.check("The config parameter `hams_thing.some_key`.\n"), [])
        self.assertEqual(self.tree.check("The config parameter `hams_thing.gone_key`.\n"), [("config", "hams_thing.gone_key")])
        self.assertEqual(self.tree.check("Not a parameter line: `hams_thing.gone_key`.\n"), [])

    def test_function_names(self):
        self.assertEqual(self.tree.check("`real_function()` `Cls.real_function(a, b)` `len()` `os.path.join()`\n"), [])
        self.assertEqual(self.tree.check("`gone_function()`\n"), [("function", "gone_function()")])
        self.assertEqual(self.tree.check("`mod::gone_function(x)`\n"), [("function", "mod::gone_function()")])


class ScanAndBaselineTests(unittest.TestCase):
    def test_excluded_documents_are_not_scanned(self):
        for rel in ("night_shift_history/a.md", "docs/proposals/a.md", "x/claims/a.md", "d/vendor/a.md"):
            self.assertTrue(cdr.is_excluded(rel), rel)
        self.assertFalse(cdr.is_excluded("docs/runbooks/a.md"))

    def test_baseline_ratchet_fails_only_on_a_new_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = os.path.join(tmp, "b.json")
            finding = ("docs/a.md", 3, "path", "gone.py")
            cdr.save_baseline(baseline, {cdr.key_of(finding)})
            with open(baseline, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), ["docs/a.md|path|gone.py"])
            known = cdr.load_baseline(baseline)
            self.assertIn(cdr.key_of(("docs/a.md", 99, "path", "gone.py")), known)
            self.assertNotIn(cdr.key_of(("docs/a.md", 3, "path", "other.py")), known)
            self.assertEqual(cdr.load_baseline(os.path.join(tmp, "missing.json")), set())


class MainTests(unittest.TestCase):
    def test_a_missing_file_or_empty_target_is_refused_not_passed(self):
        shared.assert_checker_refuses_a_bad_target(self, "check_doc_references.py")

    def test_main_in_a_git_repository_generates_a_baseline_then_fails_on_new_breakage_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(os.path.realpath(tmp), "hams_com")
            os.makedirs(os.path.join(repo, "docs"))
            doc = os.path.join(repo, "docs", "a.md")
            with open(doc, "w", encoding="utf-8") as handle:
                handle.write("See `gone.py`.\n")
            subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
            subprocess.run(["git", "-C", repo, "add", "."], check=True)
            baseline = os.path.join(tmp, "baseline.json")
            args = [repo, "--baseline", baseline]
            self.assertEqual(cdr.main(args), 1)
            self.assertEqual(cdr.main(args + ["--generate-baseline"]), 0)
            self.assertEqual(cdr.main(args), 0)
            with open(doc, "a", encoding="utf-8") as handle:
                handle.write("Also `gone_too.py`.\n")
            self.assertEqual(cdr.main(args), 1)


if __name__ == "__main__":
    unittest.main()
