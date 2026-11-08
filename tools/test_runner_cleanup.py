#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for the end-of-run clean-up and start-up sweep of test.py runs in slot mode (ADR 0116, runner_cleanup.py and
the PostgreSQL / disk / lock parts in test.py).

Nothing here touches the machine's own PostgreSQL, its Odoo filestore or its lock files. The database tests start a
PRIVATE PostgreSQL cluster (initdb in a temporary directory, a unix socket in that directory) and use real
databases, real directories, real flocks and real child processes that really get SIGTERM and SIGKILL.

Tests [@ANCHOR: runner_cleanup:database_marker]
Tests [@ANCHOR: runner_cleanup:parse_database_marker]
Tests [@ANCHOR: runner_cleanup:is_droppable_name]
Tests [@ANCHOR: runner_cleanup:drop_database_sql]
Tests [@ANCHOR: runner_cleanup:comment_database_sql]
Tests [@ANCHOR: runner_cleanup:owned_slot]
Tests [@ANCHOR: runner_cleanup:SlotLeftovers]
Tests [@ANCHOR: runner_cleanup:leftovers]
Tests [@ANCHOR: runner_cleanup:SweepOperations]
Tests [@ANCHOR: runner_cleanup:sweep]
Tests [@ANCHOR: runner_cleanup:finish_run]
Tests [@ANCHOR: test:PostgresAccess]
Tests [@ANCHOR: test:remove_directory_tree]
Tests [@ANCHOR: test:sweep_slot_leftovers]
Tests [@ANCHOR: test:install_run_cleanup]
Tests [@ANCHOR: test:run_cleanup_at_exit]
Tests [@ANCHOR: test:root_phase_database_cleanup]
"""

import contextlib
import fcntl
import importlib.util
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import infrastructure  # noqa: E402
import runner_cleanup as rc  # noqa: E402

_TEST_PY_PATH = os.path.join(_HERE, "test.py")


def _load_test_py():
    spec = importlib.util.spec_from_file_location("_hams_test_runner_module_under_cleanup_test", _TEST_PY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_test_py()


class MarkerAndNameTests(unittest.TestCase):
    def test_marker_round_trips(self):
        self.assertEqual(rc.database_marker(2, False), "hams_test_runner:v1 slot=2 kept=0")
        self.assertEqual(rc.parse_database_marker(rc.database_marker(2, False)), (2, False))
        self.assertEqual(rc.parse_database_marker(rc.database_marker(15, True)), (15, True))

    def test_any_other_comment_is_not_a_marker(self):
        for text in (None, "", "default administrative connection database", "hams_test_runner:v1 slot=x kept=0",
                     "hams_test_runner:v1 slot=2 kept=2", "xhams_test_runner:v1 slot=2 kept=0",
                     "hams_test_runner:v1 slot=2 kept=0 and more"):
            with self.subTest(text=text):
                self.assertIsNone(rc.parse_database_marker(text))

    def test_protected_and_unsafe_names_are_never_droppable(self):
        for name in ("hams_prod", "postgres", "template0", "template1", "", "a-b", 'x"; drop', "1abc", "a b", None):
            with self.subTest(name=name):
                self.assertFalse(rc.is_droppable_name(name))
                with self.assertRaises(ValueError):
                    rc.drop_database_sql(name)
                with self.assertRaises(ValueError):
                    rc.comment_database_sql(name, 2, False)
        self.assertTrue(rc.is_droppable_name("hams_test_relay_s2"))

    def test_the_statements_are_exactly_these(self):
        self.assertEqual(rc.drop_database_sql("hams_test_s2"), 'DROP DATABASE IF EXISTS "hams_test_s2" WITH (FORCE);')
        self.assertEqual(
            rc.comment_database_sql("hams_test_s2", 2, True),
            "COMMENT ON DATABASE \"hams_test_s2\" IS 'hams_test_runner:v1 slot=2 kept=1';",
        )

    def test_owned_slot_follows_the_marker_then_the_old_naming_and_nothing_else(self):
        self.assertEqual(rc.owned_slot("anything", rc.database_marker(3, False)), 3)
        self.assertEqual(rc.owned_slot("whatever_name", rc.database_marker(1, False)), 1)
        self.assertEqual(rc.owned_slot("hams_test_s2", ""), 2)
        self.assertEqual(rc.owned_slot("hams_test_relay_s15", None), 15)
        # not owned: slot 1's bare name (a run without slots uses it), another runner's names, slots out of range
        for name in ("hams_test", "hams_test_relay", "hams_dev_s2", "hams_test_s1", "hams_test_s16", "my_s2", "hams_prod"):
            with self.subTest(name=name):
                self.assertIsNone(rc.owned_slot(name, ""))
        # a marker never makes a protected name owned
        self.assertIsNone(rc.owned_slot("hams_prod", rc.database_marker(2, False)))
        self.assertIsNone(rc.owned_slot("postgres", rc.database_marker(2, False)))
        self.assertIsNone(rc.owned_slot("x", "hams_test_runner:v1 slot=99 kept=0"))


class LeftoversTests(unittest.TestCase):
    def test_groups_by_slot_and_skips_kept_foreign_and_protected(self):
        rows = [
            ("postgres", "default administrative connection database"),
            ("hams_prod", ""),
            ("hams_test", ""),                                     # slot 1 / no-slots database: not guessed
            ("hams_test_s2", ""),                                  # older test.py leftover
            ("hams_test_relay_s2", rc.database_marker(2, False)),  # marked leftover
            ("kept_run", rc.database_marker(3, True)),             # --keep-db
            ("marked_slot1", rc.database_marker(1, False)),        # slot 1 of a slot-mode run
            ("someone_elses", "mine"),
        ]
        plan = rc.leftovers(
            rows,
            ["hams_test_s2", "hams_test_s7", "kept_run", "hams_prod", "hams_test", "unrelated"],
            ["slot-2", "slot-9", "slot-1", "other", "slot-16"],
        )
        by_slot = {p.slot: p for p in plan}
        self.assertEqual(sorted(by_slot), [1, 2, 7, 9])
        self.assertEqual(by_slot[2].databases, ["hams_test_s2", "hams_test_relay_s2"])
        self.assertEqual(by_slot[2].filestores, ["hams_test_s2", "hams_test_relay_s2"])
        self.assertTrue(by_slot[2].tmpdir)
        self.assertEqual(by_slot[1].databases, ["marked_slot1"])
        self.assertFalse(by_slot[1].tmpdir)                         # slot 1's TMPDIR is the shared one, never removed
        self.assertEqual(by_slot[7].databases, [])                  # orphan filestore of a database that is gone
        self.assertEqual(by_slot[7].filestores, ["hams_test_s7"])
        self.assertTrue(by_slot[9].tmpdir)
        self.assertEqual(by_slot[9].databases, [])
        # the kept database, its filestore and every protected or foreign name are untouched
        everything = [name for p in plan for name in p.databases + p.filestores]
        for name in ("kept_run", "hams_prod", "hams_test", "someone_elses", "postgres", "unrelated"):
            self.assertNotIn(name, everything)

    def test_nothing_to_do_is_an_empty_plan(self):
        self.assertEqual(rc.leftovers([("postgres", "")], [], []), [])
        self.assertEqual(rc.leftovers([], [], []), [])

    def test_the_dataclass_holds_what_the_docs_say(self):
        item = rc.SlotLeftovers(slot=4, databases=["a"], filestores=["a"], tmpdir=True)
        self.assertEqual((item.slot, item.databases, item.filestores, item.tmpdir), (4, ["a"], ["a"], True))


class SweepTests(unittest.TestCase):
    def _operations(self, held=(), failing=()):
        log = {"dropped": [], "filestores": [], "tmpdirs": [], "said": [], "released": [], "claimed": []}

        def claim(slot):
            log["claimed"].append(slot)
            if slot in held:
                return None
            return lambda: log["released"].append(slot)

        def record(kind):
            def action(argument):
                log[kind].append(argument)
                return argument not in failing
            return action

        ops = rc.SweepOperations(
            claim_slot=claim,
            drop_database=record("dropped"),
            remove_filestore=record("filestores"),
            remove_slot_tmpdir=record("tmpdirs"),
            say=log["said"].append,
        )
        return ops, log

    def test_a_slot_with_a_live_holder_is_not_touched_and_others_are(self):
        plan = [
            rc.SlotLeftovers(2, ["live_db"], ["live_db"], True),
            rc.SlotLeftovers(3, ["dead_db"], ["dead_db"], True),
        ]
        ops, log = self._operations(held=(2,))
        outcome = rc.sweep(plan, ops)
        self.assertEqual(log["dropped"], ["dead_db"])
        self.assertEqual(log["filestores"], ["dead_db"])
        self.assertEqual(log["tmpdirs"], [3])
        self.assertEqual(outcome["skipped"], ["slot 2"])
        self.assertEqual(outcome["removed"], ["database dead_db", "filestore dead_db", "tmpdir slot-3"])
        self.assertEqual(log["released"], [3])          # every claimed slot is released
        self.assertTrue(any("slot 2 is held by a live run" in line for line in log["said"]))

    def test_a_failed_removal_is_reported_and_the_slot_still_released(self):
        ops, log = self._operations(failing=("bad",))
        outcome = rc.sweep([rc.SlotLeftovers(4, ["bad", "good"], [], False)], ops)
        self.assertEqual(outcome["failed"], ["database bad"])
        self.assertEqual(outcome["removed"], ["database good"])
        self.assertEqual(log["released"], [4])

    def test_an_empty_plan_claims_nothing(self):
        ops, log = self._operations()
        self.assertEqual(rc.sweep([], ops), {"removed": [], "skipped": [], "failed": []})
        self.assertEqual(log["claimed"], [])


class FinishRunTests(unittest.TestCase):
    def _finish(self, slot, keep, comment, drop_ok=True):
        calls = {"dropped": [], "filestores": [], "tmpdir": 0, "said": []}

        def tmpdir():
            calls["tmpdir"] += 1
            return True

        def drop(name):
            calls["dropped"].append(name)
            return drop_ok

        result = rc.finish_run(
            "db_s2", slot, keep, lambda name: comment, drop,
            lambda name: calls["filestores"].append(name) or True, tmpdir, calls["said"].append,
        )
        return result, calls

    def test_a_marked_unkept_database_is_dropped_with_its_filestore_and_directory(self):
        result, calls = self._finish(2, False, rc.database_marker(2, False))
        self.assertEqual(result, "dropped")
        self.assertEqual((calls["dropped"], calls["filestores"], calls["tmpdir"]), (["db_s2"], ["db_s2"], 1))

    def test_keep_removes_nothing_and_prints_the_name(self):
        result, calls = self._finish(2, True, rc.database_marker(2, True))
        self.assertEqual(result, "kept")
        self.assertEqual((calls["dropped"], calls["filestores"], calls["tmpdir"]), ([], [], 0))
        self.assertIn("db_s2", "\n".join(calls["said"]))
        self.assertIn("KEPT", "\n".join(calls["said"]))

    def test_a_database_an_earlier_run_kept_is_left_even_when_this_run_did_not_ask_to_keep(self):
        result, calls = self._finish(2, False, rc.database_marker(2, True))
        self.assertEqual(result, "kept")
        self.assertEqual(calls["dropped"], [])

    def test_a_database_without_this_slots_marker_is_never_dropped(self):
        for comment in ("", "somebody else's", rc.database_marker(3, False)):
            with self.subTest(comment=comment):
                result, calls = self._finish(2, False, comment)
                self.assertEqual(result, "foreign")
                self.assertEqual(calls["dropped"], [])
                self.assertIn("leaving it alone", "\n".join(calls["said"]))

    def test_no_database_still_removes_the_filestore_and_directory(self):
        result, calls = self._finish(2, False, None)
        self.assertEqual(result, "absent")
        self.assertEqual((calls["dropped"], calls["filestores"], calls["tmpdir"]), ([], ["db_s2"], 1))

    def test_keep_with_no_database_prints_nothing_to_keep(self):
        result, calls = self._finish(2, True, None)
        self.assertEqual((result, calls["said"]), ("kept", []))

    def test_a_failed_drop_is_reported_and_keeps_the_files(self):
        result, calls = self._finish(2, False, rc.database_marker(2, False), drop_ok=False)
        self.assertEqual(result, "failed")
        self.assertEqual((calls["filestores"], calls["tmpdir"]), ([], 0))


# ---------------------------------------------------------------------------------------------------------------
# Real PostgreSQL: a private cluster started for this module only.
# ---------------------------------------------------------------------------------------------------------------

_CLUSTER = {}


def setUpModule():
    try:
        initdb = infrastructure.get_pg_bin("initdb")
        pg_ctl = infrastructure.get_pg_bin("pg_ctl")
    except FileNotFoundError as e:
        raise unittest.SkipTest(f"no PostgreSQL server binaries: {e}")  # noqa: B904
    if os.geteuid() == 0:
        raise unittest.SkipTest("initdb refuses to run as root")
    root = tempfile.mkdtemp(prefix="hcl")
    data = os.path.join(root, "data")
    sock = os.path.join(root, "s")
    os.makedirs(sock)
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run([initdb, "-D", data, "-U", user, "--auth=trust", "-E", "UTF8", "--no-sync"],
                   check=True, capture_output=True)
    subprocess.run(
        [pg_ctl, "-D", data, "-w", "-l", os.path.join(root, "log"), "-o",
         f"-c listen_addresses='' -c unix_socket_directories='{sock}' -c fsync=off -c max_connections=30", "start"],
        check=True, capture_output=True,
    )
    _CLUSTER.update(root=root, data=data, sock=sock, user=user, pg_ctl=pg_ctl)


def tearDownModule():
    if _CLUSTER:
        subprocess.run([_CLUSTER["pg_ctl"], "-D", _CLUSTER["data"], "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(_CLUSTER["root"], ignore_errors=True)


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update(PGHOST=_CLUSTER["sock"], PGUSER=_CLUSTER["user"])
    return env


def _pg():
    return runner.PostgresAccess(infrastructure.get_pg_bin("psql"), env=_env())


def _make_db(pg, name, comment=None):
    assert pg.run(f'CREATE DATABASE "{name}";').returncode == 0
    if comment is not None:
        assert pg.run(f"COMMENT ON DATABASE \"{name}\" IS '{comment}';").returncode == 0


def _names(pg):
    return sorted(name for name, _ in pg.database_rows())


class PostgresAccessTests(unittest.TestCase):
    """Tests [@ANCHOR: test:PostgresAccess] against a real server."""

    def test_rows_comments_and_forced_drop_of_a_database_with_an_open_connection(self):
        pg = _pg()
        _make_db(pg, "pa_one", rc.database_marker(2, False))
        _make_db(pg, "pa_two")
        self.assertIn(("pa_one", rc.database_marker(2, False)), pg.database_rows())
        self.assertEqual(pg.database_comment("pa_one"), rc.database_marker(2, False))
        self.assertEqual(pg.database_comment("pa_two"), "")
        self.assertIsNone(pg.database_comment("pa_missing"))
        self.assertIsNone(pg.database_comment("not a name"))
        # an open session (what a killed Odoo leaves) does not stop WITH (FORCE)
        holder = subprocess.Popen(
            [infrastructure.get_pg_bin("psql"), "-X", "-q", "-At", "pa_one", "-c", "select pg_sleep(60)"], env=_env()
        )
        try:
            time.sleep(0.5)
            self.assertTrue(pg.drop_database("pa_one"))
        finally:
            holder.kill()
            holder.wait()
        self.assertIsNone(pg.database_comment("pa_one"))
        self.assertTrue(pg.drop_database("pa_one"), "dropping a database that is already gone is fine")
        self.assertTrue(pg.drop_database("pa_two"))

    def test_the_protected_databases_cannot_be_dropped_through_it(self):
        pg = _pg()
        for name in ("postgres", "hams_prod", "template1"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                pg.drop_database(name)
        self.assertIn("postgres", _names(pg))

    def test_an_unreachable_server_is_none_and_false_not_an_exception(self):
        broken = runner.PostgresAccess(infrastructure.get_pg_bin("psql"), env=dict(_env(), PGHOST="/nonexistent"))
        self.assertIsNone(broken.database_rows())
        self.assertIsNone(broken.database_comment("anything"))
        self.assertFalse(broken.drop_database("anything_db"))


class RemoveDirectoryTreeTests(unittest.TestCase):
    """Tests [@ANCHOR: test:remove_directory_tree]."""

    def test_removes_a_tree_a_missing_path_counts_as_gone_a_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "slot-2")
            os.makedirs(os.path.join(tree, "a", "b"))
            with open(os.path.join(tree, "a", "b", "f"), "w") as handle:
                handle.write("x")
            self.assertTrue(runner.remove_directory_tree(tree))
            self.assertFalse(os.path.exists(tree))
            self.assertTrue(runner.remove_directory_tree(tree))
            target = os.path.join(tmp, "precious")
            os.makedirs(target)
            link = os.path.join(tmp, "slot-3")
            os.symlink(target, link)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertFalse(runner.remove_directory_tree(link))
            self.assertIn("symbolic link", out.getvalue())
            self.assertTrue(os.path.isdir(target))

    def test_a_permission_error_falls_back_to_sudo_rm_without_following_it_blindly(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "slot-4")
            os.makedirs(tree)
            calls = []

            real_rmtree = shutil.rmtree

            def fake_run(command, **kwargs):
                calls.append(command)
                real_rmtree(tree)
                return SimpleNamespace(returncode=0)

            with patch.object(runner.shutil, "rmtree", side_effect=PermissionError("denied")), \
                 patch.object(runner.subprocess, "run", side_effect=fake_run):
                self.assertTrue(runner.remove_directory_tree(tree))
            self.assertEqual(calls, [["sudo", "-n", "rm", "-rf", "--", tree]])


class SweepSlotLeftoversTests(unittest.TestCase):
    """Tests [@ANCHOR: test:sweep_slot_leftovers] with real databases, directories and flocks."""

    def setUp(self):
        self.pg = _pg()
        self.tmp = tempfile.mkdtemp(prefix="hsw")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock_base = os.path.join(self.tmp, "runner.lock")
        self.filestore = os.path.join(self.tmp, "filestore")
        self.tmpbase = os.path.join(self.tmp, "tmpbase")
        os.makedirs(self.filestore)
        os.makedirs(self.tmpbase)
        self.created = []
        self.held = []
        self.addCleanup(self._reset)

    def _reset(self):
        for handle in self.held:
            handle.close()
        for name, _ in self.pg.database_rows() or []:
            if name not in ("postgres", "template0", "template1"):
                self.pg.run(runner_cleanup_sql(name))

    def make(self, name, comment=None, filestore=True):
        _make_db(self.pg, name, comment)
        if filestore:
            os.makedirs(os.path.join(self.filestore, name))

    def hold(self, slot):
        path = self.lock_base if slot == 1 else f"{self.lock_base}.slot-{slot}"
        handle = open(path, "a")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.held.append(handle)

    def sweep(self, held_slot=2):
        said = []
        outcome = runner.sweep_slot_leftovers(
            self.pg, held_slot, self.lock_base, self.tmpbase, filestore_base=self.filestore, say=said.append
        )
        return outcome, said

    def test_removes_the_leftovers_of_dead_runs_and_nothing_else(self):
        self.make("hams_test_s3")                                          # older test.py leftover (no marker)
        self.make("custom_db", rc.database_marker(4, False))               # marked, any --db name
        self.make("hams_test_s5", rc.database_marker(5, True))             # --keep-db: stays
        self.make("hams_test")                                             # slot 1 / no-slots database: stays
        self.make("somebody_elses_data", "hello")                          # foreign: stays
        os.makedirs(os.path.join(self.filestore, "hams_test_s9"))          # orphan filestore of a dropped database
        os.makedirs(os.path.join(self.tmpbase, "slot-3", "x_chrome_odoo"))
        os.makedirs(os.path.join(self.tmpbase, "slot-1"))                  # never removed
        outcome, _ = self.sweep(held_slot=2)
        self.assertEqual(
            _names(self.pg),
            ["hams_test", "hams_test_s5", "postgres", "somebody_elses_data", "template0", "template1"],
        )
        self.assertEqual(sorted(os.listdir(self.filestore)), ["hams_test", "hams_test_s5", "somebody_elses_data"])
        self.assertEqual(sorted(os.listdir(self.tmpbase)), ["slot-1"])
        self.assertEqual(outcome["failed"], [])
        self.assertIn("database hams_test_s3", outcome["removed"])
        self.assertIn("database custom_db", outcome["removed"])
        self.assertIn("filestore hams_test_s9", outcome["removed"])

    def test_slot_directories_left_in_the_old_disk_tmpdir_are_swept_when_tmpdir_is_in_ram(self):
        """TMPDIR moved from /var/lib/odoo/tmp to /dev/shm/hams-test-tmp: a slot directory a run left on the disk before is removed too."""
        legacy = os.path.join(self.tmp, "legacy")
        os.makedirs(os.path.join(legacy, "slot-3", "y_chrome_odoo"))
        os.makedirs(os.path.join(self.tmpbase, "slot-4"))
        with unittest.mock.patch.dict(os.environ, {"HAMS_RAM_ROOT": self.tmp}), \
                unittest.mock.patch.object(runner, "DEFAULT_TEST_TMPDIR", legacy):
            outcome, _ = self.sweep(held_slot=2)
        self.assertEqual(os.listdir(legacy), [])
        self.assertEqual(os.listdir(self.tmpbase), [])
        self.assertIn("tmpdir slot-3", outcome["removed"])
        self.assertIn("tmpdir slot-4", outcome["removed"])

    def test_a_slot_a_live_run_holds_is_not_touched(self):
        self.make("hams_test_s3")
        self.make("hams_test_s4")
        os.makedirs(os.path.join(self.tmpbase, "slot-3"))
        self.hold(3)                                                       # a live run in slot 3
        outcome, said = self.sweep(held_slot=2)
        self.assertEqual(outcome["skipped"], ["slot 3"])
        self.assertIn("hams_test_s3", _names(self.pg))
        self.assertNotIn("hams_test_s4", _names(self.pg))
        self.assertTrue(os.path.isdir(os.path.join(self.filestore, "hams_test_s3")))
        self.assertTrue(os.path.isdir(os.path.join(self.tmpbase, "slot-3")))
        self.assertTrue(any("slot 3" in line for line in said))

    def test_a_live_holder_record_protects_a_slot_whose_flock_looks_free(self):
        self.make("hams_test_s3")
        qdir = runner.lock_queue.queue_dir_for_lock(self.lock_base)
        self.assertTrue(runner.lock_queue.write_holder(qdir, "test.py x", slot=3))     # this very process is alive
        outcome, _ = self.sweep(held_slot=2)
        self.assertEqual(outcome["skipped"], ["slot 3"])
        self.assertIn("hams_test_s3", _names(self.pg))

    def test_the_slot_this_run_holds_is_swept_too(self):
        self.make("hams_test_s2")
        self.hold(2)                                                       # the sweeper's own lock
        self.sweep(held_slot=2)
        self.assertNotIn("hams_test_s2", _names(self.pg))

    def test_hams_prod_on_the_server_refuses_the_whole_sweep(self):
        self.make("hams_prod")
        self.make("hams_test_s3")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.sweep()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("hams_test_s3", _names(self.pg))
        self.assertIn("hams_prod", _names(self.pg))

    def test_an_unreachable_server_warns_and_changes_nothing(self):
        broken = runner.PostgresAccess(infrastructure.get_pg_bin("psql"), env=dict(_env(), PGHOST="/nonexistent"))
        said = []
        self.assertIsNone(
            runner.sweep_slot_leftovers(broken, 2, self.lock_base, self.tmpbase, filestore_base=self.filestore,
                                        say=said.append)
        )
        self.assertIn("Sweep skipped", said[0])

    def test_nothing_to_sweep_is_quiet_and_creates_no_lock_files(self):
        self.make("somebody_elses_data", "hello")
        outcome, said = self.sweep()
        self.assertEqual(outcome, {"removed": [], "skipped": [], "failed": []})
        self.assertEqual(said, [])
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith("runner.lock")], [])


def runner_cleanup_sql(name):
    """Test-only: drops any database of the PRIVATE cluster, including ones the runner refuses (hams_prod)."""
    return f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);'


class RootPhaseDatabaseCleanupTests(unittest.TestCase):
    """Tests [@ANCHOR: test:root_phase_database_cleanup]: the backstop after a test process that was killed."""

    def setUp(self):
        self.pg = _pg()
        self.addCleanup(lambda: [self.pg.run(runner_cleanup_sql(n)) for n, _ in self.pg.database_rows()
                                 if n not in ("postgres", "template0", "template1")])

    def args(self, slots=2, db="rp_db", keep=False):
        return SimpleNamespace(slots=slots, db=db, keep_db=keep)

    def test_drops_the_marked_database_of_its_slot_after_the_test_process_is_gone(self):
        _make_db(self.pg, "rp_db_s2", rc.database_marker(2, False))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.root_phase_database_cleanup(self.args(), 2, self.pg), "dropped")
        self.assertNotIn("rp_db_s2", _names(self.pg))

    def test_leaves_kept_foreign_and_single_slot_databases_alone(self):
        _make_db(self.pg, "rp_db_s2", rc.database_marker(2, False))
        _make_db(self.pg, "rp_db", "")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(runner.root_phase_database_cleanup(self.args(keep=True), 2, self.pg))
            self.assertIsNone(runner.root_phase_database_cleanup(self.args(slots=1), 1, self.pg))
            self.assertIsNone(runner.root_phase_database_cleanup(self.args(slots=None), 1, self.pg))
            self.assertEqual(runner.root_phase_database_cleanup(self.args(slots=2, db="rp_db"), 1, self.pg), "foreign")
            self.assertEqual(runner.root_phase_database_cleanup(self.args(), 3, self.pg), "absent")
        self.assertIn("rp_db_s2", _names(self.pg))
        self.assertIn("rp_db", _names(self.pg))

    def test_a_database_name_too_long_for_a_slot_does_nothing(self):
        self.assertIsNone(runner.root_phase_database_cleanup(self.args(db="x" * 62), 2, self.pg))


_CHILD = textwrap.dedent(
    """
    import importlib.util, os, sys, time
    sys.path.insert(0, {here!r})
    spec = importlib.util.spec_from_file_location("hams_runner_child", {test_py!r})
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    import runner_cleanup
    database, slot, keep, tmpdir = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "keep", sys.argv[4]
    runner.install_run_cleanup(database, slot, keep, tmpdir)
    pg = runner.default_postgres_access()
    assert pg.run('DROP DATABASE IF EXISTS "%s" WITH (FORCE);' % database).returncode == 0
    assert pg.run('CREATE DATABASE "%s";' % database).returncode == 0
    assert pg.run(runner_cleanup.comment_database_sql(database, slot, keep)).returncode == 0
    os.makedirs(os.path.join(tmpdir, "x_chrome_odoo"), exist_ok=True)
    # the lock a run in this slot holds, so the next run's sweep can tell this process is alive
    import fcntl
    lock = open(sys.argv[5], "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # an open connection, as Odoo has
    import subprocess
    subprocess.Popen([runner.infrastructure.get_pg_bin("psql"), "-X", "-q", "-At", database, "-c", "select pg_sleep(120)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("READY", flush=True)
    mode = sys.argv[6]
    if mode == "exit":
        sys.exit(0)
    if mode == "raise":
        raise RuntimeError("the run crashed")
    time.sleep(120)
    """
)


class RealProcessTests(unittest.TestCase):
    """Tests [@ANCHOR: test:install_run_cleanup] and [@ANCHOR: test:run_cleanup_at_exit] with real processes that end
    in every way a run ends, against a real server: nothing may be left behind unless the process could not run
    its handlers (SIGKILL), and then the next run's sweep removes it."""

    def setUp(self):
        self.pg = _pg()
        self.tmp = tempfile.mkdtemp(prefix="hrp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.children = []
        self.addCleanup(self._kill_children)
        # the filestore of the child: HOME of the odoo account is replaced by a temporary one
        self.addCleanup(lambda: [self.pg.run(runner_cleanup_sql(n)) for n, _ in self.pg.database_rows()
                                 if n not in ("postgres", "template0", "template1")])

    def _kill_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait()

    def start(self, database, slot, keep, mode):
        tmpdir = os.path.join(self.tmp, f"slot-{slot}")
        os.makedirs(tmpdir, exist_ok=True)
        lock = os.path.join(self.tmp, f"lock.slot-{slot}")
        env = _env()
        env["PGUSER"] = _CLUSTER["user"]
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD.format(here=_HERE, test_py=_TEST_PY_PATH), database, str(slot),
             "keep" if keep else "drop", tmpdir, lock, mode],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.children.append(child)
        line = child.stdout.readline()
        self.assertEqual(line.strip(), "READY", child.stderr.read() if child.poll() is not None else line)
        return child, tmpdir

    def finish(self, child, sig=None):
        if sig:
            child.send_signal(sig)
        out, err = child.communicate(timeout=60)
        return out, err

    def test_normal_exit_failure_and_sigterm_all_leave_nothing_behind(self):
        for index, mode in enumerate(("exit", "raise", "term", "hup")):
            with self.subTest(mode=mode):
                database = f"rp_run_{mode}_s{index + 2}"
                child, tmpdir = self.start(database, index + 2, False, "exit" if mode == "exit" else
                                           "raise" if mode == "raise" else "sleep")
                self.assertIn(database, _names(self.pg))
                if mode in ("term", "hup"):
                    out, err = self.finish(child, signal.SIGTERM if mode == "term" else signal.SIGHUP)
                    self.assertEqual(child.returncode, 128 + (15 if mode == "term" else 1), err)
                    self.assertIn("ending the run and cleaning up", out)
                else:
                    out, err = self.finish(child)
                self.assertNotIn(database, _names(self.pg), err)
                self.assertFalse(os.path.exists(tmpdir), "the slot directory is removed")
                self.assertIn(f"Dropped database {database}", out)

    def test_keep_db_leaves_the_database_and_prints_its_name(self):
        child, tmpdir = self.start("rp_kept_s2", 2, True, "exit")
        out, _ = self.finish(child)
        self.assertIn("rp_kept_s2", _names(self.pg))
        self.assertIn("Database rp_kept_s2 (slot 2) KEPT", out)
        self.assertTrue(os.path.isdir(tmpdir))
        # the next run's sweep does not remove it either
        runner.sweep_slot_leftovers(self.pg, 3, os.path.join(self.tmp, "lock"), self.tmp,
                                    filestore_base=os.path.join(self.tmp, "none"), say=lambda _t: None)
        self.assertIn("rp_kept_s2", _names(self.pg))

    def test_two_concurrent_runs_leave_no_database_behind(self):
        first, dir_first = self.start("rp_conc_a_s2", 2, False, "sleep")
        second, dir_second = self.start("rp_conc_b_s3", 3, False, "sleep")
        self.assertTrue({"rp_conc_a_s2", "rp_conc_b_s3"} <= set(_names(self.pg)))
        # a third run starts while both are alive and sweeps: it must touch neither
        outcome = runner.sweep_slot_leftovers(
            self.pg, 4, os.path.join(self.tmp, "lock"), self.tmp, filestore_base=os.path.join(self.tmp, "none"),
            say=lambda _t: None,
        )
        self.assertEqual(sorted(outcome["skipped"]), ["slot 2", "slot 3"])
        self.assertTrue({"rp_conc_a_s2", "rp_conc_b_s3"} <= set(_names(self.pg)))
        first.send_signal(signal.SIGTERM)
        second.send_signal(signal.SIGTERM)
        first.communicate(timeout=60)
        second.communicate(timeout=60)
        self.assertEqual([n for n in _names(self.pg) if n.startswith("rp_conc")], [])

    def test_a_killed_run_leaves_its_files_and_the_next_runs_sweep_removes_them(self):
        child, tmpdir = self.start("rp_killed_s2", 2, False, "sleep")
        self.finish(child, signal.SIGKILL)
        self.assertIn("rp_killed_s2", _names(self.pg), "SIGKILL cannot be handled: the database is left")
        os.makedirs(os.path.join(self.tmp, "filestore", "rp_killed_s2"))
        said = []
        outcome = runner.sweep_slot_leftovers(
            self.pg, 3, os.path.join(self.tmp, "lock"), self.tmp, filestore_base=os.path.join(self.tmp, "filestore"),
            say=said.append,
        )
        self.assertNotIn("rp_killed_s2", _names(self.pg))
        self.assertFalse(os.path.exists(tmpdir))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "filestore", "rp_killed_s2")))
        self.assertIn("database rp_killed_s2", outcome["removed"])

    def test_a_forked_child_dies_by_the_signal_and_does_not_run_the_cleanup(self):
        # run_cmd ends its out-of-memory watchdog (a fork of the runner) with terminate(): it must die by SIGTERM as
        # it always did, not print the runner's message, exit(143) or drop the runner's database.
        script = textwrap.dedent("""
            import importlib.util, os, signal, sys, time
            spec = importlib.util.spec_from_file_location("hams_runner_fork", %r)
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)
            runner.install_run_cleanup("never_touched_s2", 2, False, None)
            pid = os.fork()
            if pid == 0:
                time.sleep(60)
                os._exit(0)
            time.sleep(0.5)
            os.kill(pid, signal.SIGTERM)
            _, status = os.waitpid(pid, 0)
            print("child signalled" if os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGTERM else "child status %%s" %% status, flush=True)
            os._exit(0)
        """ % _TEST_PY_PATH)
        done = subprocess.run([sys.executable, "-c", script], env=_env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(done.stdout.strip(), "child signalled", done.stdout + done.stderr)
        self.assertNotIn("Received SIGTERM", done.stdout)

    def test_the_handler_does_nothing_unless_armed_and_runs_once(self):
        with patch.object(runner, "_RUN_CLEANUP", None):
            self.assertIsNone(runner.run_cleanup_at_exit(self.pg))
        _make_db(self.pg, "rp_once_s2", rc.database_marker(2, False))
        state = {"database": "rp_once_s2", "slot": 2, "keep": False, "tmpdir": None}
        with patch.object(runner, "_RUN_CLEANUP", state), patch.object(runner, "_run_cleanup_done", False), \
             patch.object(runner, "_SIGNAL_HANDLER_PID", os.getpid()), \
             patch.object(runner, "kill_test_chrome"), \
             patch.object(runner, "remove_stale_filestore"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run_cleanup_at_exit(self.pg), "dropped")
            self.assertIsNone(runner.run_cleanup_at_exit(self.pg))
        self.assertNotIn("rp_once_s2", _names(self.pg))

    def test_a_failure_while_cleaning_up_is_a_warning_not_an_exception(self):
        state = {"database": "rp_warn_s2", "slot": 2, "keep": False, "tmpdir": None}
        with patch.object(runner, "_RUN_CLEANUP", state), patch.object(runner, "_run_cleanup_done", False), \
             patch.object(runner, "_SIGNAL_HANDLER_PID", os.getpid()), \
             patch.object(runner, "kill_test_chrome", side_effect=OSError("no pkill")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(runner.run_cleanup_at_exit(self.pg), "failed")
        self.assertIn("clean-up of rp_warn_s2 failed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
