#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for list_routes.py.

Almost everything past its config-existence check requires a live,
bootstrapped Odoo registry against a real Postgres database -- the
script's own default --database value is literally "hams_prod". This
codebase treats that class of side-effecting, real-infrastructure code
(infrastructure.py, provision.py) as too high blast-radius for
unsupervised test-writing: an unmocked odoo.modules.registry.Registry(...)
call would attempt a real database connection, and `odoo` is installed
in this environment, so nothing short-circuits it. Confirmed via a real
subprocess run first: the config-existence check at the very top of
main() is the only genuinely safe, offline, no-Odoo-import boundary --
it exits before `import odoo` is ever reached. That's what
MainIntegrationTests exercises for real. The argparse defaults
("-d hams_prod", "-c /opt/hams/etc/odoo.conf") are locked in via a
source-text assertion instead of by ever running main() past the
config check, so a silent change to the default database name doesn't
go unnoticed without ever risking a real registry bootstrap.
"""

import os
import re
import subprocess
import sys
import unittest

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "list_routes.py")


class MainIntegrationTests(unittest.TestCase):
    def test_a_missing_config_file_exits_one_with_a_clear_message_before_importing_odoo(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, "-c", "/does/not/exist.conf"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Configuration file not found at /does/not/exist.conf", result.stdout)

    def test_help_output_documents_both_flags_without_touching_odoo(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--database", result.stdout)
        self.assertIn("--config", result.stdout)


class ArgparseDefaultsSourceTests(unittest.TestCase):
    """Locks in the two default flag values by reading the real source
    text directly, deliberately never executing main() past the
    config-existence guard (see module docstring for why)."""

    def setUp(self):
        with open(_SCRIPT, encoding="utf-8") as f:
            self.source = f.read()

    def test_the_default_database_name_is_still_hams_prod(self):
        match = re.search(r'"--database"[^)]*default="([^"]+)"', self.source, re.DOTALL)
        self.assertIsNotNone(match, "Could not find the --database default in list_routes.py")
        self.assertEqual(match.group(1), "hams_prod")

    def test_the_default_config_path_is_still_the_documented_etc_location(self):
        match = re.search(r'"--config"[^)]*default="([^"]+)"', self.source, re.DOTALL)
        self.assertIsNotNone(match, "Could not find the --config default in list_routes.py")
        self.assertEqual(match.group(1), "/opt/hams/etc/odoo.conf")


if __name__ == "__main__":
    unittest.main()
