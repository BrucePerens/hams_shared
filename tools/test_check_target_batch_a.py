#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Negative controls for batch A of the check_*.py linters that read a repository's Python source and Odoo module layout: each one
must exit 2 (never "Checked 0 files ... Passed") on a missing path, on a plain file and on an empty directory, and must pass on an
empty directory only with HAMS_CHECK_ALLOW_EMPTY=1 (check_target.py; docs/GATE_AUDIT.md).
"""

import json
import os
import subprocess
import tempfile
import unittest

from test_check_target import assert_checker_refuses_a_bad_target, run_checker

# script -> (extra arguments after the target, option naming the target or None)
BATCH_A = {
    "check_absolute_paths.py": ((), None),
    "check_access_csv_group_order.py": ((), None),
    "check_async_lock_scope.py": ((), None),
    "check_cache_invalidation.py": ((), None),
    "check_dead_code.py": ((), None),
    "check_dependency_cycles.py": ((), None),
    "check_fake_network_listener.py": ((), None),
    "check_float_isfinite.py": ((), None),
    "check_function_test_anchors.py": ((), None),
    "check_gdpr_erasure_uses_service_utility.py": ((), None),
    "check_init_imports.py": ((), None),
    "check_int_float_overflow.py": ((), None),
    "check_manifest_dependencies.py": ((), None),
    "check_model_extension_collisions.py": ((), None),
    "check_module_subpackage_imports.py": ((), None),
    "check_rabbitmq_pool.py": ((), None),
    "check_registry_test_cr_usage.py": ((), None),
    "check_res_config_settings_access.py": ((), None),
    "check_self_writeable_field_tests.py": ((), None),
    "check_shebang.py": ((), None),
    "check_test_tags.py": ((), None),
    "check_untyped_utility_files.py": ((), None),
}


class BatchACheckersRefuseABadTargetTests(unittest.TestCase):
    def test_every_checker_in_the_batch_is_red_on_a_missing_path_a_file_and_an_empty_directory(self):
        for script, (extra, flag) in sorted(BATCH_A.items()):
            with self.subTest(script=script):
                assert_checker_refuses_a_bad_target(self, script, extra, target_flag=flag)

    def test_check_anchor_coverage_is_red_on_a_missing_repo_and_on_coverage_data_naming_none_of_its_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = os.path.join(tmp, "coverage.json")
            with open(report, "w", encoding="utf-8") as handle:
                json.dump({"files": {"elsewhere/other.py": {"executed_lines": [1], "missing_lines": []}}}, handle)
            repo = os.path.join(tmp, "repo")
            os.mkdir(repo)
            subprocess.run(["git", "init", "-q", repo], check=True)
            with open(os.path.join(repo, "mine.py"), "w", encoding="utf-8") as handle:
                handle.write("def f():\n    return 1\n")
            subprocess.run(["git", "-C", repo, "add", "mine.py"], check=True)
            args = ("--coverage-json", report)
            done = run_checker("check_anchor_coverage.py", *args, "--repo-root", os.path.join(tmp, "gone"))
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("does not exist", done.stderr)
            done = run_checker("check_anchor_coverage.py", *args, "--repo-root", repo)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("scanned 0", done.stderr)
            self.assertNotIn("Clean", done.stdout)


if __name__ == "__main__":
    unittest.main()
