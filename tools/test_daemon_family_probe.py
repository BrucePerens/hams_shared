# Copyright © Bruce Perens K6BP. All Rights Reserved.
import unittest
from unittest import mock

import daemon_family_probe as probe
import infrastructure as infra


def _unit(name):
    for entry in infra.MANIFEST["static_files"]:
        if entry["path"].endswith("/" + name) and "prod" in entry["environments"]:
            return entry["content"]
    raise KeyError(name)


class DaemonFamilyProbeTests(unittest.TestCase):
    """Tests [@ANCHOR: daemon_family_probe:test]"""

    def test_the_transient_unit_carries_the_real_units_account_environment_and_sandbox(self):
        pairs = dict(probe.properties(_unit("sota.sync.service")))
        self.assertEqual(pairs["User"], "hamsd_activator_sync")
        self.assertEqual(pairs["Group"], "hamsd_activator_sync")
        self.assertEqual(pairs["EnvironmentFile"], "/opt/hams/etc/common.env")
        self.assertEqual(pairs["ProtectSystem"], "strict")
        self.assertEqual(pairs["ProtectProc"], "invisible")
        # One -p ReadWritePaths= per line in the unit; systemd appends them, so the transient unit has the union.
        writable = " ".join(v for k, v in probe.properties(_unit("sota.sync.service")) if k == "ReadWritePaths").split()
        self.assertEqual(writable[:2], ["/opt/hams/spool/sota_sync", "/opt/hams/downloads/sota_sync"])

    def test_environment_assignments_are_split_and_unquoted(self):
        env = [v for k, v in probe.properties(_unit("sota.sync.service")) if k == "Environment"]
        self.assertIn("ODOO_KEY_FILE=/opt/hams/etc/keys/activator_sync/activator_data_service_internal.key", env)
        self.assertIn("PYTHONPATH=/opt/hams/daemons", env)

    def test_the_daemon_itself_is_never_started(self):
        argv = probe.command(_unit("ncvec.sync.service"))
        self.assertEqual(argv[:4], ["systemd-run", "--wait", "--pipe", "--quiet"])
        self.assertEqual(argv[-3:-1], ["/usr/bin/python3", "-c"])
        joined = " ".join(argv[:-1])
        self.assertNotIn("main.py", joined)
        self.assertNotIn("ExecStart", joined)

    def test_every_family_unit_yields_a_probe_command(self):
        for unit in infra.FAMILY_ACCOUNT_UNITS:
            with self.subTest(unit=unit):
                text = _unit(unit)
                argv = probe.command(text)
                users = [a for a in argv if a.startswith("User=")]
                self.assertEqual(len(users), 1)

    def test_a_unit_group_grant_reaches_the_probe_so_it_runs_with_the_groups_the_daemon_has(self):
        argv = probe.command(_unit("callbook.dns.export.service"))
        self.assertIn("SupplementaryGroups=pdns", argv)
        self.assertIn("User=hamsd_dns_export", argv)

    def test_an_environment_value_with_spaces_stays_one_assignment(self):
        self.assertEqual(
            probe._split_environment('"A=1 2" "B=3"'),
            ["A=1 2", "B=3"],
        )

    def test_a_pass_needs_exit_zero_and_the_ok_line(self):
        ok = mock.Mock(returncode=0, stdout="PROBE-OK authenticated as uid 5 using /x\n", stderr="")
        bad = mock.Mock(returncode=1, stdout="", stderr="PermissionError: denied\n")
        with mock.patch.object(probe, "read_unit", return_value=_unit("ncvec.sync.service")):
            with mock.patch.object(probe.subprocess, "run", return_value=ok):
                self.assertEqual(probe.probe("ncvec.sync.service")[0], True)
            with mock.patch.object(probe.subprocess, "run", return_value=bad):
                self.assertEqual(probe.probe("ncvec.sync.service"), (False, "PermissionError: denied"))

    def test_a_unit_that_is_not_installed_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            probe.read_unit("no.such.unit.service")

    def test_not_root_is_refused(self):
        with mock.patch.object(probe.os, "geteuid", return_value=1000):
            self.assertEqual(probe.main(["ncvec.sync.service"]), 2)


if __name__ == "__main__":
    unittest.main()
