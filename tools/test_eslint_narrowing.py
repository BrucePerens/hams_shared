#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for run_linters.narrow_eslint_targets: a --changed run lints the changed scripts and the files that import
them, and falls back to the whole directories whenever narrowing is not provably safe."""

import os
import tempfile
import unittest

import run_linters


class NarrowEslintTargetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def narrow(self, changed, **kwargs):
        return run_linters.narrow_eslint_targets([self.root], changed, **kwargs)

    def test_changed_file_and_its_importers_only(self):
        changed = self.write("m/static/src/util.js", "export function f() { return 1; }\n")
        importer = self.write("m/static/src/user.js", 'import { f } from "./util";\nf();\n')
        importer_alias = self.write("n/static/src/other.js", "import {f} from '@m/src/util.js';\n")
        self.write("m/static/src/unrelated.js", 'import { g } from "./elsewhere";\n')
        self.write("m/static/src/mentions.js", "// util is only named in a comment\nexport const x = 1;\n")
        self.assertEqual(self.narrow([changed]), sorted([changed, importer, importer_alias]))

    def test_index_file_is_imported_by_its_directory_name(self):
        changed = self.write("m/widgets/index.js", "export const w = 1;\n")
        importer = self.write("m/app.js", 'import { w } from "./widgets";\n')
        self.assertEqual(self.narrow([changed]), sorted([changed, importer]))

    def test_require_is_an_import_too(self):
        changed = self.write("d/lib.js", "module.exports = { a: 1 };\n")
        importer = self.write("d/main.js", "const lib = require('./lib');\n")
        self.assertEqual(self.narrow([changed]), sorted([changed, importer]))

    def test_classic_script_means_the_whole_directories(self):
        changed = self.write("m/static/src/legacy.js", "var shared = 1;\nfunction helper() {}\n")
        self.write("m/static/src/other.js", "helper();\n")
        self.assertIsNone(self.narrow([changed]))

    def test_config_change_means_the_whole_directories(self):
        changed = self.write("m/a.js", "export const a = 1;\n")
        config = os.path.join(self.root, "eslint.config.js")
        self.assertIsNone(self.narrow([changed, config], full_triggers=[config]))

    def test_json_or_no_script_change_means_the_whole_directories(self):
        script = self.write("m/a.js", "export const a = 1;\n")
        data = self.write("m/data.json", "{}\\n")
        self.assertIsNone(self.narrow([script, data]))
        self.assertIsNone(self.narrow([]))

    def test_deleted_script_still_lints_its_importers(self):
        importer = self.write("m/user.js", 'import { f } from "./gone";\n')
        gone = os.path.join(self.root, "m", "gone.js")  # deleted: not on disk
        self.assertEqual(self.narrow([gone]), [importer])

    def test_files_outside_the_targets_are_ignored(self):
        with tempfile.TemporaryDirectory() as other:
            outside = os.path.join(other, "x.js")
            open(outside, "w").write("export const x = 1;\n")
            self.assertIsNone(self.narrow([outside]))

    def test_node_modules_and_target_are_not_scanned(self):
        changed = self.write("m/util.js", "export const f = 1;\n")
        self.write("m/node_modules/pkg/index.js", 'import { f } from "../../util";\n')
        self.write("m/target/doc/a.js", 'import { f } from "../util";\n')
        self.assertEqual(self.narrow([changed]), [changed])


if __name__ == "__main__":
    unittest.main()
