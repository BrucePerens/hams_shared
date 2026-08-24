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
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import provision  # noqa: E402


def _patched_provision(argv, geteuid=0, os_id="debian"):
    """Runs provision.provision() with every real side-effecting call
    mocked, and returns the mocks so a test can assert on them. geteuid
    defaults to 0 (root) since most tests care about behavior *after*
    the sudo re-exec, not the re-exec itself (covered by its own test
    below)."""
    return patch.multiple(
        provision,
        subprocess=MagicMock(wraps=subprocess),
        infrastructure=MagicMock(),
    ), patch.object(sys, "argv", ["provision.py"] + argv), patch.object(os, "geteuid", return_value=geteuid), patch.object(os, "execvp")


class ProvisionRootElevationTests(unittest.TestCase):
    def test_re_execs_via_sudo_when_not_root(self):
        with patch.object(os, "geteuid", return_value=1000), \
             patch.object(os, "execvp") as mock_execvp, \
             patch.object(sys, "argv", ["provision.py"]), \
             patch.object(provision, "infrastructure", MagicMock()):
            # execvp really does replace the process and never returns in
            # real life; mocked here it returns normally, so provision()
            # continues past it -- exactly why the real code must treat
            # everything after this call as "only reached if execvp
            # itself never actually ran," which this test doesn't need
            # to assert further than "execvp was called with sudo."
            with patch.object(provision, "subprocess", MagicMock()), \
                 patch("os.chdir"):
                try:
                    provision.provision()
                except SystemExit:
                    pass
        mock_execvp.assert_called_once()
        args = mock_execvp.call_args[0]
        self.assertEqual(args[0], "sudo")
        self.assertIn("sudo", args[1])
        self.assertIn("-H", args[1])
        self.assertIn("-E", args[1])

    def test_does_not_re_exec_when_already_root(self):
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(os, "execvp") as mock_execvp, \
             patch.object(sys, "argv", ["provision.py"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()), \
             patch("os.chdir"), patch("builtins.open", MagicMock()):
            mock_infra.get_os_identifier.return_value = "debian"
            provision.provision()
        mock_execvp.assert_not_called()


class ProvisionOsGatingTests(unittest.TestCase):
    def test_refuses_to_proceed_on_an_unsupported_os(self):
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()) as mock_subprocess, \
             patch("os.chdir"):
            mock_infra.get_os_identifier.return_value = "fedora"
            with self.assertRaises(SystemExit) as ctx:
                provision.provision()
            self.assertEqual(ctx.exception.code, 1)
        # An unsupported OS must never reach apt-get, dpkg, or
        # provision_environment -- refusing loud beats silently doing
        # Debian-specific things on a system that isn't one.
        mock_subprocess.run.assert_not_called()
        mock_infra.provision_environment.assert_not_called()

    def test_proceeds_on_debian(self):
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()) as mock_subprocess, \
             patch("os.chdir"), patch("builtins.open", MagicMock()):
            mock_infra.get_os_identifier.return_value = "debian"
            provision.provision()
        mock_infra.provision_environment.assert_called_once()

    def test_proceeds_on_ubuntu(self):
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()), \
             patch("os.chdir"):
            mock_infra.get_os_identifier.return_value = "ubuntu"
            provision.provision()
        mock_infra.provision_environment.assert_called_once()


class ProvisionDebianDummyPackageTests(unittest.TestCase):
    def test_builds_and_installs_the_dummy_pypdf2_package_on_debian(self):
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()) as mock_subprocess, \
             patch("os.chdir"), patch("builtins.open", MagicMock()):
            mock_infra.get_os_identifier.return_value = "debian"
            provision.provision()
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
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()) as mock_subprocess, \
             patch("os.chdir"):
            mock_infra.get_os_identifier.return_value = "ubuntu"
            provision.provision()
        commands_run = [c.args[0] for c in mock_subprocess.run.call_args_list]
        self.assertNotIn(["apt-get", "install", "-y", "equivs"], commands_run)


class ProvisionForceResetTests(unittest.TestCase):
    def test_force_reset_stops_odoo_drops_the_db_wipes_filestore_and_flushes_redis(self):
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py", "--force-reset"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()) as mock_subprocess, \
             patch("os.chdir"), patch("builtins.open", MagicMock()):
            mock_infra.get_os_identifier.return_value = "debian"
            provision.provision()
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
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()) as mock_subprocess, \
             patch("os.chdir"), patch("builtins.open", MagicMock()):
            mock_infra.get_os_identifier.return_value = "debian"
            provision.provision()
        commands_run = [c.args[0] for c in mock_subprocess.run.call_args_list]
        self.assertNotIn(["systemctl", "stop", "odoo"], commands_run)
        self.assertNotIn(["redis-cli", "flushall"], commands_run)

    def test_force_reset_uses_db_name_from_env_when_present(self):
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py", "--force-reset"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()) as mock_subprocess, \
             patch("os.chdir"), patch("builtins.open", MagicMock()), \
             patch.dict(os.environ, {"DB_NAME": "my_custom_db"}):
            mock_infra.get_os_identifier.return_value = "debian"
            provision.provision()
        commands_run = [c.args[0] for c in mock_subprocess.run.call_args_list]
        self.assertTrue(
            any("my_custom_db" in cmd for cmd in commands_run),
            f"expected the real DB_NAME to reach dropdb, got: {commands_run}",
        )


class ProvisionEnvPropagationTests(unittest.TestCase):
    def test_passes_test_flag_through_to_load_and_prompt_env(self):
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py", "--test"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()), \
             patch("os.chdir"), patch("builtins.open", MagicMock()):
            mock_infra.get_os_identifier.return_value = "debian"
            provision.provision()
        mock_infra.load_and_prompt_env.assert_called_once()
        self.assertTrue(mock_infra.load_and_prompt_env.call_args[0][1])

    def test_provision_environment_receives_repo_root_and_orig_user(self):
        with patch.object(os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["provision.py"]), \
             patch.object(provision, "infrastructure", MagicMock()) as mock_infra, \
             patch.object(provision, "subprocess", MagicMock()), \
             patch("os.chdir"), patch("builtins.open", MagicMock()), \
             patch.dict(os.environ, {"SUDO_USER": "bruce"}, clear=False):
            mock_infra.get_os_identifier.return_value = "debian"
            provision.provision()
        _run_sys, env_vars, orig_user = mock_infra.provision_environment.call_args[0][:3]
        self.assertEqual(orig_user, "bruce")
        self.assertEqual(env_vars["REPO_ROOT"], provision.repo_root)
        self.assertEqual(env_vars["DEBIAN_FRONTEND"], "noninteractive")


if __name__ == "__main__":
    unittest.main()
