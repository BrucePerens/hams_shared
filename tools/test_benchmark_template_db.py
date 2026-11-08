#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for benchmark_template_db.py (the template-database benchmark harness).

Nothing here needs PostgreSQL, Odoo or the test slots: file trees are built in temporary directories, a fake `/proc` is
written for the CPU sampler, a real (tiny) child process is measured, and the server-facing steps are checked through
injected recorders. The measuring itself is done by running the tool against the real runner; see
docs/TEMPLATE_DATABASE_FOR_TEST_RUNS.md.

Tests [@ANCHOR: tools:template_db_identifier]
Tests [@ANCHOR: tools:template_db_check_identifier]
Tests [@ANCHOR: tools:template_db_parse_module_list]
Tests [@ANCHOR: tools:template_db_find_modules]
Tests [@ANCHOR: tools:template_db_dependency_closure]
Tests [@ANCHOR: tools:template_db_hash_sources]
Tests [@ANCHOR: tools:template_db_tests_define_models]
Tests [@ANCHOR: tools:template_db_key]
Tests [@ANCHOR: tools:template_db_verify_template]
Tests [@ANCHOR: tools:template_db_summarize]
Tests [@ANCHOR: tools:template_db_parse_loadscreen]
Tests [@ANCHOR: tools:template_db_read_loadscreen]
Tests [@ANCHOR: tools:template_db_log_milestones]
Tests [@ANCHOR: tools:template_db_parse_odoo_log]
Tests [@ANCHOR: tools:template_db_compare_outcomes]
Tests [@ANCHOR: tools:template_db_equality_lines]
Tests [@ANCHOR: tools:template_db_clone_sql]
Tests [@ANCHOR: tools:template_db_rekey_sql]
Tests [@ANCHOR: tools:template_db_proc_cpu_seconds]
Tests [@ANCHOR: tools:template_db_postgres_cpu_sampler]
Tests [@ANCHOR: tools:template_db_as_odoo]
Tests [@ANCHOR: tools:template_db_filestore_bytes]
Tests [@ANCHOR: tools:template_db_remove_filestore]
Tests [@ANCHOR: tools:template_db_arm_plan]
Tests [@ANCHOR: tools:template_db_arm_plan_for]
Tests [@ANCHOR: tools:template_db_transform_command]
Tests [@ANCHOR: tools:template_db_run_record]
Tests [@ANCHOR: tools:template_db_record_from_run]
Tests [@ANCHOR: tools:template_db_build_succeeded]
Tests [@ANCHOR: tools:template_db_valid_records]
Tests [@ANCHOR: tools:template_db_render_report]
Tests [@ANCHOR: tools:template_db_load_runner]
Tests [@ANCHOR: tools:template_db_stub_main]
Tests [@ANCHOR: tools:template_db_clone_for_run]
Tests [@ANCHOR: tools:template_db_ram_tablespace]
Tests [@ANCHOR: tools:template_db_timed_clone]
Tests [@ANCHOR: tools:template_db_run_measured]
Tests [@ANCHOR: tools:template_db_bench]
Tests [@ANCHOR: tools:template_db_report_command]
Tests [@ANCHOR: tools:template_db_build_parser]
Tests [@ANCHOR: tools:template_db_main]
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import benchmark_template_db as b  # noqa: E402


_CLUSTER = {}


def setUpModule():
    """A private PostgreSQL cluster (initdb in a temporary directory, a unix socket in it) for the tests that run real SQL.

    Fails, never skips, when the server binaries are missing: a test host without them cannot run this suite."""
    import infrastructure  # noqa: PLC0415

    initdb, pg_ctl, psql = (infrastructure.get_pg_bin(name) for name in ("initdb", "pg_ctl", "psql"))
    root = tempfile.mkdtemp(prefix="hbt")
    data, sock = os.path.join(root, "data"), os.path.join(root, "s")
    os.makedirs(sock)
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run([initdb, "-D", data, "-U", user, "--auth=trust", "-E", "UTF8", "--no-sync"], check=True, capture_output=True)
    subprocess.run(
        [pg_ctl, "-D", data, "-w", "-l", os.path.join(root, "log"), "-o", f"-c listen_addresses='' -c unix_socket_directories='{sock}' -c fsync=off -c max_connections=30", "start"],
        check=True, capture_output=True,
    )
    _CLUSTER.update(root=root, data=data, sock=sock, user=user, pg_ctl=pg_ctl, psql=psql)


def tearDownModule():
    if _CLUSTER:
        subprocess.run([_CLUSTER["pg_ctl"], "-D", _CLUSTER["data"], "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(_CLUSTER["root"], ignore_errors=True)


def write(path, text="", mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as handle:
        handle.write(text)


def make_addons(root):
    """A tiny addons directory: app -> lib -> (Odoo's own) mail; plus an unrelated module."""
    write(os.path.join(root, "app", "__manifest__.py"), "{'name': 'app', 'depends': ['lib', 'mail']}")
    write(os.path.join(root, "app", "data", "d.xml"), "<odoo>1</odoo>")
    write(os.path.join(root, "app", "tests", "test_a.py"), "x = 1\n")
    write(os.path.join(root, "lib", "__manifest__.py"), "{'name': 'lib', 'depends': ['base']}")
    write(os.path.join(root, "other", "__manifest__.py"), "{'name': 'other', 'depends': []}")
    return root


class IdentifierAndListTests(unittest.TestCase):
    def test_identifier_accepts_plain_names_and_refuses_the_rest(self):
        self.assertEqual(b.check_identifier("hams_tpl_ab12"), "hams_tpl_ab12")
        for bad in ('a"b', "1abc", "a b", "", None, "x;drop", "a" * 64):
            with self.assertRaises(ValueError):
                b.check_identifier(bad)
        self.assertTrue(b.SAFE_IDENTIFIER_RE.match("ok_1"))

    def test_module_list_keeps_order_and_drops_duplicates(self):
        self.assertEqual(b.parse_module_list("a,b\nc  b, a"), ["a", "b", "c"])
        self.assertEqual(b.parse_module_list(""), [])
        self.assertEqual(b.parse_module_list(None), [])


class SourceTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.root = make_addons(os.path.join(self.tmp, "addons"))
        self.dirs = b.find_module_dirs([self.root, os.path.join(self.tmp, "missing")])

    def test_find_modules_sees_manifest_directories_only(self):
        self.assertEqual(sorted(self.dirs), ["app", "lib", "other"])

    def test_closure_follows_depends_and_ignores_odoo_core(self):
        self.assertEqual(b.dependency_closure(["app"], self.dirs), ["app", "lib"])
        self.assertEqual(b.dependency_closure(["base"], self.dirs), [])

    def test_a_broken_manifest_is_an_error_not_a_guess(self):
        write(os.path.join(self.root, "lib", "__manifest__.py"), "{'depends': [")
        with self.assertRaises(SyntaxError):
            b.dependency_closure(["app"], b.find_module_dirs([self.root]))

    def test_hash_changes_with_data_migration_and_python_but_not_with_pyc(self):
        names = ["app", "lib"]
        base = b.hash_module_sources(self.dirs, names)
        self.assertEqual(base, b.hash_module_sources(self.dirs, list(reversed(names))))
        write(os.path.join(self.root, "app", "__pycache__", "x.pyc"), "junk")
        self.assertEqual(base, b.hash_module_sources(self.dirs, names))
        write(os.path.join(self.root, "app", "data", "d.xml"), "<odoo>2</odoo>")
        changed_data = b.hash_module_sources(self.dirs, names)
        self.assertNotEqual(base, changed_data)
        write(os.path.join(self.root, "lib", "migrations", "1.0", "post-m.py"), "def migrate(cr, v): pass\n")
        self.assertNotEqual(changed_data, b.hash_module_sources(self.dirs, names))

    def test_a_renamed_file_with_the_same_bytes_changes_the_hash(self):
        before = b.hash_module_sources(self.dirs, ["app"])
        os.rename(os.path.join(self.root, "app", "data", "d.xml"), os.path.join(self.root, "app", "data", "e.xml"))
        self.assertNotEqual(before, b.hash_module_sources(self.dirs, ["app"]))

    def test_tests_directory_counts_only_when_asked(self):
        with_tests = b.hash_module_sources(self.dirs, ["app"], include_tests=True)
        without = b.hash_module_sources(self.dirs, ["app"], include_tests=False)
        write(os.path.join(self.root, "app", "tests", "test_a.py"), "x = 2\n")
        self.assertNotEqual(with_tests, b.hash_module_sources(self.dirs, ["app"], include_tests=True))
        self.assertEqual(without, b.hash_module_sources(self.dirs, ["app"], include_tests=False))

    def test_symlinks_are_hashed_by_target_text_not_followed(self):
        os.symlink("/nonexistent/target", os.path.join(self.root, "app", "link"))
        self.assertTrue(b.hash_module_sources(self.dirs, ["app"]))

    def test_tests_that_define_models_are_reported(self):
        self.assertEqual(b.tests_define_models(self.dirs, ["app", "lib"]), [])
        write(os.path.join(self.root, "lib", "tests", "models.py"), "from odoo import models\nclass T(models.Model):\n    _name = 'x.t'\n")
        self.assertEqual(b.tests_define_models(self.dirs, ["app", "lib"]), ["lib"])
        write(os.path.join(self.root, "lib", "tests", "models.py"), "# models.Model is mentioned only in a comment\n")
        self.assertEqual(b.tests_define_models(self.dirs, ["lib"]), [])


def sample_key(**changes):
    fields = dict(modules=("base", "app"), source_hash="aa", odoo_version="19.0-20261107", postgres_major="18", vector_version="0.8.1", options=("demo",), crypto_key_fingerprint="ff")
    fields.update(changes)
    return b.TemplateKey(**fields)


class KeyTests(unittest.TestCase):
    def test_digest_is_stable_and_the_name_encodes_it(self):
        key = sample_key()
        self.assertEqual(key.digest(), sample_key().digest())
        self.assertEqual(key.database_name(), "hams_tpl_" + key.digest()[:16])
        b.check_identifier(key.database_name())

    def test_every_field_changes_the_name(self):
        base = sample_key().database_name()
        for change in (
            {"modules": ("base", "app", "lib")},
            {"source_hash": "ab"},
            {"odoo_version": "19.0-20261108"},
            {"postgres_major": "19"},
            {"vector_version": "0.9"},
            {"options": ("without-demo",)},
            {"crypto_key_fingerprint": "00"},
        ):
            self.assertNotEqual(base, sample_key(**change).database_name(), change)

    def test_verify_template_accepts_only_its_own_comment(self):
        key = sample_key()
        self.assertEqual(b.verify_template(key.comment(), key), (True, ""))
        for comment in (None, "", "not json", "[]", json_dumps({"hams_template_db": 2}), sample_key(source_hash="zz").comment()):
            ok, why = b.verify_template(comment, key)
            self.assertFalse(ok)
            self.assertTrue(why)


def json_dumps(value):
    return json.dumps(value)


class StatsAndLoadTests(unittest.TestCase):
    def test_summarize(self):
        stats = b.summarize([10, 12, 14])
        self.assertEqual((stats["n"], stats["median"], stats["min"], stats["max"]), (3, 12, 10, 14))
        self.assertAlmostEqual(stats["stdev"], 2.0)
        self.assertAlmostEqual(stats["cv_percent"], 100 * 2.0 / 12)
        self.assertEqual(b.summarize([5])["stdev"], 0.0)
        self.assertEqual(b.summarize([None])["n"], 0)
        self.assertIsNone(b.summarize([])["median"])

    LOADSCREEN = textwrap.dedent(
        """\
        loadscreen 2026-11-08 09:54:27   up 252s   16 cores / 32 threads (21 core-equivalents)   4 test slots
        RECOMMENDATION: HOLD   headroom 5.1 core-equivalents

                               1 min     5 min    15 min
        cpu busy %              56.7      60.6      60.6
        cpu iowait %             3.4       2.9       2.9
        load1                   57.7      60.7      60.7
        postgres                18.4      17.3      17.3
        test slots busy          3.0       3.0       3.0
        tests waiting            2.0       1.9       1.9
        mem avail GB           100.5      99.1      99.1
        """
    )

    def test_loadscreen_fields(self):
        state = b.parse_loadscreen(self.LOADSCREEN)
        self.assertEqual(state["recommendation"], "HOLD")
        self.assertEqual(state["cpu_busy_1min"], 56.7)
        self.assertEqual(state["cpu_busy_5min"], 60.6)
        self.assertEqual(state["load1"], 57.7)
        self.assertEqual(state["test_slots_busy"], 3.0)
        self.assertEqual(state["mem_avail_gb"], 100.5)
        self.assertEqual(b.parse_loadscreen("garbage"), {})
        self.assertEqual(b.parse_loadscreen(None), {})

    def test_read_loadscreen_from_a_file_and_a_missing_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as handle:
            handle.write(self.LOADSCREEN)
            handle.flush()
            self.assertEqual(b.read_loadscreen(handle.name)["load1"], 57.7)
        self.assertEqual(b.read_loadscreen("/nonexistent/stats.txt"), {})


LOG = textwrap.dedent(
    """\
    [*] runner banner that is not a log line
    2026-11-08 11:17:50,445 100 INFO db odoo.modules.loading: loading 132 modules...
    2026-11-08 11:19:33,726 100 INFO db odoo.modules.loading: 132 modules loaded in 98.30s, 101925 queries (+101956 extra)
    2026-11-08 11:19:34,847 100 INFO db odoo.modules.loading: Modules loaded.
    2026-11-08 11:19:35,925 100 INFO db odoo.service.server: Starting post tests
    2026-11-08 11:20:03,043 100 INFO db odoo.addons.m.tests.test_x: Starting TestA.test_one ...
    2026-11-08 11:20:04,043 100 INFO db odoo.addons.m.tests.test_x: Starting TestA.test_two ...
    2026-11-08 11:20:05,043 100 ERROR db odoo.addons.m.tests.test_x: FAIL: TestA.test_two
    Traceback (most recent call last):
    2026-11-08 11:20:06,043 100 ERROR db odoo.addons.m.tests.test_y: ERROR: TestB.test_three
    2026-11-08 11:34:41,557 100 ERROR db odoo.tests.result: 1 failed, 1 error(s) of 3 tests when loading database 'db'
    """
)


class LogTests(unittest.TestCase):
    def test_milestones(self):
        m = b.parse_odoo_log(LOG.splitlines())
        self.assertAlmostEqual(m.modules_loaded - m.first_line, 104.402, places=3)
        self.assertAlmostEqual(m.first_test - m.post_tests_start, 27.118, places=3)
        self.assertEqual(m.first_test, m.first_post_install_test)
        self.assertEqual(m.tests_started, ["TestA.test_one", "TestA.test_two"])
        self.assertEqual(m.failed, {"TestA.test_two", "TestB.test_three"})
        self.assertEqual(m.result, (1, 1, 3))
        self.assertEqual(m.install_seconds, 98.30)

    def test_at_install_tests_are_not_post_install_tests(self):
        lines = [
            "2026-11-08 11:00:00,000 1 INFO db odoo.addons.m.tests.t: Starting TestAt.test_early ...",
            "2026-11-08 11:00:10,000 1 INFO db odoo.service.server: Starting post tests",
            "2026-11-08 11:00:20,000 1 INFO db odoo.addons.m.tests.t: Starting TestPost.test_late ...",
        ]
        m = b.parse_odoo_log(lines)
        self.assertEqual(m.first_test, m.first_line)
        self.assertAlmostEqual(m.first_post_install_test - m.post_tests_start, 10.0)

    def test_empty_log_has_no_milestones(self):
        m = b.parse_odoo_log([])
        self.assertIsNone(m.first_test)
        self.assertIsNone(m.result)

    def test_compare_outcomes(self):
        fresh = b.parse_odoo_log(LOG.splitlines())
        same = b.parse_odoo_log(LOG.splitlines())
        self.assertTrue(b.compare_outcomes(fresh, same)["same"])
        passed = b.parse_odoo_log(LOG.replace("ERROR db odoo.addons.m.tests.test_x: FAIL: TestA.test_two", "INFO db x: fine").splitlines())
        verdict = b.compare_outcomes(fresh, passed)
        self.assertFalse(verdict["same"])
        self.assertEqual(verdict["failed_differ"], ["TestA.test_two"])
        fewer = b.parse_odoo_log(LOG.replace("Starting TestA.test_one ...", "skipped").splitlines())
        verdict = b.compare_outcomes(fresh, fewer)
        self.assertEqual(verdict["only_in_reference"], ["TestA.test_one"])
        restricted = b.compare_outcomes(fresh, fewer, post_install_names=["TestA.test_two"])
        self.assertTrue(restricted["same"])


class EqualityTests(unittest.TestCase):
    def rec(self, arm, rep, names, failed, result):
        return {"set_name": "small", "arm": arm, "rep": rep, "outcome_names": names, "failed_names": failed, "result": result}

    def test_same_different_and_the_trap(self):
        fresh = self.rec("fresh_ssd", 1, ["A.t1", "A.t2"], ["setUpClass (odoo.addons.m.tests.t.B)"], [0, 1, 2])
        records = [
            fresh,
            self.rec("build", 1, [], [], None),
            self.rec("clone_ssd", 1, ["A.t2", "A.t1"], ["setUpClass (odoo.addons.m.tests.t.B)"], [0, 1, 2]),
            self.rec("clone_update", 1, ["A.t1"], [], [0, 0, 1]),
            self.rec("clone_i", 1, [], [], [0, 0, 0]),
        ]
        lines = b.equality_lines(records)
        self.assertEqual(len(lines), 3)
        self.assertIn("clone_ssd #1 vs fresh_ssd #1: SAME outcome", lines[0])
        self.assertIn("DIFFERENT", lines[1])
        self.assertIn("0 vs 2 tests started", lines[2])
        self.assertEqual(b.equality_lines([self.rec("clone_ssd", 1, [], [], None)]), [])

    def test_setupclass_errors_are_named_in_the_log(self):
        log = ["2026-11-08 19:03:23,573 9 ERROR db odoo.tests.suite: ERROR: setUpClass (odoo.addons.parking.tests.test_http.TestParkingHttp)"]
        self.assertEqual(b.parse_odoo_log(log).failed, {"setUpClass (odoo.addons.parking.tests.test_http.TestParkingHttp)"})


class PsqlRetryTests(unittest.TestCase):
    RUNNER = SimpleNamespace(infrastructure=SimpleNamespace(get_pg_bin=lambda name: "/usr/bin/psql"))

    def test_a_recovering_server_is_retried_and_a_real_error_is_not(self):
        answers = [
            SimpleNamespace(returncode=2, stdout=b"", stderr=b"FATAL:  the database system is in recovery mode"),
            SimpleNamespace(returncode=2, stdout=b"", stderr=b"psql: error: connection refused: Connection refused"),
            SimpleNamespace(returncode=0, stdout=b"42\n", stderr=b""),
        ]
        with patch.object(b.subprocess, "run", side_effect=answers) as run, patch.object(b.time, "sleep") as sleep:
            self.assertEqual(b._psql(self.RUNNER, "SELECT 1"), "42\n")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        syntax = SimpleNamespace(returncode=3, stdout=b"", stderr=b"ERROR:  syntax error")
        with patch.object(b.subprocess, "run", return_value=syntax) as run, patch.object(b.time, "sleep") as sleep:
            with self.assertRaises(b.subprocess.CalledProcessError):
                b._psql(self.RUNNER, "SELEC")
        self.assertEqual((run.call_count, sleep.call_count), (1, 0))

    def test_a_server_that_never_comes_back_gives_up(self):
        down = SimpleNamespace(returncode=2, stdout=b"", stderr=b"the database system is starting up")
        with patch.object(b.subprocess, "run", return_value=down) as run, patch.object(b.time, "sleep"):
            with self.assertRaises(b.subprocess.CalledProcessError):
                b._psql(self.RUNNER, "SELECT 1")
        self.assertEqual(run.call_count, b.PSQL_RETRIES)


class SqlTests(unittest.TestCase):
    def test_clone_sql(self):
        self.assertEqual(b.clone_database_sql("c", "t"), 'CREATE DATABASE "c" TEMPLATE "t";')
        self.assertEqual(
            b.clone_database_sql("c", "t", tablespace="ts", strategy="file_copy"),
            'CREATE DATABASE "c" TEMPLATE "t" TABLESPACE "ts" STRATEGY = FILE_COPY;',
        )
        for args, kwargs in ((("c", "t; DROP"), {}), (('c"', "t"), {}), (("c", "t"), {"tablespace": "a b"}), (("c", "t"), {"strategy": "fast"})):
            with self.assertRaises(ValueError):
                b.clone_database_sql(*args, **kwargs)

    def test_rekey_sql_touches_exactly_the_secret_parameters(self):
        sql = b.rekey_sql()
        for key in b.SECRET_PARAMETERS:
            self.assertIn(f"'{key}'", sql)
        self.assertIn("UPDATE ir_config_parameter", sql)
        self.assertIn("database.create_date", sql)
        self.assertNotIn("DELETE", sql)


class CpuTests(unittest.TestCase):
    STAT = "123 (postgres: odoo db (x) y) S 1 2 3 4 5 6 7 8 9 10 250 150 0 0 20 0 1 0 100 1 2"

    def test_proc_cpu_seconds_handles_parentheses_in_the_name(self):
        self.assertEqual(b.proc_cpu_seconds(self.STAT, 100), 4.0)
        self.assertIsNone(b.proc_cpu_seconds("garbage"))
        self.assertIsNone(b.proc_cpu_seconds("1 (x) S 1"))

    def test_sampler_sums_the_maximum_per_backend_of_the_right_database(self):
        with tempfile.TemporaryDirectory() as proc:
            def process(pid, cmdline, utime):
                write(os.path.join(proc, str(pid), "cmdline"), cmdline.replace(" ", "\0"))
                write(os.path.join(proc, str(pid), "stat"), f"{pid} (postgres) S 1 2 3 4 5 6 7 8 9 10 {utime} 0 0 0 20 0 1 0 100 1 2")

            process(10, "postgres: odoo hams_bench [local] idle", 200)
            process(11, "postgres: odoo other_db [local] idle", 900)
            process(12, "python3 hams_bench", 900)
            write(os.path.join(proc, "notapid", "x"), "")
            sampler = b.PostgresCpuSampler("hams_bench", interval=0.01, proc_root=proc, clock_ticks=100)
            with sampler:
                pass
            self.assertEqual(sampler.cpu_seconds, 2.0)
            process(10, "postgres: odoo hams_bench [local] idle", 100)  # a lower reading never lowers the total
            sampler.sample_once()
            self.assertEqual(sampler.cpu_seconds, 2.0)
            process(10, "postgres: odoo hams_bench [local] idle", 300)
            sampler.sample_once()
            self.assertEqual(sampler.cpu_seconds, 3.0)

    def test_sampler_with_a_missing_proc_root_reads_zero(self):
        sampler = b.PostgresCpuSampler("x", proc_root="/nonexistent-proc")
        sampler.sample_once()
        self.assertEqual(sampler.cpu_seconds, 0)

    def test_as_odoo_prefixes_sudo_and_optionally_env(self):
        self.assertEqual(b.as_odoo(["id"]), ["sudo", "-n", "-u", "odoo", "id"])
        self.assertEqual(b.as_odoo(["id"], env={"B": "2", "A": "1"}), ["sudo", "-n", "-u", "odoo", "env", "A=1", "B=2", "id"])

    def test_filestore_helpers_use_sudo_and_refuse_foreign_names(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="1234\t/path\n")

        with patch.object(b.subprocess, "run", fake_run):
            self.assertEqual(b.filestore_bytes("hams_tpl_1"), 1234)
            b.remove_filestore("hams_tpl_1_ram")
            with self.assertRaises(ValueError):
                b.remove_filestore("hams_test_s2")
            with self.assertRaises(ValueError):
                b.filestore_bytes("x; rm -rf /")
        self.assertEqual(calls[0][:4], ["sudo", "-n", "-u", "odoo"])
        self.assertEqual(calls[1][-1], b.FILESTORE_ROOT + "/hams_tpl_1_ram")
        self.assertEqual(len(calls), 2)
        failing = SimpleNamespace(returncode=1, stdout="")
        with patch.object(b.subprocess, "run", lambda *a, **k: failing):
            self.assertEqual(b.filestore_bytes("hams_tpl_1"), 0)


class ArmTests(unittest.TestCase):
    CMD = ["python3", "-m", "x", "odoo-bin", "--load=base", "-d", "hams_bench_s2", "-i", "base,a,b", "--test-enable", "--test-tags", "/a,/b", "--stop-after-init"]

    def test_plans(self):
        self.assertFalse(b.arm_plan("fresh_ssd").clone)
        self.assertTrue(b.arm_plan("fresh_ram").ram)
        self.assertTrue(b.arm_plan("build").build and not b.arm_plan("build").tests)
        self.assertTrue(b.arm_plan("clone_ssd").drop_install)
        self.assertTrue(b.arm_plan("clone_ram").ram and b.arm_plan("clone_ram").clone)
        self.assertTrue(b.arm_plan("clone_update").update and not b.arm_plan("clone_update").drop_install)
        self.assertEqual({a for a in b.ARMS}, {b.arm_plan(a).arm for a in b.ARMS})
        self.assertTrue(b.arm_plan("clone_i").clone and not b.arm_plan("clone_i").drop_install)
        with self.assertRaises(ValueError):
            b.arm_plan("nope")

    def test_fresh_is_untouched(self):
        cmd = list(self.CMD)
        self.assertEqual(b.transform_odoo_command(cmd, b.arm_plan("fresh_ssd")), self.CMD)
        self.assertEqual(cmd, self.CMD)

    def test_clone_drops_install_and_keeps_tests(self):
        out = b.transform_odoo_command(self.CMD, b.arm_plan("clone_ssd"))
        self.assertNotIn("-i", out)
        self.assertNotIn("base,a,b", out)
        self.assertIn("--test-enable", out)
        self.assertEqual(out[out.index("--test-tags") + 1], "/a,/b")

    def test_update_swaps_flag_and_leaves_base_out(self):
        out = b.transform_odoo_command(self.CMD, b.arm_plan("clone_update"))
        self.assertEqual(out[out.index("-u") + 1], "a,b")
        self.assertNotIn("-i", out)

    def test_build_renames_database_and_removes_tests(self):
        out = b.transform_odoo_command(self.CMD, b.arm_plan("build"), database="hams_tpl_1")
        self.assertEqual(out[out.index("-d") + 1], "hams_tpl_1")
        self.assertIn("-i", out)
        self.assertNotIn("--test-enable", out)
        self.assertNotIn("--test-tags", out)
        self.assertNotIn("/a,/b", out)


class RecordAndReportTests(unittest.TestCase):
    def test_record_times_are_measured_from_rebuild_start(self):
        m = b.parse_odoo_log(LOG.splitlines())
        start = m.first_line - 5.0
        events = {"rebuild_start": start, "rebuild_end": start + 3.0, "odoo_end": m.last_line + 1.0}
        record = b.record_from_run("small", "fresh_ssd", 1, events, m, 12.5, 3.25, {"load1": 50}, {"load1": 60}, 1)
        self.assertEqual(record.wall_prepare, 3.0)
        self.assertAlmostEqual(record.wall_to_modules_loaded, 109.402, places=3)
        self.assertEqual(record.result, [1, 1, 3])
        self.assertEqual(record.failed_names, ["TestA.test_two", "TestB.test_three"])
        self.assertEqual((record.cpu_runner_seconds, record.cpu_postgres_seconds, record.exit_code), (12.5, 3.25, 1))

    def test_missing_events_give_none_not_a_crash(self):
        record = b.record_from_run("small", "build", 1, {}, b.parse_odoo_log([]), None, None, {}, {}, 0)
        self.assertIsNone(record.wall_total)
        self.assertIsNone(record.result)
        self.assertIsInstance(record, b.RunRecord)

    def test_build_succeeds_on_modules_loaded_without_errors_whatever_the_exit_code(self):
        clean = b.parse_odoo_log(["2026-11-08 11:00:00,000 1 INFO db odoo.modules.loading: Modules loaded."])
        started = {"rebuild_start": clean.first_line - 1}
        self.assertTrue(b.build_succeeded(b.record_from_run("small", "build", 1, started, clean, 1.0, 1.0, {}, {}, 1)))
        noisy = b.parse_odoo_log([
            "2026-11-08 11:00:00,000 1 ERROR db odoo.modules.loading: boom",
            "2026-11-08 11:00:01,000 1 INFO db odoo.modules.loading: Modules loaded.",
        ])
        self.assertEqual(noisy.error_lines, 1)
        self.assertFalse(b.build_succeeded(b.record_from_run("small", "build", 1, {"rebuild_start": 0}, noisy, 1.0, 1.0, {}, {}, 0)))
        benign = b.parse_odoo_log([
            "2026-11-08 11:00:00,000 1 ERROR db odoo.tests.common: Importing test framework, avoid importing from business modules and when not running in test mode",
            "2026-11-08 11:00:01,000 1 INFO db odoo.modules.loading: Modules loaded.",
        ])
        self.assertEqual(benign.error_lines, 0)
        never = b.parse_odoo_log(["2026-11-08 11:00:00,000 1 INFO db odoo.modules.loading: loading 3 modules..."])
        self.assertFalse(b.build_succeeded(b.record_from_run("small", "build", 1, {}, never, 1.0, 1.0, {}, {}, 0)))

    def test_invalid_runs_are_left_out_of_tables_and_comparisons(self):
        good = b.RunRecord("small", "fresh_ssd", 1, wall_total=100.0)
        bad = b.RunRecord("small", "fresh_ssd", 2, wall_total=900.0, notes="INVALID: postgres crash-restarted")
        self.assertEqual(len(b.valid_records([good, bad])), 1)
        self.assertIn("| fresh_ssd (1) |", b.render_report([good, bad]))
        clone = b.RunRecord("small", "clone_ssd", 1, notes="INVALID: x")
        self.assertEqual(b.equality_lines(b.valid_records([good, clone])), [])

    def test_report_has_a_row_per_arm_and_the_load_range(self):
        records = [
            b.RunRecord("small", "fresh_ssd", 1, wall_total=100.0, wall_prepare=2.0, load_before={"load1": 40}, load_after={"load1": 60}),
            b.RunRecord("small", "fresh_ssd", 2, wall_total=120.0, wall_prepare=2.0),
            b.RunRecord("small", "clone_ssd", 1, wall_total=70.0, wall_prepare=1.0),
            b.RunRecord("large", "fresh_ssd", 1, wall_total=5000.0),
        ]
        text = b.render_report(records)
        self.assertIn("### small", text)
        self.assertIn("| fresh_ssd (2) | 2.0 [2.0-2.0] | - | - | - | 110.0 [100.0-120.0] |", text)
        self.assertIn("| clone_ssd (1) |", text)
        self.assertLess(text.index("### small"), text.index("### large"))
        self.assertIn("load1 during these runs: 40 to 60", text)


class RunnerLoadingTests(unittest.TestCase):
    def fake_repo(self, body):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        write(os.path.join(tmp, "hams_shared", "tools", "test.py"), body)
        return tmp

    def setUp(self):
        self.cwd = os.getcwd()
        self.path = list(sys.path)
        self.addCleanup(os.chdir, self.cwd)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), self.path))
        self.addCleanup(sys.modules.pop, "hams_test_runner", None)

    def test_missing_runner_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                b.load_runner_module(tmp)

    def test_runner_loads_under_its_own_name_and_the_cwd_is_the_repo(self):
        repo = self.fake_repo("VALUE = 7\n")
        module = b.load_runner_module(repo)
        self.assertEqual(module.VALUE, 7)
        self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(repo))
        self.assertIs(sys.modules["hams_test_runner"], module)

    FAKE_RUNNER = textwrap.dedent(
        """\
        import sys
        calls = []
        def rebuild_db(db_name, redis_flush_db=None):
            calls.append(("original_rebuild", db_name))
        def run_cmd(cmd, extractor=None):
            calls.append(("run", list(cmd)))
            return 0
        def main():
            rebuild_db("hams_bench_s2", 5)
            run_cmd(["odoo", "-d", "hams_bench_s2", "-i", "base,a", "--test-enable", "--test-tags", "/a"])
            calls.append(("argv", sys.argv[1:]))
        """
    )

    def run_stub(self, arm):
        repo = self.fake_repo(self.FAKE_RUNNER)
        events = os.path.join(repo, "events")
        with patch.dict(os.environ, {"HAMS_BENCH_EVENTS": events}):
            seen = []
            with patch.object(b, "clone_for_run", lambda *args: seen.append(args)):
                with patch.object(b, "_psql", lambda *args, **kwargs: seen.append(("psql", args[1]))):
                    self.assertEqual(b.stub_main([arm, repo, "hams_tpl_1", "--solo", "-u", "a"]), 0)
        runner = sys.modules["hams_test_runner"]
        with open(events, encoding="utf-8") as handle:
            names = [json.loads(line)["event"] for line in handle]
        return runner.calls, names, seen

    def test_stub_fresh_arm_runs_the_original_rebuild_and_command(self):
        calls, names, _seen = self.run_stub("fresh_ssd")
        self.assertEqual(calls[0], ("original_rebuild", "hams_bench_s2"))
        self.assertIn("-i", calls[1][1])
        self.assertEqual(calls[2], ("argv", ["--solo", "-u", "a"]))
        self.assertEqual(names, ["rebuild_start", "rebuild_end", "odoo_start", "odoo_end"])

    def test_stub_clone_arm_clones_and_drops_install(self):
        calls, _names, seen = self.run_stub("clone_ssd")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1:3], ("hams_bench_s2", "hams_tpl_1"))
        self.assertEqual([c for c in calls if c[0] == "original_rebuild"], [])
        self.assertNotIn("-i", calls[0][1])

    def test_stub_build_arm_uses_the_template_name_and_no_tests(self):
        calls, _names, _seen = self.run_stub("build")
        self.assertEqual(calls[0], ("original_rebuild", "hams_tpl_1"))
        self.assertEqual(calls[1][1][calls[1][1].index("-d") + 1], "hams_tpl_1")
        self.assertNotIn("--test-enable", calls[1][1])

    def test_stub_ram_arm_moves_the_fresh_database_into_the_tablespace(self):
        _calls, _names, seen = self.run_stub("fresh_ram")
        self.assertIn(("psql", f'ALTER DATABASE "hams_bench_s2" SET TABLESPACE "{b.BENCH_TABLESPACE}";'), seen)


class CloneForRunTests(unittest.TestCase):
    def runner(self, marker=True):
        return SimpleNamespace(
            infrastructure=SimpleNamespace(get_pg_bin=lambda name: "/usr/bin/psql"),
            list_database_names=lambda psql, env: ["postgres"],
            refuse_to_run_on_production_host=lambda database_names=(): None,
            runner_cleanup=SimpleNamespace(comment_database_sql=lambda db, slot, keep: f"COMMENT ON DATABASE \"{db}\" IS 'slot {slot}';"),
            _RUN_CLEANUP={"slot": 2, "keep": False} if marker else None,
            remove_stale_filestore=lambda db: self.removed.append(db),
        )

    def setUp(self):
        self.sql = []
        self.removed = []
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        write(os.path.join(self.tmp, "Odoo", "filestore", "hams_tpl_1", "ab", "blob"), "x")

    def fake_psql(self, runner, sql, database="postgres", env=None):
        self.sql.append((database, sql))
        return "1\n"

    def test_clone_follows_the_runners_steps_and_replaces_only_the_create(self):
        with patch.object(b, "FILESTORE_ROOT", os.path.join(self.tmp, "Odoo", "filestore")), patch.object(b, "_psql", self.fake_psql), patch.object(b.subprocess, "run") as run:
            b.clone_for_run(self.runner(), "hams_bench_s2", "hams_tpl_1", b.arm_plan("clone_ssd"), 5)
        redis = [c for c in run.call_args_list if c.args[0][0] == "redis-cli"]
        self.assertEqual(redis[0].args[0], ["redis-cli", "-n", "5", "flushdb"])
        script = " ".join(sql for _db, sql in self.sql)
        self.assertIn('DROP DATABASE IF EXISTS "hams_bench_s2" WITH (FORCE);', script)
        self.assertIn('CREATE DATABASE "hams_bench_s2" TEMPLATE "hams_tpl_1" TABLESPACE "pg_default";', script)
        self.assertIn("COMMENT ON DATABASE \"hams_bench_s2\" IS 'slot 2';", script)
        self.assertNotIn("CREATE EXTENSION", script)
        self.assertEqual(self.sql[-1], ("hams_bench_s2", b.rekey_sql()))
        self.assertEqual(self.removed, ["hams_bench_s2"])
        copies = [c.args[0] for c in run.call_args_list if c.args[0][0] == "cp"]
        self.assertEqual(copies[0][:3], ["cp", "-a", "--reflink=auto"])
        self.assertTrue(copies[0][3].endswith("filestore/hams_tpl_1"))
        self.assertTrue(copies[0][4].endswith("filestore/hams_bench_s2"))

    def test_ram_arm_creates_in_the_tablespace(self):
        with patch.object(b, "FILESTORE_ROOT", os.path.join(self.tmp, "Odoo", "filestore")), patch.object(b, "_psql", self.fake_psql), patch.object(b.subprocess, "run"):
            b.clone_for_run(self.runner(), "hams_bench_s2", "hams_tpl_1_ram", b.arm_plan("clone_ram"), None)
        self.assertIn(f'TABLESPACE "{b.BENCH_TABLESPACE}"', " ".join(sql for _db, sql in self.sql))

    def test_a_missing_template_is_an_error_never_a_fresh_install(self):
        def none(runner, sql, database="postgres", env=None):
            return "\n"

        with patch.object(b, "FILESTORE_ROOT", os.path.join(self.tmp, "Odoo", "filestore")), patch.object(b, "_psql", none), patch.object(b.subprocess, "run"):
            with self.assertRaises(RuntimeError):
                b.clone_for_run(self.runner(), "hams_bench_s2", "hams_tpl_1", b.arm_plan("clone_ssd"), None)

    def test_unsafe_names_are_refused_before_any_sql(self):
        with patch.object(b, "_psql", self.fake_psql):
            with self.assertRaises(ValueError):
                b.clone_for_run(self.runner(), 'x"; DROP DATABASE postgres; --', "hams_tpl_1", b.arm_plan("clone_ssd"), None)
        self.assertEqual(self.sql, [])


class RamTablespaceTests(unittest.TestCase):
    def make(self, existing="", mount_point=1):
        self.sql_log, self.shell_log = [], []

        def sql(statement):
            self.sql_log.append(statement)
            if "FROM pg_tablespace WHERE spcname" in statement:
                return existing
            if "JOIN pg_tablespace" in statement:
                return "left_over\n"
            return ""

        def shell(argv):
            self.shell_log.append(argv)
            return SimpleNamespace(returncode=mount_point if argv[0] == "mountpoint" else 0, stderr="")

        return b.RamTablespace(os.path.join(tempfile.mkdtemp(), "ts"), "8G", sql, shell)

    def test_enter_mounts_chowns_and_creates_and_exit_undoes_it(self):
        ts = self.make()
        with ts:
            pass
        flat = [" ".join(c) for c in self.shell_log]
        self.assertTrue(any("mount -t tmpfs -o size=8G tmpfs" in c for c in flat))
        self.assertTrue(any("chown postgres:postgres" in c for c in flat))
        self.assertTrue(any(s.startswith(f'CREATE TABLESPACE "{b.BENCH_TABLESPACE}"') for s in self.sql_log))
        self.assertIn('DROP DATABASE IF EXISTS "left_over" WITH (FORCE);', self.sql_log)
        self.assertIn(f'DROP TABLESPACE IF EXISTS "{b.BENCH_TABLESPACE}";', self.sql_log)
        self.assertTrue(flat[-1].endswith("umount " + ts.directory))
        self.assertFalse(any("fstab" in c for c in flat))

    def test_an_existing_tablespace_or_mount_is_refused_and_nothing_is_touched(self):
        for kwargs in ({"existing": "1"}, {"mount_point": 0}):
            ts = self.make(**kwargs)
            with self.assertRaises(RuntimeError):
                ts.__enter__()
            self.assertFalse(any("mount -t" in " ".join(c) for c in self.shell_log))
            ts.__exit__(None, None, None)
            self.assertFalse(any("umount" in " ".join(c) for c in self.shell_log))
            self.assertFalse(any("DROP TABLESPACE" in s for s in self.sql_log))


class MeasureTests(unittest.TestCase):
    def test_timed_clone_against_a_real_server(self):
        self.admin("CREATE DATABASE tc_template")
        self.addCleanup(self.admin, "DROP DATABASE IF EXISTS tc_template WITH (FORCE)")
        readings = []

        def cpu(pid):
            readings.append(pid)
            return float(len(readings))

        result = b.timed_clone(self.connect, b.clone_database_sql("tc_clone", "tc_template"), 'DROP DATABASE "tc_clone" WITH (FORCE);', cpu)
        self.assertEqual(len(readings), 2)
        self.assertNotEqual(readings[0], os.getpid())  # a backend pid, not ours
        self.assertEqual(result["cpu"], 1.0)
        self.assertGreater(result["wall"], 0)
        self.assertEqual(self.query("SELECT count(*) FROM pg_database WHERE datname = 'tc_clone'"), "0")  # dropped afterwards

    def test_timed_clone_without_cpu_readings_and_the_real_backend_reading(self):
        self.admin("CREATE DATABASE tc_template2")
        self.addCleanup(self.admin, "DROP DATABASE IF EXISTS tc_template2 WITH (FORCE)")
        sql = b.clone_database_sql("tc_clone2", "tc_template2", strategy="file_copy")
        drop = 'DROP DATABASE "tc_clone2" WITH (FORCE);'
        self.assertIsNone(b.timed_clone(self.connect, sql, drop, lambda pid: None)["cpu"])
        real = b.timed_clone(self.connect, sql, drop, b._backend_cpu)
        self.assertGreaterEqual(real["cpu"], 0.0)

    def test_the_rekey_changes_exactly_the_secrets_in_a_clone_and_not_the_template(self):
        self.admin("CREATE DATABASE rk_template")
        self.addCleanup(self.admin, "DROP DATABASE IF EXISTS rk_template WITH (FORCE)")
        self.admin(
            "CREATE TABLE ir_config_parameter (key text PRIMARY KEY, value text);"
            "INSERT INTO ir_config_parameter VALUES ('database.secret','s'),('database.uuid','u'),('form_shield.signing_key','k'),"
            "('database.create_date','2000-01-01'),('web.base.url','https://x');",
            database="rk_template",
        )
        self.admin(b.clone_database_sql("rk_clone", "rk_template"))
        self.addCleanup(self.admin, 'DROP DATABASE IF EXISTS rk_clone WITH (FORCE)')
        self.admin(b.rekey_sql(), database="rk_clone")
        clone = dict(line.split("|") for line in self.query("SELECT key || '|' || value FROM ir_config_parameter ORDER BY key", database="rk_clone").split("\n"))
        template = dict(line.split("|") for line in self.query("SELECT key || '|' || value FROM ir_config_parameter ORDER BY key", database="rk_template").split("\n"))
        self.assertEqual(template["database.secret"], "s")
        for key in b.SECRET_PARAMETERS:
            self.assertNotEqual(clone[key], template[key], key)
        self.assertEqual(clone["web.base.url"], "https://x")
        self.assertGreater(len(clone["form_shield.signing_key"]), 32)

    def admin(self, sql, database="postgres"):
        return self.query(sql, database)

    def query(self, sql, database="postgres"):
        done = subprocess.run([_CLUSTER["psql"], "-X", "-q", "-At", "-h", _CLUSTER["sock"], "-U", _CLUSTER["user"], "-v", "ON_ERROR_STOP=1", database], input=sql, text=True, capture_output=True, check=True)
        return done.stdout.strip()

    def connect(self):
        import psycopg2  # noqa: PLC0415

        conn = psycopg2.connect(host=_CLUSTER["sock"], user=_CLUSTER["user"], dbname="postgres")
        conn.autocommit = True
        return conn

    def test_run_measured_copies_output_and_reports_exit_and_cpu(self):
        class NoSampler:
            cpu_seconds = 1.5

            def __init__(self, database):
                self.database = database

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "run.log")
            code = "import sys; print('hello'); print('err', file=sys.stderr); s = sum(i * i for i in range(2000000)); sys.exit(3)"
            lines, returncode, cpu, pg = b.run_measured([sys.executable, "-c", code], dict(os.environ), tmp, log_path, "db", sampler_factory=NoSampler)
            self.assertEqual(returncode, 3)
            self.assertEqual(sorted(line.strip() for line in lines), ["err", "hello"])
            self.assertGreater(cpu, 0)
            self.assertEqual(pg, 1.5)
            with open(log_path, encoding="utf-8") as handle:
                self.assertEqual(sorted(handle.read().split()), ["err", "hello"])


class FakeBench:
    """Stands in for Bench: records the calls run_command makes, touches no server."""

    instances = []

    def __init__(self, args):
        self.args = args
        self.calls, self.sql_log = [], []
        self.meta = {"key_seconds": 0.5, "tests_define_models": [], "closure_modules": 3}
        self.runner = SimpleNamespace(
            runner_slots=SimpleNamespace(configured_slot_count=lambda requested: 4),
            _acquire_lock_via_queue=lambda path, timeout, slots, only: 2,
            _test_runner_lock_path=lambda: "/nonexistent/lock",
        )
        FakeBench.instances.append(self)

    def build_key(self):
        return sample_key()

    def build_template(self, key, slot, slots, rep):
        self.calls.append(("build", slot, slots, rep))

    def sql(self, statement):
        self.sql_log.append(statement)
        return "stored" if "shobj_description" in statement else "1000"

    def clone_microbenchmark(self, template, ram_template, reps):
        self.calls.append(("micro", template, ram_template, reps))

    def run_arm(self, arm, rep, slot, slots, template, test_tags=None):
        self.calls.append(("arm", arm, rep, template, test_tags))

    def save(self):
        self.calls.append(("save",))


class CommandTests(unittest.TestCase):
    def args(self, **changes):
        base = dict(set_name="small", reps=2, build_reps=1, arms="fresh_ssd,clone_ram", test_tags="/parking", tmpfs_dir="/nonexistent/ts", tmpfs_size="8G",
                    clone_micro=True, keep_template=False, repo_root="/x", out_dir="/y", modules=None)
        base.update(changes)
        return SimpleNamespace(**base)

    def test_run_command_orders_the_work_and_always_cleans_up(self):
        FakeBench.instances.clear()
        events = []

        class FakeRam:
            def __init__(self, *a):
                events.append("init")

            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, *exc):
                events.append("exit")

        with patch.object(b, "Bench", FakeBench), patch.object(b, "RamTablespace", FakeRam), patch.object(b, "read_loadscreen", lambda: {}), patch.object(b.subprocess, "run"):
            self.assertEqual(b.run_command(self.args()), 0)
        bench = FakeBench.instances[0]
        key = sample_key()
        arms = [c for c in bench.calls if c[0] == "arm"]
        self.assertEqual([(c[1], c[2]) for c in arms], [("fresh_ssd", 1), ("clone_ram", 1), ("fresh_ssd", 2), ("clone_ram", 2)])
        self.assertEqual([c[3] for c in arms if c[1] == "clone_ram"], [key.database_name() + "_ram"] * 2)
        self.assertEqual([c[3] for c in arms if c[1] == "fresh_ssd"], [key.database_name()] * 2)
        self.assertEqual(arms[0][4], "/parking")
        self.assertEqual(bench.calls[0], ("build", 2, 4, 1))
        self.assertEqual(events, ["init", "enter", "exit"])
        joined = " ".join(bench.sql_log)
        self.assertIn(f'CREATE DATABASE "{key.database_name()}_ram" TEMPLATE "{key.database_name()}" TABLESPACE', joined)
        self.assertIn(f'DROP DATABASE IF EXISTS "{key.database_name()}" WITH (FORCE);', joined)
        self.assertEqual(bench.meta["template_verified"][0], False)

    def test_run_command_cleans_up_when_a_run_fails(self):
        FakeBench.instances.clear()

        class Exploding(FakeBench):
            def run_arm(self, *args, **kwargs):
                raise RuntimeError("boom")

        class FakeRam:
            exited = []

            def __init__(self, *a):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                FakeRam.exited.append(True)

        with patch.object(b, "Bench", Exploding), patch.object(b, "RamTablespace", FakeRam), patch.object(b, "read_loadscreen", lambda: {}), patch.object(b.subprocess, "run"):
            with self.assertRaises(RuntimeError):
                b.run_command(self.args())
        self.assertEqual(FakeRam.exited, [True])
        self.assertIn(f'DROP DATABASE IF EXISTS "{sample_key().database_name()}" WITH (FORCE);', " ".join(Exploding.instances[0].sql_log))

    def test_no_ram_arm_means_no_tablespace(self):
        FakeBench.instances.clear()
        with patch.object(b, "Bench", FakeBench), patch.object(b, "RamTablespace", side_effect=AssertionError("must not be created")), patch.object(b, "read_loadscreen", lambda: {}), patch.object(b.subprocess, "run"):
            self.assertEqual(b.run_command(self.args(arms="fresh_ssd,clone_ssd", clone_micro=False)), 0)

    def test_key_command_prints_the_name(self):
        out = io.StringIO()
        with patch.object(b, "Bench", FakeBench), contextlib.redirect_stdout(out):
            self.assertEqual(b.key_command(self.args()), 0)
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["database"], sample_key().database_name())
        self.assertEqual(printed["closure"], 3)


class BenchAndCommandTests(unittest.TestCase):
    def test_run_arm_assembles_the_stub_command_and_builds_the_record(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "logs"))
        bench = b.Bench.__new__(b.Bench)
        bench.args = SimpleNamespace(set_name="small", strategy=None, copy_method="copy")
        bench.repo_root, bench.out_dir, bench.records = tmp, tmp, []
        bench.meta = {}
        bench.runner = SimpleNamespace(runner_slots=SimpleNamespace(slot_database_name=lambda name, slot: f"{name}_s{slot}"))
        bench.module_list = lambda: ["parking"]
        seen = {}

        def fake_measured(cmd, env, cwd, log_path, database, **kwargs):
            seen.update(cmd=cmd, env=env, database=database)
            events = [c.split("=", 1)[1] for c in cmd if c.startswith("HAMS_BENCH_EVENTS=")][0]
            with open(events, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "rebuild_start", "time": 1000.0}) + "\n")
                handle.write(json.dumps({"event": "odoo_end", "time": 1090.0}) + "\n")
            return [], 0, 40.0, 5.0

        with patch.object(b, "run_measured", fake_measured), patch.object(b, "read_loadscreen", lambda: {"load1": 9.0}):
            record = bench.run_arm("clone_ssd", 2, 3, 4, "hams_tpl_1", test_tags="/parking")
        self.assertEqual(seen["cmd"][:4], ["sudo", "-n", "-u", "odoo"])
        stub = seen["cmd"].index("_stub")
        self.assertEqual(seen["cmd"][stub + 1:stub + 3], ["clone_ssd", tmp])
        self.assertEqual(seen["cmd"][stub + 3], "hams_tpl_1")
        self.assertIn("HOME=/var/lib/odoo", seen["cmd"])
        for expected in ("--solo", "--preflight-done", "--slot-held", "3", "--slots", "4", "/parking", "parking"):
            self.assertIn(expected, seen["cmd"])
        self.assertIn("HAMS_TEST_LOCK_HELD=1", seen["cmd"])
        self.assertIn("HAMS_ISOLATED_NS=1", seen["cmd"])
        self.assertTrue(any(c.startswith("HAMS_BENCH_EVENTS=") for c in seen["cmd"]))
        self.assertEqual(seen["database"], f"{b.BENCH_DATABASE}_s3")
        self.assertEqual(record.wall_total, 90.0)
        self.assertIn("NO odoo.tests.result", record.notes)
        self.assertEqual(bench.records, [record])
        with open(os.path.join(tmp, "results_small.json"), encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)["records"]), 1)

    def test_report_command_prints_tables_and_clone_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            record = {"set_name": "small", "arm": "fresh_ssd", "rep": 1, "wall_total": 50.0}
            meta = {"set": "small", "build": [{"rep": 1, "wall": 30.0, "cpu_runner": 20.0, "cpu_postgres": 5.0, "database_bytes": 9e7, "filestore_bytes": 2e6}],
                    "clone": [{"combo": "ssd->ssd wal_log", "wall": 1.5, "cpu": 0.5}, {"combo": "ssd->ssd wal_log", "wall": 2.5, "cpu": 0.7}]}
            write(path, json.dumps({"meta": meta, "records": [record]}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(b.report_command(SimpleNamespace(files=[path])), 0)
        text = out.getvalue()
        self.assertIn("| fresh_ssd (1) |", text)
        self.assertIn("build #1: 30.0s wall", text)
        self.assertIn("CREATE DATABASE ssd->ssd wal_log: 2.00s [1.50-2.50]", text)

    def test_parser_and_main_dispatch(self):
        args = b.build_parser().parse_args(["run", "--repo-root", "/x", "--set", "medium", "--reps", "2", "--arms", "fresh_ssd"])
        self.assertEqual((args.set_name, args.reps, args.arms, args.build_reps, args.clone_micro), ("medium", 2, "fresh_ssd", 1, True))
        with self.assertRaises(SystemExit):
            b.build_parser().parse_args(["run", "--repo-root", "/x", "--set", "huge"])
        with patch.object(b, "stub_main", return_value=7) as stub:
            self.assertEqual(b.main(["_stub", "a", "b"]), 7)
            stub.assert_called_once_with(["a", "b"])
        with patch.object(b, "report_command", return_value=0) as report:
            self.assertEqual(b.main(["report", "f.json"]), 0)
            self.assertEqual(report.call_args.args[0].files, ["f.json"])


if __name__ == "__main__":
    unittest.main()
