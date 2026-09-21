#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_claims_freshness.py.
"""

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

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

    def test_a_centralized_store_claim_is_not_flagged_orphaned_it_belongs_to_check_claims_py(self):
        """Real bug found running this checker against the actual hams_com repo for the first
        time ever (it has never been wired into run_linters.py -- ADR 0091 itself names this as a
        real, tracked follow-on, 'not yet wired... not forgotten'): a claim under
        docs/bug_hunt_claims/<repo>/... documents code that lives in a DIFFERENT git repo
        (hams_open/hams_shared), per this project's own centralized-claims-store policy (the
        skill's own docs: 'Verify a centralized claim with ... check_claims.py ... since the
        normal check_claims_freshness.py assumes the claim and its code share one repo root').
        Before this fix, this checker's own `f'{os.sep}claims{os.sep}' in claim_path` test doesn't
        distinguish a centralized-store claim from a co-located one -- it tries to verify the
        centralized claim's anchor against ITS OWN repo's Python files, never finds it (the real
        anchor lives in a different repo entirely), and reports it as 'orphaned claim' -- a false
        positive on every single centralized claim, confirmed live: running this checker against
        the real hams_com repo for the first time produced over 1100 'orphaned claim' false
        positives, the overwhelming majority under docs/bug_hunt_claims/, none of them real."""
        _write(os.path.join(self.tmp, "mod.py"), "def bar():\n    pass\n")
        _write(
            os.path.join(self.tmp, "docs", "bug_hunt_claims", "hams_open", "some_module", "claims", "foo.md"),
            "---\nanchor: some_module:foo\ncode_hash: sha256:abc123\n---\n\n1. Does something.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(problems, [])

    def test_a_not_computable_code_hash_is_skipped_not_flagged_orphaned(self):
        """The established, already-in-active-use convention (this checker's own predecessor
        claims across the real repo -- every Rust-anchored claim in daemons/hams_local_relay/
        claims/, plus several written by this exact bug-hunt dispatch for un-anchored Python
        functions) for a claim this checker structurally cannot hash yet (a Rust/JS anchor, or no
        real [@ANCHOR:] at all) is `code_hash: not computable -- <reason>`, not a real sha256.
        Before this fix, this checker didn't recognize that convention at all: it still tried to
        resolve the anchor against its own Python-only index, never found it (the real anchor, if
        any, lives in a .rs/.js file this checker can't parse), and reported "orphaned claim" --
        a false positive, and a misleading one (implying the anchor was deleted, when in fact it
        was simply never Python). Confirmed live: running this checker against the real hams_com
        repo, ALL 344 of its post-centralized-store-fix "orphaned claim" findings turned out to be
        claims using this exact `not computable` convention, none of them genuinely orphaned."""
        _write(os.path.join(self.tmp, "mod.py"), "def bar():\n    pass\n")
        _write(
            os.path.join(self.tmp, "claims", "foo.md"),
            "---\nanchor: some_crate:foo\ncode_hash: not computable -- Rust claims-hashing "
            "tooling doesn't exist yet\n---\n\n1. Does something.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(problems, [])

    def test_a_readme_inside_a_claims_directory_is_not_treated_as_a_claim(self):
        """Real, live false positive found against the real hams_com repo: hardware/claims/
        README.md is a real, deliberate index/explanation file for the directory ('hardware/ --
        reviewed, out of scope for the bug-hunt campaign'), not a claim -- it carries no
        frontmatter at all because it isn't one. Before this fix, this checker's own
        `f'{os.sep}claims{os.sep}' in claim_path` test doesn't distinguish a README from a real
        claim file, so it was reported as 'missing required frontmatter fields' -- a false
        positive on a file that was never meant to have any."""
        _write(os.path.join(self.tmp, "mod.py"), "def bar():\n    pass\n")
        _write(
            os.path.join(self.tmp, "claims", "README.md"),
            "# claims/ -- what this directory is for\n\nJust an explanation, not a claim.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(problems, [])

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

    def test_a_stale_claim_under_an_excluded_top_level_dir_is_not_flagged(self):
        """EXCLUDED_TOP_LEVEL_DIRS (ingest/, ics_forms/, ics_training/): another session has
        real, active, in-progress work landing throughout these directories the same night this
        gate was wired into run_linters.py (2026-09-18), so a stale claim there is expected
        churn, not a real gap -- see check_claims_freshness.py's own EXCLUDED_TOP_LEVEL_DIRS
        comment for the full history."""
        _write(os.path.join(self.tmp, "ingest", "mod.py"), self._function_source())
        _write(
            os.path.join(self.tmp, "ingest", "claims", "foo.md"),
            "---\nanchor: my_module:foo\ncode_hash: sha256:0000000000000000000000000000000000000000000000000000000000000000\n---\n\n1. Returns x + 1.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(problems, [])

    def test_a_stale_claim_outside_any_excluded_dir_is_still_flagged(self):
        """The exclusion is scoped to the three named top-level directories only -- a stale claim
        anywhere else in the same repo must still be caught."""
        _write(os.path.join(self.tmp, "ingest", "mod.py"), self._function_source())
        _write(
            os.path.join(self.tmp, "ingest", "claims", "foo.md"),
            "---\nanchor: my_module:foo\ncode_hash: sha256:0000000000000000000000000000000000000000000000000000000000000000\n---\n\n1. Returns x + 1.\n",
        )
        _write(
            os.path.join(self.tmp, "mymod", "mod.py"),
            "def bar(x):\n    # [@ANCHOR: mymod:bar]\n    return x + 2\n",
        )
        _write(
            os.path.join(self.tmp, "mymod", "claims", "bar.md"),
            "---\nanchor: mymod:bar\ncode_hash: sha256:0000000000000000000000000000000000000000000000000000000000000000\n---\n\n1. Returns x + 2.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(len(problems), 1)
        self.assertIn("mymod", problems[0][0])

    def test_a_directory_merely_starting_with_an_excluded_name_is_not_excluded(self):
        """Exact top-level path-component match only -- `ingest_foo/` must not be swept in by a
        `startswith` accident."""
        _write(os.path.join(self.tmp, "ingest_foo", "mod.py"), self._function_source())
        _write(
            os.path.join(self.tmp, "ingest_foo", "claims", "foo.md"),
            "---\nanchor: my_module:foo\ncode_hash: sha256:0000000000000000000000000000000000000000000000000000000000000000\n---\n\n1. Returns x + 1.\n",
        )
        _init_git_repo(self.tmp)
        problems = ccf.check_claims(self.tmp)
        self.assertEqual(len(problems), 1)

    def test_parse_claim_frontmatter_handles_quoted_values(self):
        content = '---\nanchor: "my_module:foo"\ncode_hash: \'sha256:abc\'\n---\nbody\n'
        fields = ccf.parse_claim_frontmatter(content)
        self.assertEqual(fields["anchor"], "my_module:foo")
        self.assertEqual(fields["code_hash"], "sha256:abc")


# Tests [@ANCHOR: COMM_claim_retirement]

class ClaimRetirementTests(unittest.TestCase):
    """A claim whose anchored function was deliberately deleted or renamed is kept on purpose --
    it holds the original finding -- but its anchor can no longer resolve. Before the retirement
    convention, every such claim was reported as an orphan forever, and a gate that can never go
    green stops being read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_claim(self, relpath, frontmatter, body="Historical record.\n"):
        path = os.path.join(self.tmp, relpath)
        _write(path, "---\n%s---\n\n%s" % (frontmatter, body))
        return path

    def test_a_retired_claim_with_a_reason_is_skipped_not_orphaned(self):
        self._write_claim(
            os.path.join("claims", "gone.md"),
            "anchor: my_module:deleted_function\n"
            "code_hash: sha256:deadbeef\n"
            "retired: 2026-09-15\n"
            "retired_reason: deleted\n",
        )
        _init_git_repo(self.tmp)
        problems, retired = ccf.scan_claims(self.tmp)
        self.assertEqual(problems, [])
        self.assertEqual(len(retired), 1)

    def test_a_retired_claim_naming_a_live_successor_is_skipped(self):
        self._write_claim(
            os.path.join("claims", "new_name.md"),
            "anchor: my_module:new_name\ncode_hash: not computable\n",
        )
        self._write_claim(
            os.path.join("claims", "old_name.md"),
            "anchor: my_module:old_name\n"
            "code_hash: sha256:deadbeef\n"
            "retired: 2026-09-15\n"
            "superseded_by: new_name.md\n",
        )
        _init_git_repo(self.tmp)
        problems, retired = ccf.scan_claims(self.tmp)
        self.assertEqual(problems, [])
        self.assertEqual(len(retired), 2 - 1)  # only old_name.md is retired

    def test_a_successor_in_another_module_resolves_against_the_repo_root(self):
        """The real case this exists for: a claim whose function moved to a different module, so
        the successor is not a sibling file."""
        self._write_claim(
            os.path.join("other_module", "claims", "moved.md"),
            "anchor: other_module:moved\ncode_hash: not computable\n",
        )
        self._write_claim(
            os.path.join("my_module", "claims", "moved.md"),
            "anchor: my_module:moved\n"
            "code_hash: sha256:deadbeef\n"
            "retired: 2026-09-15\n"
            "superseded_by: other_module/claims/moved.md\n",
        )
        _init_git_repo(self.tmp)
        problems, _retired = ccf.scan_claims(self.tmp)
        self.assertEqual(problems, [])

    def test_retired_alone_cannot_silence_an_orphan(self):
        """The whole point of requiring one of the two fields: otherwise `retired:` is a one-line
        way to hide a genuinely orphaned claim, which is what the checker exists to surface."""
        self._write_claim(
            os.path.join("claims", "sneaky.md"),
            "anchor: my_module:vanished\ncode_hash: sha256:deadbeef\nretired: 2026-09-15\n",
        )
        _init_git_repo(self.tmp)
        problems, retired = ccf.scan_claims(self.tmp)
        self.assertEqual(len(retired), 1, "it is still counted as retired")
        self.assertEqual(len(problems), 1)
        self.assertIn("neither 'superseded_by:' nor 'retired_reason:'", problems[0][1])

    def test_a_superseded_by_typo_is_an_error(self):
        self._write_claim(
            os.path.join("claims", "old_name.md"),
            "anchor: my_module:old_name\n"
            "code_hash: sha256:deadbeef\n"
            "retired: 2026-09-15\n"
            "superseded_by: no_such_claim.md\n",
        )
        _init_git_repo(self.tmp)
        problems, _retired = ccf.scan_claims(self.tmp)
        self.assertEqual(len(problems), 1)
        self.assertIn("names no existing claim file", problems[0][1])

    def test_a_successor_that_is_itself_retired_is_an_error(self):
        """Otherwise a chain of retirements hides the claim just as effectively as a typo."""
        self._write_claim(
            os.path.join("claims", "middle.md"),
            "anchor: my_module:middle\n"
            "code_hash: sha256:deadbeef\n"
            "retired: 2026-09-15\n"
            "retired_reason: deleted\n",
        )
        self._write_claim(
            os.path.join("claims", "oldest.md"),
            "anchor: my_module:oldest\n"
            "code_hash: sha256:deadbeef\n"
            "retired: 2026-09-15\n"
            "superseded_by: middle.md\n",
        )
        _init_git_repo(self.tmp)
        problems, _retired = ccf.scan_claims(self.tmp)
        self.assertEqual(len(problems), 1)
        self.assertIn("is itself retired", problems[0][1])

    def test_a_live_claim_is_unaffected_by_the_new_field(self):
        _write(os.path.join(self.tmp, "mod.py"),
               "def foo(x):\n    # [@ANCHOR: my_module:foo]\n    return x + 1\n")
        real_hash = ccf.compute_function_hash(
            os.path.join(self.tmp, "mod.py"))["my_module:foo"]
        self._write_claim(
            os.path.join("claims", "foo.md"),
            "anchor: my_module:foo\ncode_hash: sha256:%s\n" % real_hash,
        )
        _init_git_repo(self.tmp)
        problems, retired = ccf.scan_claims(self.tmp)
        self.assertEqual(problems, [])
        self.assertEqual(retired, [])

    def test_check_claims_still_returns_only_problems(self):
        """Its long-standing callers pass its result straight to a list comparison."""
        # _init_git_repo commits, so the repo needs at least one file to commit.
        _write(os.path.join(self.tmp, "README.md"), "no claims here\n")
        _init_git_repo(self.tmp)
        self.assertEqual(ccf.check_claims(self.tmp), [])



class TestMainRejectsAMissingDirectory(unittest.TestCase):
    def test_a_nonexistent_directory_is_an_error_not_a_success(self):
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
        missing = os.path.join(parent, "no_such_module")
        out = io.StringIO()
        with mock.patch("sys.argv", ["check_claims_freshness.py", missing]), contextlib.redirect_stdout(out):
            rc = ccf.main()
        self.assertEqual(rc, 2)
        self.assertNotIn("SUCCESS", out.getvalue())
        self.assertIn("is not a directory", out.getvalue())

    def test_an_existing_directory_with_no_claims_still_succeeds(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        out = io.StringIO()
        with mock.patch("sys.argv", ["check_claims_freshness.py", empty]), contextlib.redirect_stdout(out):
            rc = ccf.main()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
