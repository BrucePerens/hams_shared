#!/usr/bin/env python3
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
import migrate_claim_hashes as mch  # noqa: E402


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
        """A claim under
        docs/bug_hunt_claims/<repo>/... documents code that lives in a DIFFERENT git repo
        (hams_open/hams_shared), per this project's own centralized-claims-store policy (the
        skill's own docs: 'Verify a centralized claim with ... check_claims.py ... since the
        normal check_claims_freshness.py assumes the claim and its code share one repo root').
        This checker's own `f'{os.sep}claims{os.sep}' in claim_path` test doesn't
        distinguish a centralized-store claim from a co-located one -- it tries to verify the
        centralized claim's anchor against ITS OWN repo's Python files, never finds it (the real
        anchor lives in a different repo entirely), and reports it as 'orphaned claim' -- a false
        positive on every single centralized claim: running this checker against
        the real hams_com repo produced over 1100 'orphaned claim' false
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
        claims/, plus several written for un-anchored Python
        functions) for a claim this checker structurally cannot hash yet (a Rust/JS anchor, or no
        real [@ANCHOR:] at all) is `code_hash: not computable -- <reason>`, not a real sha256.
        Without special handling, this checker would not recognize that convention: it would still try to
        resolve the anchor against its own Python-only index, never find it (the real anchor, if
        any, lives in a .rs/.js file this checker can't parse), and report "orphaned claim" --
        a false positive, and a misleading one (implying the anchor was deleted, when in fact it
        was simply never Python). In the real hams_com
        repo, ALL 344 "orphaned claim" findings left once centralized-store claims are excluded were
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
        """A real false positive against the real hams_com repo: hardware/claims/
        README.md is a real, deliberate index/explanation file for the directory ('hardware/ --
        reviewed, out of scope for claims'), not a claim -- it carries no
        frontmatter at all because it isn't one. This checker's own
        `f'{os.sep}claims{os.sep}' in claim_path` test doesn't distinguish a README from a real
        claim file, so it would be reported as 'missing required frontmatter fields' -- a false
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
        # A test file's own
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


class UnclaimedAnchorTests(unittest.TestCase):
    """`--unclaimed`: the inventory of implementation anchors that have no claim file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write(os.path.join(self.tmp, "my_mod", "__manifest__.py"), "{'name': 'my_mod'}\n")

    def _src(self, rel, content):
        _write(os.path.join(self.tmp, rel), content)

    def _claim(self, rel, anchor):
        _write(
            os.path.join(self.tmp, rel),
            f"---\nanchor: {anchor}\ncode_hash: sha256:00\nreview_tier: 3\n---\n\n1. Claim.\n",
        )

    def _unclaimed(self, store_roots=()):
        _init_git_repo(self.tmp)
        return [(m, n) for m, n, _loc in ccf.find_unclaimed_anchors(self.tmp, store_roots)]

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_an_anchor_without_a_claim_file_is_listed_and_one_with_a_claim_is_not(self):
        self._src(
            "my_mod/models/thing.py",
            "def claimed():\n    # [@ANCHOR: my_mod:claimed]\n    return 1\n\n\n"
            "def unclaimed():\n    # [@ANCHOR: unclaimed]\n    return 2\n",
        )
        self._claim("my_mod/claims/claimed.md", "my_mod:claimed")
        self.assertEqual(self._unclaimed(), [("my_mod", "unclaimed")])

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_the_location_is_reported_relative_to_the_scanned_directory(self):
        self._src("my_mod/models/thing.py", "def f():\n    # [@ANCHOR: lonely]\n    pass\n")
        _init_git_repo(self.tmp)
        self.assertEqual(
            ccf.find_unclaimed_anchors(self.tmp),
            [("my_mod", "lonely", os.path.join("my_mod", "models", "thing.py") + ":2")],
        )

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_links_doc_anchors_and_conversational_mentions_are_not_implementation_anchors(self):
        self._src(
            "my_mod/models/thing.py",
            "def f():\n"
            "    # Verified by [@ANCHOR: test_f_works]\n"
            "    # Triggers [@ANCHOR: other_mod:handoff]\n"
            "    # see [@ANCHOR: somewhere_else]\n"
            "    # [@ANCHOR: doc_f_overview]\n"
            "    # [@ANCHOR: story_f_user_story]\n"
            "    pass\n",
        )
        self.assertEqual(self._unclaimed(), [])

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_anchors_in_test_files_are_test_anchors_and_skipped(self):
        self._src(
            "my_mod/tests/test_thing.py",
            "# Tests [@ANCHOR: my_mod:claimed]\n"
            "def test_it():\n    # [@ANCHOR: test_it_base_declaration]\n    pass\n",
        )
        self._src("my_mod/static/tests/thing.test.js", "// [@ANCHOR: js_test_anchor]\n")
        self._src("daemons/relay/src/thing.rs.test", "// [@ANCHOR: rust_test_anchor]\n")
        self.assertEqual(self._unclaimed(), [])

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_a_claim_matches_by_its_anchor_field_even_when_the_file_name_differs(self):
        self._src("my_mod/models/thing.py", "def f():\n    # [@ANCHOR: COMM_mcp_f_tool]\n    pass\n")
        self._claim("my_mod/claims/f.md", "my_mod:mcp_f_tool")
        self.assertEqual(self._unclaimed(), [])

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_a_claim_in_another_modules_claims_directory_does_not_count(self):
        _write(os.path.join(self.tmp, "other_mod", "__manifest__.py"), "{'name': 'other_mod'}\n")
        self._src("my_mod/models/thing.py", "def f():\n    # [@ANCHOR: shared_name]\n    pass\n")
        self._claim("other_mod/claims/shared_name.md", "shared_name")
        self.assertEqual(self._unclaimed(), [("my_mod", "shared_name")])

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_rust_type_method_anchors_match_a_claim_naming_the_same_type_method(self):
        self._src(
            "daemons/relay/src/est.rs",
            "impl Est {\n    // [@ANCHOR: Est::new]\n    fn new() {}\n"
            "    // [@ANCHOR: Est::observe]\n    fn observe() {}\n}\n",
        )
        self._claim("daemons/relay/claims/est_new.md", "Est::new")
        self.assertEqual(self._unclaimed(), [("Est", ":observe")])

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_a_centralized_claims_store_counts_by_anchor_name(self):
        self._src("my_mod/models/thing.py", "def f():\n    # [@ANCHOR: stored_elsewhere]\n    pass\n")
        store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        _write(
            os.path.join(store, "my_mod", "models", "claims", "stored_elsewhere.md"),
            "---\nanchor: stored_elsewhere\ncode_hash: sha256:00\n---\n",
        )
        self.assertEqual(self._unclaimed(store_roots=[store]), [])
        self.assertEqual(
            [(m, n) for m, n, _loc in ccf.find_unclaimed_anchors(self.tmp)],
            [("my_mod", "stored_elsewhere")],
        )

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_a_retired_claim_still_counts_as_a_decision_about_its_anchor(self):
        self._src("my_mod/models/thing.py", "def f():\n    # [@ANCHOR: old_name]\n    pass\n")
        _write(
            os.path.join(self.tmp, "my_mod", "claims", "old_name.md"),
            "---\nanchor: old_name\ncode_hash: sha256:00\nretired: 2026-09-15\n"
            "retired_reason: deleted\n---\n",
        )
        self.assertEqual(self._unclaimed(), [])

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_main_unclaimed_lists_and_exits_zero(self):
        self._src("my_mod/models/thing.py", "def f():\n    # [@ANCHOR: lonely]\n    pass\n")
        _init_git_repo(self.tmp)
        out = io.StringIO()
        with mock.patch("sys.argv", ["check_claims_freshness.py", "--unclaimed", self.tmp]), \
                contextlib.redirect_stdout(out):
            rc = ccf.main()
        self.assertEqual(rc, 0)
        self.assertIn("my_mod:lonely", out.getvalue())
        self.assertIn("TOTAL: 1 implementation anchor(s) in 1 module(s)", out.getvalue())

    # Tests [@ANCHOR: COMM_unclaimed_anchors]
    def test_main_rejects_a_missing_claims_store(self):
        _init_git_repo(self.tmp)
        out = io.StringIO()
        argv = ["check_claims_freshness.py", "--unclaimed", self.tmp, "--claims-store",
                os.path.join(self.tmp, "nope")]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(out):
            rc = ccf.main()
        self.assertEqual(rc, 2)
        self.assertIn("not a directory", out.getvalue())



_COMMENTED = (
    "# Doubles the input.\n"
    "# [@ANCHOR: my_module:foo]\n"
    "def foo(x):\n"
    '    """Return x plus one."""\n'
    "    # add one\n"
    "    y = x + 1  # the sum\n"
    '    return y, "# not a comment"\n'
)


class CodeHashIgnoresCommentsTests(unittest.TestCase):
    """`code_hash` covers code, not prose: a comment-only change must not stale a claim, a code
    change must (2026-11-09: comment-rewording pull requests staled 27 claims)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "mod.py")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hash(self, source):
        _write(self.path, source)
        return ccf.compute_function_hash(self.path)["my_module:foo"]

    def _claim_problems(self, source, recorded):
        _write(self.path, source)
        _write(
            os.path.join(self.tmp, "claims", "foo.md"),
            f"---\nanchor: my_module:foo\ncode_hash: sha256:{recorded}\n---\n\n1. Claim.\n",
        )
        _init_git_repo(self.tmp)
        return ccf.check_claims(self.tmp)

    def test_rewording_comments_and_docstring_keeps_the_hash(self):
        reworded = (
            "# [@ANCHOR: my_module:foo]\n"
            "def foo(x):\n"
            '    """Something else entirely,\n    over two lines."""\n'
            "\n"
            "    # a different comment\n"
            "    y = x + 1\n"
            '    return y, "# not a comment"   # trailing\n'
        )
        self.assertEqual(self._hash(_COMMENTED), self._hash(reworded))

    def test_a_comment_only_change_does_not_stale_a_claim(self):
        recorded = self._hash(_COMMENTED)
        reworded = _COMMENTED.replace("add one", "increment").replace("the sum", "result")
        self.assertEqual(self._claim_problems(reworded, recorded), [])

    def test_a_code_change_stales_a_claim(self):
        recorded = self._hash(_COMMENTED)
        problems = self._claim_problems(_COMMENTED.replace("x + 1", "x + 2"), recorded)
        self.assertEqual(len(problems), 1)
        self.assertIn("code_hash mismatch", problems[0][1])

    def test_a_changed_string_literal_stales_a_claim(self):
        """A `#` inside a string is code, and a string literal can carry behaviour."""
        self.assertNotEqual(
            self._hash(_COMMENTED), self._hash(_COMMENTED.replace("# not a comment", "# other"))
        )

    def test_a_changed_indentation_or_statement_order_changes_the_hash(self):
        swapped = (
            "# [@ANCHOR: my_module:foo]\n"
            "def foo(x):\n"
            "    y = x + 1\n"
            "    if x:\n"
            "        return y\n"
            "    return None\n"
        )
        nested = swapped.replace("    return None\n", "        return None\n")
        self.assertNotEqual(self._hash(swapped), self._hash(nested))

    def test_nested_function_docstrings_are_ignored_too(self):
        src = (
            "# [@ANCHOR: my_module:foo]\n"
            "def foo(x):\n"
            "    def inner():\n"
            '        """Inner docs."""\n'
            "        return x\n"
            "    return inner\n"
        )
        self.assertEqual(self._hash(src), self._hash(src.replace("Inner docs.", "Reworded.")))

    def test_a_tests_list_in_a_test_docstring_does_not_take_the_anchor_from_the_real_function(self):
        _write(self.path, _COMMENTED)
        test_path = os.path.join(self.tmp, "test_mod.py")
        _write(
            test_path,
            "def test_foo():\n"
            '    """Tests [@ANCHOR: other:x] [@ANCHOR: my_module:foo]: it works."""\n'
            "    assert True\n",
        )
        _init_git_repo(self.tmp)
        index = ccf.build_anchor_hash_index(self.tmp)
        self.assertEqual(index, {"my_module:foo": ccf.compute_function_hash(self.path)["my_module:foo"]})

    def test_a_legacy_hash_of_unchanged_text_is_still_accepted(self):
        """Claims written before the definition changed keep passing until migrated."""
        _write(self.path, _COMMENTED)
        legacy = ccf.compute_legacy_function_hash(self.path)["my_module:foo"]
        self.assertNotEqual(legacy, ccf.compute_function_hash(self.path)["my_module:foo"])
        self.assertEqual(self._claim_problems(_COMMENTED, legacy), [])

    def test_a_legacy_hash_of_changed_text_is_stale(self):
        _write(self.path, _COMMENTED)
        legacy = ccf.compute_legacy_function_hash(self.path)["my_module:foo"]
        problems = self._claim_problems(_COMMENTED.replace("add one", "increment"), legacy)
        self.assertEqual(len(problems), 1)


class MigrateClaimHashesTests(unittest.TestCase):
    """The one-sweep migration to the comment-insensitive hash."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "mod.py")
        self.claim = os.path.join(self.tmp, "claims", "foo.md")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)

    def _commit(self, message):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)

    def _claim_text(self):
        with open(self.claim, encoding="utf-8") as f:
            return f.read()

    def _setup_history(self):
        """Commit 1: the claim is written against _COMMENTED's raw hash. Commit 2: comments
        reworded. Returns the legacy hash recorded in commit 1."""
        _write(self.path, _COMMENTED)
        legacy = ccf.compute_legacy_function_hash(self.path)["my_module:foo"]
        _write(self.claim, f"---\nanchor: my_module:foo\ncode_hash: sha256:{legacy}\nreview_tier: 1\n---\n\nBody.\n")
        _init_git_repo(self.tmp)
        return legacy

    def _run(self, apply):
        return mch.migrate([self.tmp], [self.tmp], apply=apply, out=lambda *_: None)

    def test_unchanged_code_legacy_hash_is_rewritten(self):
        self._setup_history()
        result = self._run(apply=True)
        self.assertEqual(len(result["legacy"]), 1)
        norm = ccf.compute_function_hash(self.path)["my_module:foo"]
        self.assertIn(f"code_hash: sha256:{norm}\n", self._claim_text())
        self.assertEqual(ccf.check_claims(self.tmp), [])

    def test_comment_only_change_since_the_claim_is_rewritten_via_history(self):
        self._setup_history()
        _write(self.path, _COMMENTED.replace("add one", "increment"))
        self._commit("reword comment")
        result = self._run(apply=True)
        self.assertEqual(len(result["comment-only"]), 1)
        self.assertEqual(result["stale"], [])
        self.assertEqual(ccf.check_claims(self.tmp), [])
        self.assertIn("Body.\n", self._claim_text())

    def test_a_real_code_change_since_the_claim_is_reported_and_not_rewritten(self):
        self._setup_history()
        before = self._claim_text()
        _write(self.path, _COMMENTED.replace("x + 1", "x + 2"))
        self._commit("change code")
        result = self._run(apply=True)
        self.assertEqual(len(result["stale"]), 1)
        self.assertEqual(self._claim_text(), before)

    def test_report_only_without_apply_and_orphans_are_listed(self):
        self._setup_history()
        before = self._claim_text()
        _write(
            os.path.join(self.tmp, "claims", "gone.md"),
            "---\nanchor: my_module:gone\ncode_hash: sha256:00\n---\n",
        )
        self._commit("orphan")
        result = self._run(apply=False)
        self.assertEqual(len(result["legacy"]), 1)
        self.assertEqual(len(result["orphan"]), 1)
        self.assertEqual(self._claim_text(), before)


if __name__ == "__main__":
    unittest.main()
