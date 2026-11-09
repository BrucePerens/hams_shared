#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for the claim hash algorithms and the history check of legacy claims
(check_claims_freshness.py): v1 / v1b / v2, `hash_algorithm`, the date table, and the rule that a
legacy claim is not stale merely because comments or formatting changed since it was written.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("HAMS_CLAIM_CACHE_DIR", tempfile.mkdtemp())

import check_claims_freshness as ccf  # noqa: E402
import claim_normalizers as cn  # noqa: E402

PY = '''# [@ANCHOR: t:x]
def foo(a, b=1):
    """Add."""
    total = a + b  # add
    return total
'''
PY_COMMENTS = PY.replace("# add", "# a different comment").replace("Add.", "Else.")
PY_BLACK = '''# [@ANCHOR: t:x]
def foo(
    a,
    b = 1,
):
    total=a+b
    return total
'''
PY_CODE = PY.replace("a + b", "a - b")

SOURCES = {
    "py": (PY, PY_COMMENTS, PY_BLACK, PY_CODE),
    "rs": (
        "// [@ANCHOR: t:x]\nfn foo(a: i32, b: i32) -> i32 {\n    let t = a + b; // add\n    t\n}\n",
        "// [@ANCHOR: t:x]\n/// doc\nfn foo(a: i32, b: i32) -> i32 {\n    let t = a + b; /* other */\n    t\n}\n",
        "// [@ANCHOR: t:x]\nfn foo(\n    a: i32,\n    b: i32,\n) -> i32 {\n    let t=a+b;\n    t\n}\n",
        "// [@ANCHOR: t:x]\nfn foo(a: i32, b: i32) -> i32 {\n    let t = a - b; // add\n    t\n}\n",
    ),
    "js": (
        "// [@ANCHOR: t:x]\nfunction foo(a, b) {\n  const t = a + b; // add\n  return t;\n}\n",
        "// [@ANCHOR: t:x]\n/** doc */\nfunction foo(a, b) {\n  const t = a + b; /* other */\n  return t;\n}\n",
        "// [@ANCHOR: t:x]\nfunction foo(\n  a,\n  b,\n) {\n    const t = a+b\n    return t;\n}\n",
        "// [@ANCHOR: t:x]\nfunction foo(a, b) {\n  const t = a - b; // add\n  return t;\n}\n",
    ),
    "xml": (
        '<odoo>\n<!-- [@ANCHOR: t:x] -->\n<record id="a" model="m"><field name="v">1</field></record>\n</odoo>\n',
        '<odoo>\n<!-- [@ANCHOR: t:x] -->\n<!-- why -->\n<record id="a" model="m"><!-- c --><field name="v">1</field></record>\n</odoo>\n',
        '<odoo><!-- [@ANCHOR: t:x] --><record model="m" id="a">\n  <field name="v">\n 1 </field>\n</record></odoo>\n',
        '<odoo>\n<!-- [@ANCHOR: t:x] -->\n<record id="a" model="m"><field name="v">2</field></record>\n</odoo>\n',
    ),
    "sh": (
        "# [@ANCHOR: t:x]\nfoo() {\n  echo \"$1\" # say\n}\n",
        "# [@ANCHOR: t:x]\n# doc\nfoo() {\n  echo \"$1\"\n}\n",
        "# [@ANCHOR: t:x]\nfoo()\n{\n  echo \"$1\";\n}\n",
        "# [@ANCHOR: t:x]\nfoo() {\n  echo \"$2\" # say\n}\n",
    ),
    "sql": (
        "-- [@ANCHOR: t:x]\nCREATE VIEW v AS SELECT a FROM t; -- c\n",
        "-- [@ANCHOR: t:x]\n/* other */ CREATE VIEW v AS SELECT a FROM t;\n",
        "-- [@ANCHOR: t:x]\ncreate view v\nas select a\nfrom t ;\n",
        "-- [@ANCHOR: t:x]\nCREATE VIEW v AS SELECT b FROM t; -- c\n",
    ),
}


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.cache, ignore_errors=True)
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        self._clock = 1_700_000_000

    def _git(self, *args, date=None):
        env = dict(os.environ)
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
        subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True, env=env)

    def _commit(self, message="c", date=None):
        if date is None:
            self._clock += 100
            date = f"{self._clock} +0000"
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", message, date=date)

    def source(self, suffix):
        return os.path.join(self.tmp, f"mod.{suffix}")

    def claim(self, recorded, extra=""):
        _write(
            os.path.join(self.tmp, "claims", "foo.md"),
            f"---\nanchor: t:x\ncode_hash: sha256:{recorded}\n{extra}review_tier: 1\n---\n\nBody.\n",
        )

    def v2(self, text, suffix):
        return ccf.anchor_hashes_from_text(text, f"mod.{suffix}")["t:x"].v2

    def scan(self):
        ctx = ccf.FreshnessContext(cache_dir=self.cache)
        problems, _retired = ccf.scan_claims(self.tmp, ctx)
        self.ctx = ctx
        return problems


class V2ClaimsTests(RepoCase):
    """A claim declaring hash_algorithm v2: exact token-stream comparison, in every language."""

    def test_comment_and_format_changes_are_fresh_and_a_code_change_is_stale_in_every_language(self):
        for suffix, (base, comments, formatted, code) in SOURCES.items():
            for label, text, stale in (
                ("unchanged", base, False),
                ("comments", comments, False),
                ("formatting", formatted, False),
                ("code", code, True),
            ):
                with self.subTest(language=suffix, change=label):
                    self.setUp()
                    _write(self.source(suffix), base)
                    self.claim(self.v2(base, suffix), "hash_algorithm: v2\n")
                    self._commit("claim")
                    _write(self.source(suffix), text)
                    self._commit("change")
                    problems = self.scan()
                    if stale:
                        self.assertEqual(len(problems), 1, problems)
                        self.assertIn("code_hash mismatch", problems[0][1])
                    else:
                        self.assertEqual(problems, [])

    def test_a_v2_claim_is_never_given_a_history_lookup(self):
        _write(self.source("py"), PY)
        self.claim(self.v2(PY, "py"), "hash_algorithm: v2\n")
        self._commit()
        _write(self.source("py"), PY_CODE)
        self._commit()
        self.assertEqual(len(self.scan()), 1)
        self.assertEqual(self.ctx.history_lookups, 0)

    def test_an_unknown_algorithm_is_a_problem(self):
        _write(self.source("py"), PY)
        self.claim(self.v2(PY, "py"), "hash_algorithm: v9\n")
        self._commit()
        problems = self.scan()
        self.assertEqual(len(problems), 1)
        self.assertIn("unknown hash_algorithm", problems[0][1])

    def test_a_missing_anchor_is_still_a_hard_failure(self):
        _write(self.source("rs"), SOURCES["rs"][0].replace("t:x", "t:other"))
        self.claim("0" * 64, "hash_algorithm: v2\n")
        self._commit()
        problems = self.scan()
        self.assertEqual(len(problems), 1)
        self.assertIn("orphaned", problems[0][1])

    def test_not_computable_claims_are_still_accepted(self):
        _write(self.source("rs"), SOURCES["rs"][0])
        _write(
            os.path.join(self.tmp, "claims", "foo.md"),
            "---\nanchor: t:x\ncode_hash: not computable -- Rust claims-hashing tooling doesn't exist yet\n---\n",
        )
        self._commit()
        self.assertEqual(self.scan(), [])


class LegacyClaimTests(RepoCase):
    """A legacy Python claim (no hash_algorithm) is judged by what changed since its own commit."""

    def _legacy(self, field):
        h = ccf.function_hashes_from_text(PY, "mod.py")["t:x"]
        return getattr(h, field)

    def _setup(self, field, change):
        _write(self.source("py"), PY)
        self.claim(self._legacy(field))
        self._commit("claim")
        _write(self.source("py"), change)
        self._commit("change after the claim")

    def test_comment_only_change_since_the_claim_is_fresh(self):
        for field in ("v1", "v1b"):
            with self.subTest(field):
                self.setUp()
                self._setup(field, PY_COMMENTS)
                self.assertEqual(self.scan(), [])
                if field == "v1":  # v1b already ignores comments: no history needed
                    self.assertGreater(self.ctx.history_lookups, 0)

    def test_format_only_change_since_the_claim_is_fresh(self):
        for field in ("v1", "v1b"):
            with self.subTest(field):
                self.setUp()
                self._setup(field, PY_BLACK)
                self.assertEqual(self.scan(), [])

    def test_a_code_change_since_the_claim_is_stale_and_says_so(self):
        for field in ("v1", "v1b"):
            with self.subTest(field):
                self.setUp()
                self._setup(field, PY_CODE)
                problems = self.scan()
                self.assertEqual(len(problems), 1)
                self.assertIn("the code changed since", problems[0][1])

    def test_a_comment_change_then_a_code_change_is_stale(self):
        self._setup("v1", PY_COMMENTS)
        _write(self.source("py"), PY_CODE)
        self._commit()
        self.assertEqual(len(self.scan()), 1)

    def test_missing_history_falls_back_safely_and_reports_why(self):
        """The recorded hash is of code that no commit ever held: stale, with the reason."""
        _write(self.source("py"), PY)
        self.claim("ab" * 32)
        self._commit()
        problems = self.scan()
        self.assertEqual(len(problems), 1)
        self.assertIn("history cannot say what changed", problems[0][1])
        self.assertIn("treated as stale", problems[0][1])

    def test_an_uncommitted_claim_cannot_be_dated_and_is_reported(self):
        _write(self.source("py"), PY)
        self._commit()
        self.claim("ab" * 32)
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)  # tracked, not committed
        problems = self.scan()
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot be dated", problems[0][1])

    def test_the_verdict_is_cached_and_a_second_run_walks_no_history(self):
        self._setup("v1", PY_COMMENTS)
        self.assertEqual(self.scan(), [])
        first = self.ctx.history_lookups
        self.assertGreater(first, 0)
        self.assertTrue(os.path.exists(os.path.join(self.cache, "history_verdicts.json")))
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.ctx.history_lookups, 0)

    def test_the_cache_does_not_hide_a_later_code_change(self):
        self._setup("v1", PY_COMMENTS)
        self.assertEqual(self.scan(), [])
        _write(self.source("py"), PY_CODE)
        self._commit()
        self.assertEqual(len(self.scan()), 1)

    def test_a_claim_whose_own_commit_is_after_the_code_change_uses_the_revision_it_was_written_against(self):
        """The hash was computed from the code as of the claim's commit; later edits are what count."""
        _write(self.source("py"), PY_CODE)
        self._commit("code first")
        self.claim(ccf.function_hashes_from_text(PY_CODE, "mod.py")["t:x"].v1)
        self._commit("claim")
        _write(self.source("py"), PY_CODE.replace("# add", "# reworded"))
        self._commit("comment")
        self.assertEqual(self.scan(), [])


class DateTableTests(RepoCase):
    def test_the_table_maps_dates_to_algorithms(self):
        self.assertEqual(ccf.algorithm_for_date("2026-09-01"), "v1")
        self.assertEqual(ccf.algorithm_for_date("2026-11-09"), "v1b")
        self.assertEqual(ccf.algorithm_for_date("2026-11-09T23:00:00+00:00"), "v1b")
        self.assertEqual(ccf.algorithm_for_date("2026-11-10"), "v2")
        self.assertEqual(ccf.algorithm_for_date(None), "v2")

    def test_the_date_comes_from_the_commit_not_the_file_modification_time(self):
        _write(self.source("py"), PY)
        self.claim("ab" * 32)
        self._commit("old claim", date="2026-10-01T12:00:00 +0000")
        claim = os.path.join(self.tmp, "claims", "foo.md")
        os.utime(claim, (time.time() + 10_000_000, time.time() + 10_000_000))
        ctx = ccf.FreshnessContext(cache_dir=self.cache)
        self.assertEqual(ctx.claim_date({}, claim), "2026-10-01")
        self.assertEqual(ccf.algorithm_for_date(ctx.claim_date({}, claim)), "v1")

    def test_a_front_matter_date_wins_over_the_commit(self):
        ctx = ccf.FreshnessContext(cache_dir=self.cache)
        self.assertEqual(ctx.claim_date({"date": "2026-11-09"}, "/nonexistent/x.md"), "2026-11-09")

    def test_the_problem_names_the_algorithm_the_claim_was_written_with(self):
        _write(self.source("py"), PY)
        self.claim("ab" * 32)
        self._commit("old claim", date="2026-10-01T12:00:00 +0000")
        problems = self.scan()
        self.assertIn("hash algorithm v1", problems[0][1])


class SpeedTests(RepoCase):
    N = 2400

    def test_a_store_of_2400_current_claims_needs_no_git_history_walk(self):
        per_file = 60
        for f in range(self.N // per_file):
            body = "".join(
                f"# [@ANCHOR: m{f}:fn{k}]\ndef fn{k}(a):\n    # note\n    return a + {k}\n\n" for k in range(per_file)
            )
            _write(os.path.join(self.tmp, f"mod{f}.py"), body)
        mix = 0
        # write each claim with the hash of its function under a rotating algorithm
        for f in range(self.N // per_file):
            with open(os.path.join(self.tmp, f"mod{f}.py"), encoding="utf-8") as h:
                hashes = ccf.function_hashes_from_text(h.read(), "mod.py")
            for k in range(per_file):
                h = hashes[f"m{f}:fn{k}"]
                recorded, extra = [(h.v1, ""), (h.v1b, ""), (h.v2, "hash_algorithm: v2\n")][mix % 3]
                mix += 1
                _write(
                    os.path.join(self.tmp, "claims", f"m{f}_fn{k}.md"),
                    f"---\nanchor: m{f}:fn{k}\ncode_hash: sha256:{recorded}\n{extra}---\n\nBody.\n",
                )
        self._commit("store")
        calls = []
        real = ccf._git_out

        def counting(root, *args):
            calls.append(args[0])
            return real(root, *args)

        start = time.time()
        with mock.patch.object(ccf, "_git_out", counting):
            problems = self.scan()
        elapsed = time.time() - start
        self.assertEqual(problems, [])
        self.assertEqual([c for c in calls if c in ("log", "show")], [], "a current store must not walk history")
        self.assertLess(elapsed, 60, f"{self.N} claims took {elapsed:.1f}s")


if __name__ == "__main__":
    unittest.main()
