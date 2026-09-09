#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_js_function_test_anchors.py (ADR 0090 decision 2's JS sub-track).

Real, non-mocked `node`/`acorn` invocations throughout -- same "use the real tool, don't mock it"
precedent as test_pipeline.py's own real pandoc/chromium calls, since both are already installed
and this is exactly the integration point (`js_function_scan.cjs`'s own subprocess boundary) most
likely to break in a way a mock would hide.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_js_function_test_anchors as cjfta  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _init_git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


class ScanTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_function_declaration_with_no_anchor_is_a_gap(self):
        _write(os.path.join(self.tmp, "foo.js"), "function bar() {\n    return 1;\n}\n")
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertIn("foo.js::bar", gaps)

    def test_a_function_declaration_with_a_real_anchor_is_not_a_gap(self):
        _write(
            os.path.join(self.tmp, "foo.js"),
            "// [@ANCHOR: COMM_bar]\nfunction bar() {\n    return 1;\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertNotIn("foo.js::bar", gaps)

    def test_a_verified_by_citation_with_no_real_base_anchor_is_still_a_gap(self):
        # Same defect as the Python scanner's own real bug (see
        # check_function_test_anchors.py's `_is_base_anchor_declaration`): a bare
        # `ANCHOR_PATTERN.search` over the joined span would count a
        # `// Verified by [@ANCHOR: ...]` citation as a real base declaration,
        # silently exempting the function.
        _write(
            os.path.join(self.tmp, "foo.js"),
            "// Verified by [@ANCHOR: mod:test_bar]\nfunction bar() {\n    return 1;\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertIn("foo.js::bar", gaps)

    def test_a_class_method_gets_a_qualified_class_dot_method_identity(self):
        _write(
            os.path.join(self.tmp, "foo.js"),
            "class Foo {\n    bar() {\n        return 1;\n    }\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertIn("foo.js::Foo.bar", gaps)

    def test_a_nested_closure_is_not_independently_counted(self):
        # Regression test for a real bug found and fixed the same session:
        # the walker originally kept descending into a matched function's
        # own body, counting a private inner helper as its own separate
        # gap -- wrong, since it's not an independently testable unit, the
        # same reason check_function_test_anchors.py's own
        # `_direct_functions` never descends into a Python FunctionDef's
        # body either.
        _write(
            os.path.join(self.tmp, "foo.js"),
            "function outer() {\n"
            "    const helper = () => {\n"
            "        return 1;\n"
            "    };\n"
            "    return helper();\n"
            "}\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertIn("foo.js::outer", gaps)
        self.assertNotIn("foo.js::helper", gaps)

    def test_a_test_dot_js_file_is_excluded_entirely(self):
        _write(os.path.join(self.tmp, "foo.test.js"), "function bar() {\n    return 1;\n}\n")
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_real_tour_registration_file_is_excluded_by_content(self):
        _write(
            os.path.join(self.tmp, "my_tour.js"),
            'import { registry } from "@web/core/registry";\n'
            'registry.category("web_tour.tours").add("my_tour", {\n'
            "    steps() {\n"
            "        return [];\n"
            "    },\n"
            "});\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_same_named_file_with_tour_in_its_name_but_no_registration_is_still_scanned(self):
        # Real, found-live case: ham_onboarding/static/src/js/onboarding_tour_utils.js has
        # "tour" in its filename but is genuine application code (a helper library imported BY
        # tours, not a tour registration itself) -- content, not filename, is what decides.
        _write(
            os.path.join(self.tmp, "onboarding_tour_utils.js"),
            "export function helperUsedByTours() {\n    return 1;\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertIn("onboarding_tour_utils.js::helperUsedByTours", gaps)

    def test_a_file_declared_under_web_assets_tests_in_the_manifest_is_excluded(self):
        # Real, found-live case: zero_sudo/static/src/js/tour_failure_dump.js is loaded as a raw
        # script directly from the "web.assets_tests" bundle -- never `import`ed by anything at
        # all, so an import-graph-based exclusion (this fix's own first, wrong draft) can't see it.
        # The manifest's own "web.assets_tests" bundle is Odoo's real, authoritative "test-only,
        # never a real production page" declaration -- the same category test_*.py/*.test.js/a
        # tour registration itself already get exempted as.
        _write(
            os.path.join(self.tmp, "zero_sudo", "static", "src", "js", "tour_failure_dump.js"),
            "function isBenign(message) {\n    return message.includes('benign');\n}\n",
        )
        _write(
            os.path.join(self.tmp, "zero_sudo", "__manifest__.py"),
            "{\n"
            "    'name': 'Zero Sudo',\n"
            "    'assets': {\n"
            "        'web.assets_tests': [\n"
            "            'zero_sudo/static/src/js/tour_failure_dump.js',\n"
            "        ],\n"
            "    },\n"
            "}\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_file_declared_under_a_real_production_bundle_is_still_scanned(self):
        # The real generalizable check is the "web.assets_tests" bundle specifically -- a file
        # declared under a real production bundle (web.assets_backend/web.assets_frontend) stays
        # in scope even if some other module's manifest also happens to exist.
        _write(
            os.path.join(self.tmp, "zero_sudo", "static", "src", "js", "real_component.js"),
            "function realFeature() {\n    return 1;\n}\n",
        )
        _write(
            os.path.join(self.tmp, "zero_sudo", "__manifest__.py"),
            "{\n"
            "    'name': 'Zero Sudo',\n"
            "    'assets': {\n"
            "        'web.assets_backend': [\n"
            "            'zero_sudo/static/src/js/real_component.js',\n"
            "        ],\n"
            "    },\n"
            "}\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertIn("zero_sudo/static/src/js/real_component.js::realFeature", gaps)

    def test_a_vendored_lib_directory_is_excluded(self):
        _write(
            os.path.join(self.tmp, "static", "lib", "vendor.js"),
            "function thirdParty() {\n    return 1;\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_min_dot_js_file_is_excluded(self):
        _write(os.path.join(self.tmp, "vendor.min.js"), "function thirdParty(){return 1;}\n")
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_syntax_error_file_is_skipped_not_crashed_on(self):
        _write(os.path.join(self.tmp, "broken.js"), "function broken( {\n    return 1\n")
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_begin_end_anchor_counts_as_anchored(self):
        _write(
            os.path.join(self.tmp, "foo.js"),
            "function bar() {\n"
            "    // [@ANCHOR-BEGIN: COMM_bar]\n"
            "    return 1;\n"
            "    // [@ANCHOR-END: COMM_bar]\n"
            "}\n",
        )
        _init_git_repo(self.tmp)
        gaps = cjfta.scan_tree(self.tmp)
        self.assertNotIn("foo.js::bar", gaps)


class BaselineRatchetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.baseline_path = os.path.join(self.tmp, "baseline.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_pre_existing_gap_in_the_baseline_does_not_fail(self):
        repo = os.path.join(self.tmp, "repo")
        _write(os.path.join(repo, "foo.js"), "function bar() {\n    return 1;\n}\n")
        _init_git_repo(repo)
        gaps = cjfta.scan_tree(repo)
        cjfta.save_baseline(self.baseline_path, gaps)

        baseline = cjfta.load_baseline(self.baseline_path)
        current_gaps = cjfta.scan_tree(repo)
        new_gaps = [k for k in current_gaps if k not in baseline]
        self.assertEqual(new_gaps, [])

    def test_a_newly_added_unanchored_function_is_a_real_new_gap(self):
        repo = os.path.join(self.tmp, "repo")
        _write(os.path.join(repo, "foo.js"), "function bar() {\n    return 1;\n}\n")
        _init_git_repo(repo)
        cjfta.save_baseline(self.baseline_path, cjfta.scan_tree(repo))

        _write(
            os.path.join(repo, "foo.js"),
            "function bar() {\n    return 1;\n}\nfunction baz() {\n    return 2;\n}\n",
        )
        baseline = cjfta.load_baseline(self.baseline_path)
        current_gaps = cjfta.scan_tree(repo)
        new_gaps = [k for k in current_gaps if k not in baseline]
        self.assertEqual(new_gaps, ["foo.js::baz"])


if __name__ == "__main__":
    unittest.main()
