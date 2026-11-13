#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for runner_slots.py, the per-slot derivations behind test.py's
concurrent test slots (ADR 0116).

The default-equals-before tests pin the promise that a machine that
has not opted in sees exactly the values test.py used before slots
existed. The Chrome pattern test runs the real ``pgrep -f`` (the same
regular-expression matcher ``pkill -f`` uses) against real processes.

Tests [@ANCHOR: runner_slots:parse_slot_count]
Tests [@ANCHOR: runner_slots:configured_slot_count]
Tests [@ANCHOR: runner_slots:slot_lock_path]
Tests [@ANCHOR: runner_slots:slot_database_name]
Tests [@ANCHOR: runner_slots:slot_file_name]
Tests [@ANCHOR: runner_slots:in_private_network]
Tests [@ANCHOR: runner_slots:SlotResources]
Tests [@ANCHOR: runner_slots:slot_resources]
Tests [@ANCHOR: runner_slots:broker_listening]
Tests [@ANCHOR: runner_slots:modules_mentioning_broker]
Tests [@ANCHOR: runner_slots:slot_refusals]
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import runner_slots as rs  # noqa: E402


class TestParseSlotCount(unittest.TestCase):
    def test_whole_numbers_in_range(self):
        self.assertEqual(rs.parse_slot_count("1", "x"), 1)
        self.assertEqual(rs.parse_slot_count(" 4\n", "x"), 4)
        self.assertEqual(rs.parse_slot_count(rs.MAX_SLOTS, "x"), 15)

    def test_everything_else_is_an_error_naming_the_source(self):
        for bad in ("", "0", "16", "-1", "2.5", "two", "1e1", "０"):
            with self.subTest(bad=bad):
                with self.assertRaises(rs.SlotConfigError) as ctx:
                    rs.parse_slot_count(bad, "HAMS_TEST_SLOTS")
                self.assertIn("HAMS_TEST_SLOTS", str(ctx.exception))


class TestConfiguredSlotCount(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test_slots")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_no_opt_in_is_one_slot(self):
        self.assertEqual(
            rs.configured_slot_count(None, {}, self.path), 1
        )

    def test_the_file_opts_a_machine_in(self):
        self.write("4\n")
        self.assertEqual(
            rs.configured_slot_count(None, {}, self.path), 4
        )

    def test_environment_then_file_and_the_flag_can_only_raise_the_count(self):
        self.write("4\n")
        env = {rs.SLOTS_ENV: "3"}
        self.assertEqual(
            rs.configured_slot_count(2, env, self.path), 3
        )
        self.assertEqual(rs.configured_slot_count(6, env, self.path), 6)
        self.assertEqual(
            rs.configured_slot_count(None, env, self.path), 3
        )
        self.assertEqual(
            rs.configured_slot_count(
                None, {rs.SLOTS_ENV: ""}, self.path
            ),
            4,
        )

    def test_a_bad_value_anywhere_is_an_error_not_one_slot(self):
        self.write("lots\n")
        with self.assertRaises(rs.SlotConfigError):
            rs.configured_slot_count(None, {}, self.path)
        with self.assertRaises(rs.SlotConfigError):
            rs.configured_slot_count(
                None, {rs.SLOTS_ENV: "99"}, self.path
            )
        with self.assertRaises(rs.SlotConfigError):
            rs.configured_slot_count("0", {}, self.path)

    def test_an_unreadable_file_is_an_error_not_one_slot(self):
        os.mkdir(self.path)  # exists, cannot be read as a file
        with self.assertRaises(OSError):
            rs.configured_slot_count(None, {}, self.path)

    def test_the_default_path_is_the_documented_host_file(self):
        self.assertEqual(rs.SLOTS_FILE, "/opt/hams/etc/test_slots")


class TestSlotNames(unittest.TestCase):
    def test_slot_one_is_what_it_always_was(self):
        base = "/tmp/hams_odoo_test_runner.lock"
        self.assertEqual(rs.slot_lock_path(base, 1), base)
        self.assertEqual(
            rs.slot_database_name("hams_test", 1), "hams_test"
        )
        self.assertEqual(
            rs.slot_file_name("filtered_test.txt", 1),
            "filtered_test.txt",
        )
        self.assertEqual(rs.slot_file_name("log", 1), "log")
        self.assertEqual(rs.slot_suffix(1), "")

    def test_higher_slots_are_distinct_per_slot(self):
        base = "/tmp/hams_odoo_test_runner.lock"
        self.assertEqual(rs.slot_lock_path(base, 3), base + ".slot-3")
        self.assertEqual(
            rs.slot_database_name("hams_test_relay", 2),
            "hams_test_relay_s2",
        )
        self.assertEqual(
            rs.slot_file_name("filtered_test.txt", 2),
            "filtered_test.slot-2.txt",
        )
        self.assertEqual(
            rs.slot_file_name("coverage_report", 2),
            "coverage_report.slot-2",
        )
        self.assertEqual(rs.slot_file_name("log", 15), "log.slot-15")

    def test_no_two_slots_share_any_name(self):
        for build in (
            lambda slot: rs.slot_lock_path("/l", slot),
            lambda slot: rs.slot_database_name("db", slot),
            lambda slot: rs.slot_file_name("test_progress.txt", slot),
            lambda slot: rs.slot_file_name("offline_ns", slot),
        ):
            names = [
                build(slot) for slot in range(1, rs.MAX_SLOTS + 1)
            ]
            self.assertEqual(len(names), len(set(names)), names)

    def test_a_name_over_the_postgres_limit_is_refused(self):
        long_name = "d" * 61
        self.assertEqual(
            rs.slot_database_name(long_name, 1), long_name
        )
        with self.assertRaises(rs.SlotConfigError):
            rs.slot_database_name(long_name, 2)


class TestPrivateNetwork(unittest.TestCase):
    def test_only_the_final_process_of_the_full_pipeline_is_private(
        self,
    ):
        both = {"HAMS_ISOLATED_NS": "1", "HAMS_TEST_LOCK_HELD": "1"}
        self.assertTrue(rs.in_private_network(both))
        self.assertFalse(
            rs.in_private_network({"HAMS_ISOLATED_NS": "1"})
        )
        self.assertFalse(
            rs.in_private_network({"HAMS_TEST_LOCK_HELD": "1"})
        )
        self.assertFalse(rs.in_private_network({}))


class TestSlotResources(unittest.TestCase):
    TMP = "/var/lib/odoo/tmp"

    def test_one_slot_equals_what_test_py_used_before_slots(self):
        for private in (True, False):
            r = rs.slot_resources(1, 1, private, self.TMP)
            self.assertEqual(r.http_port, 8075)
            self.assertEqual(r.redis_db, 1)
            self.assertEqual(r.tmpdir, self.TMP)
            self.assertFalse(r.scoped)
            self.assertEqual(r.chrome_kill_pattern, "_chrome_odoo")
            self.assertEqual(
                r.chrome_profile_globs,
                ("/tmp/*_chrome_odoo", "/var/tmp/*_chrome_odoo"),
            )
        self.assertIsNone(rs.slot_resources(1, 1, False, None).tmpdir)

    def test_private_network_needs_nothing_per_slot(self):
        for slot in (1, 2, 3):
            r = rs.slot_resources(slot, 3, True, self.TMP)
            self.assertFalse(r.scoped)
            self.assertEqual((r.http_port, r.redis_db), (8075, 1))
            self.assertEqual(r.tmpdir, self.TMP)
            self.assertEqual(r.chrome_kill_pattern, "_chrome_odoo")

    def test_a_shared_host_gets_a_port_redis_db_and_tmpdir_per_slot(
        self,
    ):
        seen = set()
        for slot in (1, 2, 3):
            r = rs.slot_resources(slot, 3, False, self.TMP)
            self.assertTrue(r.scoped)
            self.assertEqual(r.http_port, 8075 + slot - 1)
            self.assertEqual(r.redis_db, slot)
            seen.add((r.http_port, r.redis_db, r.tmpdir))
        self.assertEqual(len(seen), 3)
        self.assertEqual(
            rs.slot_resources(2, 3, False, self.TMP + "/").tmpdir,
            self.TMP + "/slot-2",
        )
        self.assertEqual(
            rs.slot_resources(1, 3, False, self.TMP).tmpdir, self.TMP
        )

    def test_scoped_run_needs_a_plain_tmpdir(self):
        for bad in (None, "", "relative/dir", "/tmp/a b", "/tmp/a'b"):
            with self.subTest(bad=bad):
                with self.assertRaises(rs.SlotConfigError):
                    rs.slot_resources(2, 3, False, bad)

    def test_a_slot_outside_the_count_is_refused(self):
        for slot in (0, 4):
            with self.assertRaises(rs.SlotConfigError):
                rs.slot_resources(slot, 3, False, self.TMP)

    def _fake_chrome(self, directory):
        """A real process whose command line names a Chrome profile."""
        profile = os.path.join(directory, "tmpabc123_chrome_odoo")
        script = (
            "exec -a 'chrome --user-data-dir=%s' sleep 60" % profile
        )
        proc = subprocess.Popen(["bash", "-c", script])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def _matching(self, pattern):
        out = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", pattern],
            capture_output=True,
            text=True,
        )
        return {int(pid) for pid in out.stdout.split()}

    def test_each_slots_pattern_finds_its_own_chrome_and_no_other(
        self,
    ):
        base = "/var/lib/odoo/tmp"
        procs = {
            slot: self._fake_chrome(
                base if slot == 1 else f"{base}/slot-{slot}"
            )
            for slot in (1, 2, 3)
        }
        patterns = {
            slot: rs.slot_resources(
                slot, 3, False, base
            ).chrome_kill_pattern
            for slot in procs
        }
        for _ in range(100):
            if all(self._matching(patterns[s]) for s in procs):
                break
            time.sleep(0.05)
        for slot, proc in procs.items():
            with self.subTest(slot=slot):
                self.assertEqual(
                    self._matching(patterns[slot]), {proc.pid}
                )

    def test_the_unscoped_pattern_still_finds_every_chrome(self):
        proc = self._fake_chrome("/tmp")
        for _ in range(100):
            if proc.pid in self._matching("_chrome_odoo"):
                break
            time.sleep(0.05)
        self.assertIn(
            proc.pid,
            self._matching(
                rs.slot_resources(
                    1, 1, False, None
                ).chrome_kill_pattern
            ),
        )


class TestBroker(unittest.TestCase):
    def test_listening_and_not_listening(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.assertTrue(rs.broker_listening(port=port))
        listener.close()
        self.assertFalse(rs.broker_listening(port=port))


class TestModulesMentioningBroker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def module(self, name, files):
        directory = os.path.join(self.root, name)
        for relative, text in files.items():
            path = os.path.join(directory, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        return directory

    def test_only_modules_whose_python_mentions_the_broker_are_named(
        self,
    ):
        dirs = {
            "uses_pika": self.module(
                "uses_pika", {"tests/test_q.py": "import pika\n"}
            ),
            "uses_pool": self.module(
                "uses_pool", {"models/a.py": "x = 'RabbitMQ pool'\n"}
            ),
            "uses_amqp": self.module(
                "uses_amqp", {"daemon/main.py": "AMQP = 1\n"}
            ),
            "plain": self.module(
                "plain",
                {
                    "models/b.py": "x = 1\n",
                    "tests/test_b.py": "pass\n",
                },
            ),
        }
        self.assertEqual(
            rs.modules_mentioning_broker(dirs),
            ["uses_amqp", "uses_pika", "uses_pool"],
        )

    def test_other_file_types_and_vendored_trees_do_not_count(self):
        dirs = {
            "m": self.module(
                "m",
                {
                    "README.md": "rabbitmq\n",
                    "static/lib.py": "pika\n",
                    "target/build.py": "amqp\n",
                    "node_modules/x/y.py": "rabbitmq\n",
                    "models/ok.py": "x = 1\n",
                },
            )
        }
        self.assertEqual(rs.modules_mentioning_broker(dirs), [])

    def test_a_missing_directory_is_not_a_user(self):
        self.assertEqual(
            rs.modules_mentioning_broker(
                {"gone": os.path.join(self.root, "gone")}
            ),
            [],
        )


class TestSlotRefusals(unittest.TestCase):
    TMP = "/var/lib/odoo/tmp"

    def resources(self, slot, private):
        return rs.slot_resources(slot, 3, private, self.TMP)

    def test_slot_one_is_never_refused(self):
        for private in (True, False):
            self.assertEqual(
                rs.slot_refusals(
                    self.resources(1, private), True, True, ["m"]
                ),
                [],
            )

    def test_a_plain_run_in_a_higher_slot_is_allowed(self):
        for private in (True, False):
            self.assertEqual(
                rs.slot_refusals(
                    self.resources(2, private), False, False, []
                ),
                [],
            )

    def test_on_the_host_pause_on_fail_and_mcp_are_slot_one_only_and_name_the_shortcut(self):
        reasons = rs.slot_refusals(
            self.resources(2, False), True, True, []
        )
        self.assertEqual(len(reasons), 2)
        self.assertIn("8075", reasons[0])
        self.assertIn("9222", reasons[1])
        self.assertIn("HAMS_ISOLATED_NS=1", reasons[1])
        self.assertIn("Leave HAMS_ISOLATED_NS=1 out", reasons[1])

    def test_the_full_pipeline_limits_only_mcp_above_slot_one(self):
        reasons = rs.slot_refusals(self.resources(2, True), True, True, ["backup_management"])
        self.assertEqual(len(reasons), 1)
        self.assertIn("--mcp", reasons[0])
        self.assertEqual(rs.slot_refusals(self.resources(3, True), True, False, ["hams_rabbitmq"]), [])

    def test_a_shared_host_broker_refuses_only_on_the_host(self):
        on_host = rs.slot_refusals(
            self.resources(2, False),
            False,
            False,
            ["backup_management", "hams_rabbitmq"],
        )
        self.assertEqual(len(on_host), 1)
        self.assertIn(
            "backup_management, hams_rabbitmq use RabbitMQ",
            on_host[0],
        )
        in_namespace = rs.slot_refusals(
            self.resources(2, True),
            False,
            False,
            ["backup_management"],
        )
        self.assertEqual(in_namespace, [])


if __name__ == "__main__":
    unittest.main()

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: runner_slots:runner_slots_slot_config_error_class]
