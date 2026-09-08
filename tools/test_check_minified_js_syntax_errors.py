#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_minified_js_syntax_errors.py.

_check_one() shells out to the real rjsmin.jsmin() (the exact call Odoo's
own asset pipeline makes) and a real `node --input-type=module --check`
subprocess, matching test_check_js_syntax.py's own established convention
of letting node run for real rather than mocking it. The "minification
actually breaks otherwise-valid syntax" case is hard to construct
against whatever rjsmin version happens to be installed (its own docs
claim only nested template literals are unsupported, and that specific
construct no longer reproduces against the version installed in this dev
environment -- confirmed directly, not assumed). So the detection-path
tests below instead use a source file that is already syntactically
invalid before minification: rjsmin does not validate JS grammar, only
transforms whitespace/comments, so invalid input passes through as
invalid output -- confirmed directly (rjsmin.jsmin("const x = ;") ==
"const x=;"). This exercises the exact same real rjsmin -> real node
--check pipeline the tool uses in production, without needing a
version-specific rjsmin miscompilation to already be known.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_minified_js_syntax_errors as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_minified_js_syntax_errors.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CollectMinifiedJsAssetsTests(unittest.TestCase):
    # Same underlying logic as check_minified_js_nested_templates.py's own
    # collector (this file keeps its own copy, per that tool's own
    # documented convention of staying independently invocable) -- covered
    # here too rather than assumed identical.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_collects_js_assets_from_a_real_manifest_on_disk(self):
        _write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {\n"
            "        'web.assets_backend': ['mod_a/static/src/js/a.js', 'mod_a/static/src/js/b.js'],\n"
            "    },\n"
            "}\n",
        )
        result = chk.collect_minified_js_assets(self.tmp)
        self.assertEqual(
            set(result.keys()), {"mod_a/static/src/js/a.js", "mod_a/static/src/js/b.js"}
        )

    def test_non_js_assets_are_not_collected(self):
        _write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {\n"
            "        'web.assets_backend': ['mod_a/static/src/css/a.css', 'mod_a/static/src/js/b.js'],\n"
            "    },\n"
            "}\n",
        )
        result = chk.collect_minified_js_assets(self.tmp)
        self.assertEqual(set(result.keys()), {"mod_a/static/src/js/b.js"})


class ResolveAssetPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolves_against_the_first_matching_search_root(self):
        real = os.path.join(self.tmp, "root_a", "mod_a", "static", "src", "js", "a.js")
        os.makedirs(os.path.dirname(real))
        with open(real, "w") as f:
            f.write("// x")
        result = chk.resolve_asset_path(
            "mod_a/static/src/js/a.js",
            [os.path.join(self.tmp, "root_a"), os.path.join(self.tmp, "root_b")],
        )
        self.assertEqual(result, real)

    def test_returns_none_when_no_search_root_has_the_file(self):
        result = chk.resolve_asset_path(
            "mod_a/static/src/js/missing.js", [os.path.join(self.tmp, "root_a")]
        )
        self.assertIsNone(result)


class CheckOneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self, content):
        p = os.path.join(self.tmp, "asset.js")
        _write(p, content)
        return p

    def test_valid_source_that_stays_valid_after_real_minification_returns_none(self):
        p = self._path("export function f(x) {\n    return x + 1;\n}\n")
        self.assertIsNone(chk._check_one(("mod_a/static/src/js/asset.js", p)))

    def test_source_already_broken_before_minification_is_still_caught_after_it(self):
        # rjsmin doesn't validate JS grammar (confirmed directly: it passes
        # "const x = ;" through as "const x=;"), so this exercises the real
        # rjsmin -> real node --check pipeline end to end without needing a
        # version-specific rjsmin miscompilation.
        p = self._path("const x = ;\n")
        result = chk._check_one(("mod_a/static/src/js/asset.js", p))
        self.assertIsNotNone(result)
        asset_path, real_path, err = result
        self.assertEqual(asset_path, "mod_a/static/src/js/asset.js")
        self.assertEqual(real_path, p)
        self.assertIn("asset.js", err)
        self.assertNotIn("[stdin]", err)

    def test_an_unreadable_file_reports_the_read_failure_instead_of_crashing(self):
        result = chk._check_one(("mod_a/static/src/js/missing.js", os.path.join(self.tmp, "missing.js")))
        self.assertIsNotNone(result)
        _asset_path, _real_path, err = result
        self.assertIn("could not read file", err)


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout + result.stderr

    def test_no_bundled_js_assets_passes_with_its_own_message(self):
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("No bundled JS assets found", out)

    def test_a_clean_bundled_js_file_passes(self):
        _write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {'web.assets_backend': ['mod_a/static/src/js/a.js']},\n"
            "}\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "a.js"),
            "export function f(x) { return x + 1; }\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("all parse clean", out)

    def test_a_bundled_file_that_minifies_into_invalid_js_fails_with_the_asset_path_shown(self):
        _write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {'web.assets_backend': ['mod_a/static/src/js/bad.js']},\n"
            "}\n",
        )
        _write(os.path.join(self.tmp, "mod_a", "static", "src", "js", "bad.js"), "const x = ;\n")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("INVALID JAVASCRIPT", out)
        self.assertIn("mod_a/static/src/js/bad.js", out)
        self.assertIn("web.assets_backend", out)

    def test_an_asset_not_resolvable_in_any_search_root_is_silently_skipped(self):
        _write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {'web.assets_backend': ['mod_a/static/src/js/does_not_exist.js']},\n"
            "}\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("No resolvable bundled JS assets found", out)


if __name__ == "__main__":
    unittest.main()
