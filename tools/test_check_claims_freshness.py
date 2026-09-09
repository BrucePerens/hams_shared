#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_claims_freshness.py.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_claims_freshness as ccf  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _init_git_repo(tmp):
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp, check=True)


class CheckClaimsFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _function_source(self):
        return (
            "def foo(x):\n"
            "    # [@ANCHOR: my_module:foo]\n"
            "    return x + 1\n"
        )

    def _hash_of(self, span):
        return hashlib.sha256(span.encode("utf-8")).hexdigest()

    def test_a_claim_with_a_matching_hash_is_fresh(self):
        _write(os.path.join(self.tmp, "mod.py"), self._function_source())
        span = "# [@ANCHOR: my_module:foo]\ndef foo(x):\n    # [@ANCHOR: my_module:foo]\n    return x + 1"
        # Recompute the real span the same way the tool does, rather than hand-deriving it,
        # so this test doesn't silently depend on guessing _function_span's own exact behavior.
        real_hash = ccf.compute_function_hash(os.path.join(self.tmp, "mod.py"))["my_module:foo"]
        _write(
            os.path.join(self.tmp, "claims", "foo.md"),
            f"---\nanchor: my_module:foo\ncode_hash: sha256:{real_hash}\n---\n\n1. Returns x + 1.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(problems, [])

    def test_a_claim_with_a_stale_hash_is_flagged(self):
        _write(os.path.join(self.tmp, "mod.py"), self._function_source())
        _write(
            os.path.join(self.tmp, "claims", "foo.md"),
            "---\nanchor: my_module:foo\ncode_hash: sha256:0000000000000000000000000000000000000000000000000000000000000000\n---\n\n1. Returns x + 1.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(len(problems), 1)
        self.assertIn("code_hash mismatch", problems[0][1])

    def test_a_claim_whose_anchor_no_longer_exists_is_flagged_as_orphaned(self):
        _write(os.path.join(self.tmp, "mod.py"), "def bar():\n    pass\n")
        _write(
            os.path.join(self.tmp, "claims", "foo.md"),
            "---\nanchor: my_module:foo\ncode_hash: sha256:abc123\n---\n\n1. Does something.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(len(problems), 1)
        self.assertIn("orphaned claim", problems[0][1])

    def test_a_claim_missing_required_frontmatter_fields_is_flagged(self):
        _write(os.path.join(self.tmp, "mod.py"), self._function_source())
        _write(
            os.path.join(self.tmp, "claims", "foo.md"),
            "---\nanchor: my_module:foo\n---\n\n1. Returns x + 1.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(len(problems), 1)
        self.assertIn("missing required frontmatter", problems[0][1])

    def test_a_markdown_file_outside_a_claims_directory_is_ignored(self):
        _write(os.path.join(self.tmp, "docs", "foo.md"), "---\nanchor: x\ncode_hash: sha256:abc\n---\n")
        _init_git_repo(self.tmp)
        self.assertEqual(ccf.check_claims(self.tmp), [])

    def test_a_file_with_no_frontmatter_at_all_is_flagged_not_silently_skipped(self):
        _write(os.path.join(self.tmp, "mod.py"), self._function_source())
        _write(os.path.join(self.tmp, "claims", "foo.md"), "Just some prose, no frontmatter.\n")
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(len(problems), 1)
        self.assertIn("missing required frontmatter", problems[0][1])

    def test_a_tests_link_to_an_anchor_does_not_clobber_the_base_declarations_hash(self):
        # Real bug found while prototyping this checker against hams_com: a test file's own
        # "# Tests [@ANCHOR: name]" line matches the same ANCHOR_PATTERN as the base declaration,
        # so a naive "any function whose span contains this anchor text" scan hashes whichever
        # file/function is scanned LAST under that same anchor key -- silently hashing the test
        # function's own body instead of the real source function's, and reporting phantom
        # staleness (or phantom freshness) unrelated to the actual anchored code.
        _write(os.path.join(self.tmp, "mod.py"), self._function_source())
        _write(
            os.path.join(self.tmp, "test_mod.py"),
            "def test_foo():\n"
            "    # Tests [@ANCHOR: my_module:foo]\n"
            "    assert foo(1) == 2\n",
        )
        real_hash = ccf.compute_function_hash(os.path.join(self.tmp, "mod.py"))["my_module:foo"]
        _init_git_repo(self.tmp)
        index = ccf.build_anchor_hash_index(self.tmp)
        self.assertEqual(index["my_module:foo"], real_hash)

    def test_parse_claim_frontmatter_handles_quoted_values(self):
        content = '---\nanchor: "my_module:foo"\ncode_hash: \'sha256:abc\'\n---\nbody\n'
        fields = ccf.parse_claim_frontmatter(content)
        self.assertEqual(fields["anchor"], "my_module:foo")
        self.assertEqual(fields["code_hash"], "sha256:abc")


if __name__ == "__main__":
    unittest.main()
