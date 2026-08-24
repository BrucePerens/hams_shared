#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for provision.py.

provision() is almost entirely orchestration -- sudo re-exec, apt-get,
systemctl, dropdb, rm -rf, redis-cli -- run for real only as root on a
real Debian/Ubuntu box, per this script's own module docstring ("Must be
run as root"). Actually executing any of that here would be destructive
(--force-reset really does drop a database and wipe a filestore) and
isn't what these tests verify anyway: what matters is that the *decision
logic* around those real commands is correct -- which branch runs, in
what order, and that a genuinely unsupported OS refuses to proceed
rather than plowing ahead. So os.geteuid, os.execvp, subprocess.run, and
every infrastructure.py function provision() calls are mocked; only
provision()'s own control flow runs for real, against a real argv.

Every test patches sys.argv (via unittest.mock.patch.object) rather than
relying on whatever argv this test process itself was started with,
since provision() parses arguments straight from sys.argv via
argparse.parse_known_args() with no way to inject them otherwise.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import provision


class ProvisionTestCase(unittest.TestCase):
    def safe_patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        mock_obj = patcher.start()
        self.addCleanup(patcher.stop)
        return mock_obj

    def safe_patch_object(self, target, attribute, *args, **kwargs):
        patcher = patch.object(target, attribute, *args, **kwargs)
        mock_obj = patcher.start()
        self.addCleanup(patcher.stop)
        return mock_obj

    def safe_patch_dict(self, target, values, clear=False):
        patcher = patch.dict(target, values, clear=clear)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_provision(self, argv, geteuid=0, os_id="debian", patch_open=True):
        """Runs provision.provision() with every real side-effecting call
        mocked and returns (mock_infra, mock_subprocess) for assertions.
        geteuid defaults to 0 (root) since most tests care about behavior
        *after* the sudo re-exec, not the re-exec itself."""
        self.safe_patch_object(os, "geteuid", return_value=geteuid)
        self.safe_patch_object(sys, "argv", ["provision.py"] + argv)
        mock_infra = self.safe_patch_object(provision, "infrastructure", MagicMock())
        mock_infra.get_os_identifier.return_value = os_id
        mock_subprocess = self.safe_patch_object(provision, "subprocess", MagicMock())
        self.safe_patch("os.chdir")
        if patch_open:
            self.safe_patch("builtins.open", MagicMock())
        provision.provision()
        return mock_infra, mock_subprocess


class ProvisionRootElevationTests(ProvisionTestCase):
    def test_re_execs_via_sudo_when_not_root(self):
        mock_execvp = self.safe_patch_object(os, "execvp")
        self.safe_patch_object(os, "geteuid", return_value=1000)
        self.safe_patch_object(sys, "argv", ["provision.py"])
        self.safe_patch_object(provision, "infrastructure", MagicMock())
        self.safe_patch_object(provision, "subprocess", MagicMock())
        self.safe_patch("os.chdir")
        # execvp really does replace the process and never returns in
        # real life; mocked here it returns normally, so provision()
        # continues past it into the unsupported-OS gate (infrastructure
        # is a bare MagicMock here, so get_os_identifier() returns a
        # MagicMock that never equals "debian"/"ubuntu") and exits there
        # -- exactly why the real code must treat everything after the
        # execvp call as "only reached if execvp itself never actually
        # ran." This test only cares that execvp was invoked correctly,
        # not about that downstream exit, but asserts it happens rather
        # than silently discarding it.
        with self.assertRaises(SystemExit):
            provision.provision()
        mock_execvp.assert_called_once()
        args = mock_execvp.call_args[0]
        self.assertEqual(args[0], "sudo")
        self.assertIn("sudo", args[1])
        self.assertIn("-H", args[1])
        self.assertIn("-E", args[1])

    def test_does_not_re_exec_when_already_root(self):
        mock_execvp = self.safe_patch_object(os, "execvp")
        self.run_provision([])
        mock_execvp.assert_not_called()


class ProvisionOsGatingTests(ProvisionTestCase):
    def test_refuses_to_proceed_on_an_unsupported_os(self):
        self.safe_patch_object(os, "geteuid", return_value=0)
        self.safe_patch_object(sys, "argv", ["provision.py"])
        mock_infra = self.safe_patch_object(provision, "infrastructure", MagicMock())
        mock_infra.get_os_identifier.return_value = "fedora"
        mock_subprocess = self.safe_patch_object(provision, "subprocess", MagicMock())
        self.safe_patch("os.chdir")
        with self.assertRaises(SystemExit) as ctx:
            provision.provision()
        self.assertEqual(ctx.exception.code, 1)
        # An unsupported OS must never reach apt-get, dpkg, or
        # provision_environment -- refusing loud beats silently doing
        # Debian-specific things on a system that isn't one.
        mock_subprocess.run.assert_not_called()
        mock_infra.provision_environment.assert_not_called()

    def test_proceeds_on_debian(self):
        mock_infra, _ = self.run_provision([], os_id="debian")
        mock_infra.provision_environment.assert_called_once()

    def test_proceeds_on_ubuntu(self):
        mock_infra, _ = self.run_provision([], os_id="ubuntu", patch_open=False)
        mock_infra.provision_environment.assert_called_once()


class ProvisionDebianDummyPackageTests(ProvisionTestCase):
    def test_builds_and_installs_the_dummy_pypdf2_package_on_debian(self):
        _, mock_subprocess = self.run_provision([], os_id="debian")
        commands_run = [c.args[0] for c in mock_subprocess.run.call_args_list]
        self.assertIn(["apt-get", "update", "-y"], commands_run)
        self.assertIn(["apt-get", "install", "-y", "equivs"], commands_run)
        self.assertTrue(
            any(cmd[:1] == ["equivs-build"] for cmd in commands_run),
            f"expected an equivs-build invocation, got: {commands_run}",
        )
        self.assertTrue(
            any(cmd[:2] == ["dpkg", "-i"] for cmd in commands_run),
            f"expected a dpkg -i invocation, got: {commands_run}",
        )

    def test_skips_the_dummy_pypdf2_package_on_ubuntu(self):
        _, mock_subprocess = self.run_provision([], os_id="ubuntu", patch_open=False)
        commands_run = [c.args[0] for c in mock_subprocess.run.call_args_list]
        self.assertNotIn(["apt-get", "install", "-y", "equivs"], commands_run)


class ProvisionForceResetTests(ProvisionTestCase):
    def test_force_reset_stops_odoo_drops_the_db_wipes_filestore_and_flushes_redis(self):
        _, mock_subprocess = self.run_provision(["--force-reset"], os_id="debian")
        commands_run = [c.args[0] for c in mock_subprocess.run.call_args_list]
        self.assertIn(["systemctl", "stop", "odoo"], commands_run)
        self.assertTrue(
            any(cmd[:3] == ["sudo", "-u", "postgres"] and "dropdb" in cmd for cmd in commands_run),
            f"expected a dropdb invocation, got: {commands_run}",
        )
        self.assertTrue(
            any(cmd[:2] == ["rm", "-rf"] for cmd in commands_run),
            f"expected an rm -rf of the filestore, got: {commands_run}",
        )
        self.assertIn(["redis-cli", "flushall"], commands_run)

    def test_without_force_reset_none_of_the_teardown_commands_run(self):
        _, mock_subprocess = self.run_provision([], os_id="debian")
        commands_run = [c.args[0] for c in mock_subprocess.run.call_args_list]
        self.assertNotIn(["systemctl", "stop", "odoo"], commands_run)
        self.assertNotIn(["redis-cli", "flushall"], commands_run)

    def test_force_reset_uses_db_name_from_env_when_present(self):
        self.safe_patch_dict(os.environ, {"DB_NAME": "my_custom_db"})
        _, mock_subprocess = self.run_provision(["--force-reset"], os_id="debian")
        commands_run = [c.args[0] for c in mock_subprocess.run.call_args_list]
        self.assertTrue(
            any("my_custom_db" in cmd for cmd in commands_run),
            f"expected the real DB_NAME to reach dropdb, got: {commands_run}",
        )


class ProvisionEnvPropagationTests(ProvisionTestCase):
    def test_passes_test_flag_through_to_load_and_prompt_env(self):
        mock_infra, _ = self.run_provision(["--test"], os_id="debian")
        mock_infra.load_and_prompt_env.assert_called_once()
        self.assertTrue(mock_infra.load_and_prompt_env.call_args[0][1])

    def test_provision_environment_receives_repo_root_and_orig_user(self):
        self.safe_patch_dict(os.environ, {"SUDO_USER": "bruce"})
        mock_infra, _ = self.run_provision([], os_id="debian")
        _run_sys, env_vars, orig_user = mock_infra.provision_environment.call_args[0][:3]
        self.assertEqual(orig_user, "bruce")
        self.assertEqual(env_vars["REPO_ROOT"], provision.repo_root)
        self.assertEqual(env_vars["DEBIAN_FRONTEND"], "noninteractive")


if __name__ == "__main__":
    unittest.main()
