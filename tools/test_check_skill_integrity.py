#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_skill_integrity.py.

check_skill_file() reads real git HEAD state (git show HEAD:<path>) to
detect a removed mandatory section, so the "section removed" tests need a
real temp git repository, not bare files.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_skill_integrity as chk  # noqa: E402


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _commit_file(repo, relpath, content, message="add"):
    _write(os.path.join(repo, relpath), content)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)


_GOOD_SKILL = (
    "---\n"
    "name: my-skill\n"
    "description: does a thing\n"
    "---\n"
    "## Workflow\nStep one.\n\n"
    "## Common Pitfalls & Strict Rules\nDon't do that.\n"
)


class CheckSkillFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _init_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_well_formed_unchanged_skill_file_passes(self):
        _commit_file(self.tmp, "SKILL.md", _GOOD_SKILL)
        p = os.path.join(self.tmp, "SKILL.md")
        self.assertEqual(chk.check_skill_file(p, self.tmp), [])

    def test_missing_frontmatter_entirely_is_flagged(self):
        p = os.path.join(self.tmp, "SKILL.md")
        _write(p, "# Just a heading\nno frontmatter here\n")
        errors = chk.check_skill_file(p, self.tmp)
        self.assertTrue(any("Missing or invalid YAML frontmatter" in e for e in errors))

    def test_unclosed_frontmatter_is_flagged(self):
        p = os.path.join(self.tmp, "SKILL.md")
        _write(p, "---\nname: x\ndescription: y\nno closing marker\n")
        errors = chk.check_skill_file(p, self.tmp)
        self.assertTrue(any("Unclosed YAML frontmatter" in e for e in errors))

    def test_missing_name_field_is_flagged(self):
        p = os.path.join(self.tmp, "SKILL.md")
        _write(p, "---\ndescription: y\n---\nbody\n")
        errors = chk.check_skill_file(p, self.tmp)
        self.assertTrue(any("Missing 'name:'" in e for e in errors))

    def test_missing_description_field_is_flagged(self):
        p = os.path.join(self.tmp, "SKILL.md")
        _write(p, "---\nname: x\n---\nbody\n")
        errors = chk.check_skill_file(p, self.tmp)
        self.assertTrue(any("Missing 'description:'" in e for e in errors))

    def test_ignore_structure_true_bypasses_all_checks_including_earlier_errors(self):
        # Real, verified, non-obvious behavior: `return []` discards even
        # a genuinely-missing name/description error accumulated before
        # the ignore_structure check runs, not just the later git-based
        # section checks. Confirmed via a standalone script before writing
        # this assertion, not assumed from reading the code.
        p = os.path.join(self.tmp, "SKILL.md")
        _write(p, "---\ndescription: y\nignore_structure: true\n---\nbody\n")
        self.assertEqual(chk.check_skill_file(p, self.tmp), [])

    def test_removing_the_workflow_section_present_in_head_is_flagged(self):
        _commit_file(self.tmp, "SKILL.md", _GOOD_SKILL)
        p = os.path.join(self.tmp, "SKILL.md")
        _write(p, _GOOD_SKILL.replace("## Workflow\nStep one.\n\n", ""))
        errors = chk.check_skill_file(p, self.tmp)
        self.assertTrue(any("## Workflow" in e for e in errors))

    def test_removing_the_pitfalls_section_present_in_head_is_flagged(self):
        _commit_file(self.tmp, "SKILL.md", _GOOD_SKILL)
        p = os.path.join(self.tmp, "SKILL.md")
        _write(
            p,
            _GOOD_SKILL.replace(
                "## Common Pitfalls & Strict Rules\nDon't do that.\n", ""
            ),
        )
        errors = chk.check_skill_file(p, self.tmp)
        self.assertTrue(any("Common Pitfalls" in e for e in errors))

    def test_a_brand_new_file_not_yet_in_head_has_no_removed_section_check(self):
        # git show HEAD:<path> fails for a file that was never committed,
        # caught and treated as empty HEAD content -- so there is nothing
        # to compare against, and the "removed section" checks can never
        # fire for a genuinely new file.
        p = os.path.join(self.tmp, "SKILL.md")
        _write(p, "---\nname: x\ndescription: y\n---\nbody with no sections\n")
        self.assertEqual(chk.check_skill_file(p, self.tmp), [])

    def test_a_file_that_keeps_both_mandatory_sections_after_editing_other_content_passes(self):
        _commit_file(self.tmp, "SKILL.md", _GOOD_SKILL)
        p = os.path.join(self.tmp, "SKILL.md")
        _write(p, _GOOD_SKILL + "\n## Extra Notes\nSomething new.\n")
        self.assertEqual(chk.check_skill_file(p, self.tmp), [])


class MainIntegrationTests(unittest.TestCase):
    """main() hardcodes its own skills_dir relative to this script's real
    location (workspace_root/agents/skills), so it isn't fixture-driven the
    way the other checkers' main()s are -- exercised against the real,
    live agents/skills/ tree instead."""

    def test_main_runs_against_the_real_repo_tree_without_crashing(self):
        # main() hardcodes its own skills_dir relative to __file__, so it
        # can't be pointed at a synthetic fixture -- this only confirms the
        # real path-resolution/glob wiring works and the process doesn't
        # crash, not a specific pass/fail outcome. Deliberately NOT
        # asserting returncode == 0: after the cwd fix above, the
        # '## Workflow'/'## Common Pitfalls' section-removal check is
        # newly live against real git HEAD state, and an uncommitted
        # working-tree edit to a real SKILL.md (including from the
        # concurrent Gemini ingestion pipeline) could make this fail for
        # a real, correct reason unrelated to this test. The frontmatter
        # and section-removal logic itself is already covered hermetically
        # by the ten tests above.
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_skill_integrity.py")
        result = subprocess.run(
            [sys.executable, script], capture_output=True, text=True, timeout=30
        )
        self.assertIn(result.returncode, (0, 1))


if __name__ == "__main__":
    unittest.main()
