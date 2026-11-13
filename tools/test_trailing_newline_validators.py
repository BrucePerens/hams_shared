#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Validators that end in `\\Z`, not `$`.

In Python `$` also matches just before a final newline, so a `^...$` pattern used with `.match()` lets
exactly one trailing newline through. These patterns guard names that reach SQL, paths, tenant specs,
backup directories and a command line, so each must refuse "<good value>\\n" and still accept the good
value. Tests the patterns in b2_backup, tenant_lib, odoo_snapshot_source, provision, test.py, benchmark_template_db,
rmsgw_callsign_handoff_wrapper and mirror_relay_tool_releases.
"""

import copy
import importlib.util
import os
import unittest

import b2_backup
import mirror_relay_tool_releases as mirror
import odoo_snapshot_source
import tenant_lib as lib
from test_tenant_lib import PERENS

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    # test.py is loaded by path: `import test` would resolve to the standard library's package.
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTrailingNewlineRefused(unittest.TestCase):
    def _pairs(self, regex, good):
        self.assertTrue(regex.match(good), f"{regex.pattern} must accept {good!r}")
        self.assertIsNone(regex.match(good + "\n"), f"{regex.pattern} must refuse {good!r} plus a newline")

    def test_b2_backup_patterns(self):
        self._pairs(b2_backup.NAME_RE, "pgdata")
        self._pairs(b2_backup.SHA1_NAME_RE, "a" * 40)
        self._pairs(b2_backup.ARCHIVE_SET_RE, "20261104T010203Z")

    def test_tenant_lib_patterns(self):
        self._pairs(lib.NAME_RE, "perens")
        self._pairs(lib.MODULE_RE, "ham_base")
        self._pairs(lib.LABEL_RE, "perens")
        self._pairs(lib.SIZE_RE, "512M")
        self._pairs(lib.STATIC_PATH_RE, "/srv/site/")

    def test_tenant_spec_values_refuse_a_trailing_newline(self):
        lib.validate_spec(copy.deepcopy(PERENS))
        for key, value in (("cpu_quota", "200%\n"), ("admin_login", "admin\n"), ("language", "en_US\n"), ("name", "perens\n")):
            with self.subTest(key=key), self.assertRaises(lib.SpecError):
                lib.validate_spec(dict(copy.deepcopy(PERENS), **{key: value}))

    def test_odoo_snapshot_source_patterns(self):
        self._pairs(odoo_snapshot_source.IDENT_RE, "res_partner")
        self._pairs(odoo_snapshot_source.ORDER_RE, "id desc")

    def test_test_runner_database_name_pattern(self):
        runner = _load("_hams_test_runner_for_newline_check", "test.py")
        self._pairs(runner._SAFE_DB_NAME_RE, "hams_test")

    def test_template_benchmark_identifier_pattern(self):
        benchmark = _load("_hams_template_benchmark_for_newline_check", "benchmark_template_db.py")
        self._pairs(benchmark.SAFE_IDENTIFIER_RE, "hams_tpl_ab12")
        with self.assertRaises(ValueError):
            benchmark.check_identifier("hams_tpl_ab12\n")

    def test_rmsgw_callsign_pattern(self):
        wrapper = _load("_hams_rmsgw_wrapper_for_newline_check", "rmsgw_callsign_handoff_wrapper.py")
        self._pairs(wrapper._CALLSIGN_RE, "K6BP-10")

    def test_relay_tool_asset_patterns(self):
        for (_tool, _platform), pattern in mirror.ASSET_PATTERNS.items():
            name = {"windows": "pat_1.0_windows_i386.zip", "macos": "pat_1.0_darwin_amd64.pkg",
                    "linux": "pat_1.0_linux_amd64.tar.gz"}[_platform]
            self._pairs(pattern, name)


if __name__ == "__main__":
    unittest.main()

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: odoo_snapshot_source:odoo_snapshot_source_pg_db_class]
