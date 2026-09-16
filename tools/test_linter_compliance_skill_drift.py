#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fails when check_burn_list.py accepts a bypass tag the linter-compliance skill never mentions.

AGENTS.md names agents/skills/linter-compliance/SKILL.md as the reference agents must follow. It
used to be generated from literate docstrings in check_burn_list.py by extract_skill_docs.py; those
docstrings were removed, the generator had nothing left to read, and SKILL.md silently fell 35
tags behind the linter. Bruce chose (2026-09-15) to maintain SKILL.md by hand with this check
guarding it, rather than restore the generator.

Tags are read from check_burn_list.py's string constants, not its comments: a constant is what the
linter actually matches or tells the user to write, while a comment can mention a tag in passing.
"""
import ast
import os
import re
import unittest

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
LINTER_PATH = os.path.join(TOOLS_DIR, "check_burn_list.py")
SKILL_PATH = os.path.join(TOOLS_DIR, "..", "agents", "skills", "linter-compliance", "SKILL.md")

# A tag ends in a letter or digit, so prose such as "burn-ignore-" followed by a placeholder is not
# read as a tag named "burn-ignore-".
TAG_REGEX = re.compile(r"(?:burn|audit)-ignore(?:-[a-z0-9]+)*")


def extract_linter_tags(source):
    """Return {tag: first line number} for every bypass tag in the source's string constants."""
    tags = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for tag in TAG_REGEX.findall(node.value):
                tags.setdefault(tag, node.lineno)
    return tags


def undocumented_tags(linter_source, skill_text):
    """Tags the linter accepts that the skill text never names, sorted."""
    documented = set(TAG_REGEX.findall(skill_text))
    return sorted(tag for tag in extract_linter_tags(linter_source) if tag not in documented)


class TestLinterComplianceSkillDrift(unittest.TestCase):
    def test_every_linter_bypass_tag_is_documented_in_the_skill(self):
        with open(LINTER_PATH, encoding="utf-8") as linter_file:
            linter_source = linter_file.read()
        with open(SKILL_PATH, encoding="utf-8") as skill_file:
            skill_text = skill_file.read()
        missing = undocumented_tags(linter_source, skill_text)
        self.assertEqual(
            missing,
            [],
            "check_burn_list.py accepts these bypass tags but "
            "agents/skills/linter-compliance/SKILL.md never mentions them. Add each one to "
            "SKILL.md's section 6.5 with what it suppresses and when it is legitimate: "
            + ", ".join(missing),
        )

    def test_extraction_reads_real_linter_tags(self):
        """Guards the check against passing vacuously: if the regex or the AST walk broke, an
        empty tag set would make the test above pass while checking nothing."""
        with open(LINTER_PATH, encoding="utf-8") as linter_file:
            tags = extract_linter_tags(linter_file.read())
        for known in ("burn-ignore-sudo", "audit-ignore-outbound-fetch", "burn-ignore-os-account-probe"):
            self.assertIn(known, tags)
        self.assertGreater(len(tags), 30)

    # Fixture tag names are assembled with "-".join() rather than written out: check_burn_list.py
    # matches raw line text, so a literal made-up tag in this file would itself be reported as an
    # UNAUTHORIZED BYPASS.
    def test_a_tag_only_mentioned_in_a_comment_is_not_required(self):
        prose_tag = "-".join(("burn", "ignore", "only", "in", "prose"))
        real_tag = "-".join(("burn", "ignore", "real", "tag"))
        source = f'# {prose_tag}\nALLOWED = ["{real_tag}"]\n'
        self.assertEqual(sorted(extract_linter_tags(source)), [real_tag])

    def test_a_new_undocumented_tag_is_reported_and_a_documented_one_is_not(self):
        documented_tag = "-".join(("burn", "ignore", "documented"))
        new_tag = "-".join(("audit", "ignore", "brand", "new"))
        source = f'ALLOWED = ["{documented_tag}", "{new_tag}"]\n'
        skill = f"Use `# {documented_tag}` when ...\n"
        self.assertEqual(undocumented_tags(source, skill), [new_tag])

    def test_a_longer_documented_tag_does_not_cover_its_prefix(self):
        """`burn-ignore-sudo-extra` in the skill must not count as documenting `burn-ignore-sudo`,
        which a plain substring search would accept."""
        source = 'ALLOWED = ["burn-ignore-sudo"]\n'
        self.assertEqual(undocumented_tags(source, "burn-ignore-sudo-extra"), ["burn-ignore-sudo"])


if __name__ == "__main__":
    unittest.main()
