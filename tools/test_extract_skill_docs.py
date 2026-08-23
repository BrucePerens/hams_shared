#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for extract_skill_docs.py.

Like extract_burn_docs.py, main() is __main__-guarded (safe to import),
but hardcodes both source_file (tools/check_burn_list.py) and
target_file (../agents/skills/linter-compliance/SKILL.md) relative to
its own __file__, with no CLI args. MainScriptTests copies the script
into a fixture's own tools/ subdirectory alongside a fixture
check_burn_list.py and a fixture agents/skills/linter-compliance/
directory, and runs it from there, so the real write lands in the
fixture, never the real repo tree.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extract_skill_docs.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class MainScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tools_dir = os.path.join(self.tmp, "tools")
        os.makedirs(self.tools_dir)
        os.makedirs(os.path.join(self.tmp, "agents", "skills", "linter-compliance"))
        shutil.copy(_SCRIPT, os.path.join(self.tools_dir, "extract_skill_docs.py"))
        self.output_path = os.path.join(
            self.tmp, "agents", "skills", "linter-compliance", "SKILL.md"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_source(self, content):
        _write(os.path.join(self.tools_dir, "check_burn_list.py"), content)

    def _run(self):
        result = subprocess.run(
            [sys.executable, os.path.join(self.tools_dir, "extract_skill_docs.py")],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_single_bang_prefixed_block_is_extracted_and_written(self):
        self._write_source(
            '"""!markdown\n# Heading\nSome content.\n"""\n'
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("Successfully extracted 1 literate documentation blocks", out)
        with open(self.output_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("# Heading", content)
        self.assertIn("Some content.", content)

    def test_two_blocks_are_joined_with_a_horizontal_rule_delimiter_between_them(self):
        self._write_source(
            '"""!markdown\nFirst block.\n"""\n'
            'x = 1\n'
            '"""!markdown\nSecond block.\n"""\n'
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        with open(self.output_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("First block.\n\n---\n\nmarkdown\nSecond block.\n", content)

    def test_the_trailing_delimiter_is_stripped_from_the_last_block(self):
        self._write_source('"""!markdown\nOnly block.\n"""\n')
        self._run()
        with open(self.output_path, encoding="utf-8") as f:
            content = f.read()
        self.assertFalse(content.endswith("---\n\n"))
        self.assertTrue(content.endswith("\n"))

    def test_blocks_are_ordered_by_their_position_in_the_source_not_discovery_order(self):
        # ast.walk() is breadth-first, not strictly source order -- the
        # script re-sorts by node.lineno afterward specifically to
        # guarantee source order. Verified via a case where the two
        # docstrings sit at different nesting depths.
        self._write_source(
            'class Foo:\n'
            '    """!markdown\nInside a class, comes first in source.\n"""\n'
            '\n'
            '"""!markdown\nModule level, comes second in source.\n"""\n'
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        with open(self.output_path, encoding="utf-8") as f:
            content = f.read()
        self.assertLess(
            content.index("comes first in source"),
            content.index("comes second in source"),
        )

    def test_a_plain_string_without_a_leading_bang_is_not_extracted(self):
        self._write_source('"""just a regular docstring, no bang"""\n')
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("No markdown blocks found.", out)
        self.assertFalse(os.path.exists(self.output_path))

    def test_no_matching_blocks_at_all_exits_one_and_writes_nothing(self):
        self._write_source("x = 1\ny = 2\n")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("No markdown blocks found.", out)
        self.assertFalse(os.path.exists(self.output_path))

    def test_a_syntax_error_in_the_source_file_exits_one_with_a_clear_message(self):
        self._write_source("def f(:\n")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("Failed to parse", out)

    def test_real_verified_behavior_the_literal_markdown_prefix_line_is_never_actually_stripped(self):
        # Real, verified discrepancy between this script's own comment and
        # its actual code: the comment above the dedent call claims to
        # 'Remove the "markdown\n" prefix', but the code only strips the
        # single leading "!" character (text[1:]) -- the literal word
        # "markdown" on its own line is left in the extracted output.
        # Confirmed empirically before writing this assertion, not
        # assumed from the comment's stated intent. Documented here
        # rather than fixed: a repo-wide grep confirms check_burn_list.py
        # currently has zero "!"-prefixed blocks at all, so running this
        # script against the real repo right now finds nothing and exits
        # 1 -- this is currently-inert tooling, not a live path.
        self._write_source('"""!markdown\n# Heading\n"""\n')
        self._run()
        with open(self.output_path, encoding="utf-8") as f:
            content = f.read()
        self.assertTrue(content.startswith("markdown\n"))

    def test_real_verified_behavior_dedent_is_neutered_by_the_undedented_markdown_header_line(self):
        # A direct consequence of the finding above: because the
        # unindented "markdown" header line is always the first line
        # fed to textwrap.dedent(), its common-leading-whitespace
        # calculation is always pinned to zero, so any indentation on
        # the block's actual content lines is never normalized despite
        # the dedent() call.
        self._write_source(
            '"""!markdown\n'
            '    indented content line, dedent has no visible effect on it\n'
            '"""\n'
        )
        self._run()
        with open(self.output_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("    indented content line", content)


if __name__ == "__main__":
    unittest.main()
