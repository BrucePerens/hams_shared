#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for pre_flight_check.py.

Unlike list_routes.py, this is pure filesystem inspection -- no live
Odoo registry, no database, no network -- and main()'s two required
CLI args (-m/--module, --addons-path) are fully fixture-addressable,
the same way check_js_syntax.py's main() is. The one hardcoded path is
tier_config.json, resolved relative to __file__ (hams_shared/), so
TierViolationTests copies the script into a fixture's own tools/
subdirectory alongside a fixture tier_config.json, the same technique
used elsewhere in this sweep for a hardcoded-relative-to-__file__ path.
Confirmed empirically first: hams_shared/ currently has no real
tier_config.json at all, so TIERS is always {} and the tier-violation
branch is always skipped in the real repo today -- get_tier() always
returns 99, and `dep_tier > module_tier and module_tier != 99` can
never be true when module_tier is itself 99.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pre_flight_check as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pre_flight_check.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class ParseManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_valid_manifest_dict_literal_is_parsed(self):
        p = os.path.join(self.tmp, "__manifest__.py")
        _write(p, '{"name": "mod_a", "depends": ["base"]}\n')
        self.assertEqual(chk.parse_manifest(p), {"name": "mod_a", "depends": ["base"]})

    def test_a_missing_manifest_file_exits_one_with_a_clear_message(self):
        p = os.path.join(self.tmp, "does_not_exist.py")
        with self.assertRaises(SystemExit) as ctx:
            chk.parse_manifest(p)
        self.assertEqual(ctx.exception.code, 1)

    def test_a_manifest_that_is_not_valid_python_literal_syntax_exits_one(self):
        p = os.path.join(self.tmp, "__manifest__.py")
        _write(p, "{not valid python")
        with self.assertRaises(SystemExit) as ctx:
            chk.parse_manifest(p)
        self.assertEqual(ctx.exception.code, 1)

    def test_a_manifest_containing_a_function_call_is_rejected_by_literal_eval(self):
        # ast.literal_eval deliberately refuses executable expressions,
        # even syntactically valid ones -- a real, load-bearing safety
        # property (a manifest can never execute arbitrary code just by
        # being pre-flight-checked).
        p = os.path.join(self.tmp, "__manifest__.py")
        _write(p, '{"name": open("/etc/passwd").read()}')
        with self.assertRaises(SystemExit) as ctx:
            chk.parse_manifest(p)
        self.assertEqual(ctx.exception.code, 1)


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addons = os.path.join(self.tmp, "addons")
        os.makedirs(self.addons)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _module(self, name, manifest_content):
        mod_dir = os.path.join(self.tmp, name)
        _write(os.path.join(mod_dir, "__manifest__.py"), manifest_content)
        return mod_dir

    def _addon(self, name):
        _write(os.path.join(self.addons, name, "__manifest__.py"), '{"name": "%s"}' % name)

    def _run(self, module_dir, addons_path=None):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "-m",
                module_dir,
                "--addons-path",
                addons_path if addons_path is not None else self.addons,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_module_with_no_depends_key_passes_without_touching_addons_paths(self):
        mod = self._module("mod_a", '{"name": "mod_a"}')
        code, out = self._run(mod)
        self.assertEqual(code, 0, out)

    def test_a_dependency_present_in_the_addons_path_passes(self):
        self._addon("dep_ok")
        mod = self._module("mod_a", '{"depends": ["dep_ok"]}')
        code, out = self._run(mod)
        self.assertEqual(code, 0, out)

    def test_a_missing_dependency_fails_and_names_it(self):
        mod = self._module("mod_a", '{"depends": ["dep_missing"]}')
        code, out = self._run(mod)
        self.assertEqual(code, 1)
        self.assertIn("PRE-FLIGHT CHECK FAILED", out)
        self.assertIn("dep_missing", out)

    def test_a_core_module_dependency_is_exempted_even_when_absent_from_addons_paths(self):
        mod = self._module("mod_a", '{"depends": ["base", "web", "mail"]}')
        code, out = self._run(mod)
        self.assertEqual(code, 0, out)

    def test_a_directory_that_looks_like_a_dependency_but_has_no_manifest_does_not_count(self):
        os.makedirs(os.path.join(self.addons, "dep_shell"))
        mod = self._module("mod_a", '{"depends": ["dep_shell"]}')
        code, out = self._run(mod)
        self.assertEqual(code, 1)
        self.assertIn("dep_shell", out)

    def test_multiple_addons_paths_are_all_searched(self):
        second_addons = os.path.join(self.tmp, "addons2")
        _write(os.path.join(second_addons, "dep_elsewhere", "__manifest__.py"), '{"name": "dep_elsewhere"}')
        mod = self._module("mod_a", '{"depends": ["dep_elsewhere"]}')
        code, out = self._run(mod, addons_path=f"{self.addons},{second_addons}")
        self.assertEqual(code, 0, out)

    def test_a_missing_module_manifest_exits_one(self):
        code, out = self._run(os.path.join(self.tmp, "does_not_exist"))
        self.assertEqual(code, 1)
        self.assertIn("Manifest file not found", out)


class TierViolationTests(unittest.TestCase):
    """tier_config.json is resolved relative to __file__, one directory
    above tools/, so the script and its parent dir are copied into a
    fixture to make that path fixture-addressable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tools_dir = os.path.join(self.tmp, "tools")
        os.makedirs(self.tools_dir)
        shutil.copy(_SCRIPT, os.path.join(self.tools_dir, "pre_flight_check.py"))
        self.addons = os.path.join(self.tmp, "addons")
        os.makedirs(self.addons)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tier_config(self, content):
        _write(os.path.join(self.tmp, "tier_config.json"), content)

    def _module(self, name, manifest_content):
        mod_dir = os.path.join(self.tmp, name)
        _write(os.path.join(mod_dir, "__manifest__.py"), manifest_content)
        return mod_dir

    def _addon(self, name):
        _write(os.path.join(self.addons, name, "__manifest__.py"), '{"name": "%s"}' % name)

    def _run(self, module_dir):
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(self.tools_dir, "pre_flight_check.py"),
                "-m",
                module_dir,
                "--addons-path",
                self.addons,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_lower_tier_module_depending_on_a_higher_tier_module_is_flagged(self):
        self._tier_config('{"1": ["mod_low"], "2": ["dep_high"]}')
        self._addon("dep_high")
        mod = self._module("mod_low", '{"depends": ["dep_high"]}')
        code, out = self._run(mod)
        self.assertEqual(code, 1)
        self.assertIn("ARCHITECTURE VIOLATION", out)
        self.assertIn("Tier 1", out)
        self.assertIn("dep_high", out)

    def test_a_same_or_lower_tier_dependency_is_not_a_violation(self):
        self._tier_config('{"1": ["dep_low"], "2": ["mod_high"]}')
        self._addon("dep_low")
        mod = self._module("mod_high", '{"depends": ["dep_low"]}')
        code, out = self._run(mod)
        self.assertEqual(code, 0, out)

    def test_an_untiered_module_is_treated_as_tier_99_and_never_flagged(self):
        # get_tier() falls back to 99 for anything not listed, and the
        # violation check explicitly excludes module_tier == 99 --
        # confirmed real behavior, not assumed.
        self._tier_config('{"1": ["dep_high_named_elsewhere"]}')
        self._addon("dep_high_named_elsewhere")
        mod = self._module("mod_untiered", '{"depends": ["dep_high_named_elsewhere"]}')
        code, out = self._run(mod)
        self.assertEqual(code, 0, out)

    def test_no_tier_config_file_present_skips_tier_checking_entirely(self):
        # Matches the real repo's current state: no tier_config.json
        # exists in hams_shared/ at all.
        self._addon("dep_high")
        mod = self._module("mod_low", '{"depends": ["dep_high"]}')
        code, out = self._run(mod)
        self.assertEqual(code, 0, out)
        self.assertNotIn("ARCHITECTURE VIOLATION", out)


if __name__ == "__main__":
    unittest.main()
