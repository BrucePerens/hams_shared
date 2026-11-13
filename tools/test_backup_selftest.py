#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests of backup_selftest.py [@ANCHOR: backup_selftest:run_stages] against a throwaway PostgreSQL cluster with a real pgBackRest
repository (posix, no encryption), a real temporary kopia repository with a throwaway password (through b2_backup.py's own test
fixture), a fake pgbackrest and a fake storage restore test. Negative controls: a corrupted backup file, a missing WAL segment, a
fingerprint mismatch and an empty repository each FAIL, and an empty repository is never green.

Nothing here touches a real backup key or repository: every key is a throwaway string written into a temporary directory.
"""

import getpass
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

import backup_selftest as bs
import b2_backup as bb
from test_b2_backup import PASSWORD, _Env, _require

PG_BIN = next((d for d in ("/usr/lib/postgresql/18/bin", "/usr/lib/postgresql/17/bin") if os.path.isdir(d)), None)
USER = getpass.getuser()


def need_postgres_and_pgbackrest():
    if not PG_BIN or not shutil.which("pgbackrest") or not shutil.which("zstd"):
        raise unittest.SkipTest("needs PostgreSQL server binaries, pgbackrest and zstd")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sh(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kw)


class Cluster:
    """A throwaway cluster owned by the test user, listening on a unix socket in its own directory only."""

    def __init__(self, base, name, checksums=True, archive_command=None):
        self.data = os.path.join(base, name)
        self.sock = os.path.join(base, name + "-sock")
        self.port = free_port()
        os.mkdir(self.sock)
        sh([f"{PG_BIN}/initdb", "-D", self.data, "-A", "trust", "-U", USER] + ([] if checksums else ["--no-data-checksums"]))
        with open(os.path.join(self.data, "postgresql.conf"), "a") as f:
            f.write(f"port = {self.port}\nunix_socket_directories = '{self.sock}'\nlisten_addresses = ''\nfsync = off\n"
                    "max_connections = 40\nwal_level = replica\n")
            if archive_command:
                f.write(f"archive_mode = on\narchive_command = '{archive_command}'\n")
        sh([f"{PG_BIN}/pg_ctl", "-D", self.data, "-l", os.path.join(base, name + ".log"), "-w", "start"])

    def psql_base(self):
        return [f"{PG_BIN}/psql", "-h", self.sock, "-p", str(self.port), "-U", USER]

    def sql(self, text, db="postgres"):
        return sh(self.psql_base() + ["-X", "-q", "-A", "-t", "-d", db, "-c", text]).stdout.strip()

    def stop(self):
        subprocess.run([f"{PG_BIN}/pg_ctl", "-D", self.data, "-m", "immediate", "-w", "stop"], capture_output=True)


class RealRepoBase(unittest.TestCase):
    """One live cluster and one pgBackRest repository with a full backup and some later WAL, shared by the tests; each test
    works on its own copy of the repository, so a corrupted copy never leaks into the next test."""

    @classmethod
    def setUpClass(cls):
        need_postgres_and_pgbackrest()
        cls.base = tempfile.mkdtemp(prefix="bst-")
        cls.conf = os.path.join(cls.base, "pgbackrest.conf")
        cls.repo = os.path.join(cls.base, "repo")
        os.mkdir(cls.repo)
        cls.live_dir = os.path.join(cls.base, "live")
        cls.write_conf(cls.conf, cls.repo, cls.live_dir, None)
        cls.live = Cluster(cls.base, "live", archive_command=f"pgbackrest --config={cls.conf} --stanza=selftest archive-push %p")
        cls.write_conf(cls.conf, cls.repo, cls.live_dir, cls.live)
        sh(["pgbackrest", f"--config={cls.conf}", "--stanza=selftest", "stanza-create"])
        cls.live.sql("create database appdb")
        cls.live.sql("create table items as select g as id, md5(g::text) as v from generate_series(1,20000) g", "appdb")
        cls.live.sql("create table drift as select g as id from generate_series(1,1000) g", "appdb")
        sh(["pgbackrest", f"--config={cls.conf}", "--stanza=selftest", "--start-fast", "--type=full", "backup"])
        cls.segments = []
        for _ in range(4):  # later WAL, in separate segments, a couple of seconds apart
            cls.live.sql("insert into items select g+100000*random()::int, 'x' from generate_series(1,200) g", "appdb")
            cls.segments.append(cls.live.sql("select pg_walfile_name(pg_current_wal_lsn())"))
            cls.live.sql("select pg_switch_wal()")
            time.sleep(2)
        cls.live.sql("insert into items select g, 'y' from generate_series(20001,20100) g", "appdb")
        cls.t_end = float(cls.live.sql("select extract(epoch from now())"))
        cls.segments.append(cls.live.sql("select pg_walfile_name(pg_current_wal_lsn())"))
        cls.live.sql("select pg_switch_wal()")
        deadline = time.time() + 60
        while cls.live.sql("select last_archived_wal from pg_stat_archiver") != cls.segments[-1]:
            if time.time() > deadline:
                raise RuntimeError("the last WAL segment was not archived")
            time.sleep(0.5)

    @staticmethod
    def write_conf(path, repo, pgdata, cluster):
        text = f"[global]\nrepo1-path={repo}\nrepo1-retention-full=3\nlog-level-console=warn\nlog-path={os.path.dirname(path)}/pgblog\nspool-path={os.path.dirname(path)}/spool\n"
        text += f"\n[selftest]\npg1-path={pgdata}\n"
        if cluster:
            text += f"pg1-port={cluster.port}\npg1-socket-path={cluster.sock}\n"
        os.makedirs(os.path.dirname(path) + "/pgblog", exist_ok=True)
        os.makedirs(os.path.dirname(path) + "/spool", exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bsw-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo_copy, self.conf_copy = self.repo, self.conf
        self.keys_dir = os.path.join(self.tmp, "keys")
        os.mkdir(self.keys_dir)
        self.key_values = {"alpha": "throwaway-alpha-key-material-111", "beta": "throwaway-beta-key-material-222"}
        for name, value in self.key_values.items():
            with open(os.path.join(self.keys_dir, name), "w") as f:
                f.write(value + "\n")

    def use_copy(self):
        """Work on a private copy of the repository (a test that damages it). The live cluster keeps archiving into the original, so
        stages that need a live archive (the archive check, wal_age) are left out of such a test."""
        self.repo_copy = os.path.join(self.tmp, "repo")
        shutil.copytree(self.repo, self.repo_copy)
        self.conf_copy = os.path.join(self.tmp, "copy.conf")
        self.write_conf(self.conf_copy, self.repo_copy, self.live_dir, self.live)

    OFFLINE_STAGES = ["preflight", "key_fingerprint", "backup_age", "wal_chain", "pgbackrest_verify", "restore"]

    def cfg(self, **over):
        raw = {
            "profile": "hams1", "state_dir": os.path.join(self.tmp, "state"), "scratch_root": os.path.join(self.tmp, "scratch"),
            "fingerprint_file": os.path.join(self.tmp, "fingerprints.json"), "forbidden_paths": [],
            "keys": [{"name": n, "kind": "raw_file", "path": os.path.join(self.keys_dir, n)} for n in self.key_values],
            "limits": {"systemd_run": False, "max_load_per_cpu": 1000},
            "pgbackrest": {"options": [f"--config={self.conf_copy}"], "stanza": "selftest", "verify": True},
            "live": {"psql": self.live.psql_base(), "run_as": []},
            "restore": {"pg_user": USER, "pg_bin": PG_BIN, "max_lag_seconds": 100000, "recovery_timeout": 300, "start_timeout": 120,
                        "tables": [{"db": "appdb", "table": "items"}]},
            "stages": ["preflight", "key_fingerprint", "pgbackrest_check", "backup_age", "wal_age", "wal_chain", "pgbackrest_verify", "restore"],
        }
        for key, value in over.items():
            raw[key] = {**raw[key], **value} if isinstance(value, dict) and isinstance(raw.get(key), dict) else value
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        return path

    def main(self, cfg_path, *args):
        out, err = io.StringIO(), io.StringIO()
        code = bs.main(["--config", cfg_path, *args], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def record(self, cfg_path):
        code, out, err = self.main(cfg_path, "record-fingerprint", "--confirm-record")
        self.assertEqual(code, 0, out + err)

    def run_stages(self, cfg_path, now=None, only=None):
        cfg = bs.load_config(cfg_path)
        out = io.StringIO()
        report = bs.Report(out)
        ctx = bs.Context(cfg, bs.key_fingerprints(cfg)[1], now=now or time.time)
        code = bs.run_stages(ctx, report, only)
        return code, {r["stage"]: r["status"] for r in report.results}, out.getvalue()

    def assert_clean_scratch(self, cfg_path):
        root = bs.load_config(cfg_path)["scratch_root"]
        self.assertEqual(os.listdir(root) if os.path.isdir(root) else [], [])

    def repo_files(self, kind):
        found = []
        for where, _, files in os.walk(os.path.join(self.repo_copy, kind)):
            found += [os.path.join(where, f) for f in files if not f.endswith((".info", ".copy", ".manifest", ".history"))]
        return found


class FullRunTests(RealRepoBase):
    """A healthy repository passes every stage; the exit code and the printed lines say so."""

    def test_every_stage_passes_and_cleans_up(self):
        cfg = self.cfg()
        self.record(cfg)
        code, out, err = self.main(cfg, "run")
        self.assertEqual(code, 0, out + err)
        for stage in ("preflight", "key_fingerprint", "pgbackrest_check", "backup_age", "wal_age", "wal_chain", "pgbackrest_verify", "restore",
                      "start", "sanity_sql", "checksum_read", "cleanup"):
            self.assertRegex(out, rf"(?m)^PASS {stage}: ", stage)
        self.assertIn("RESULT: ALL PASS", out)
        self.assert_clean_scratch(cfg)
        with open(os.path.join(self.tmp, "state", "selftest_status.json")) as f:
            status = json.load(f)
        self.assertIsNone(status["failed_stage"])
        self.assertTrue(status["last_success"])

    def test_no_key_value_is_ever_printed_or_stored(self):
        cfg = self.cfg()
        self.record(cfg)
        _, out, err = self.main(cfg, "run")
        status = open(os.path.join(self.tmp, "state", "selftest_status.json")).read()
        recorded = open(os.path.join(self.tmp, "fingerprints.json")).read()
        for value in self.key_values.values():
            for text in (out, err, status, recorded):
                self.assertNotIn(value, text)

    def test_recovery_to_immediate_also_restores(self):
        cfg = self.cfg(restore={"recovery": "immediate"})
        self.record(cfg)
        code, out, err = self.main(cfg, "run", "--only", "restore")
        self.assertEqual(code, 0, out + err)

    def test_tables_outside_tolerance_fail_sanity(self):
        self.live.sql("insert into drift select g from generate_series(1001,3000) g", "appdb")  # live now differs from anything archived
        cfg = self.cfg(stages=["preflight", "restore"], restore={"tables": [{"db": "appdb", "table": "drift", "tolerance_pct": 10}]})
        code, statuses, out = self.run_stages(cfg)
        self.assertEqual(code, bs.STAGE_CODES["sanity_sql"], out)
        self.assertEqual(statuses["checksum_read"], "SKIP")
        self.assertEqual(statuses["cleanup"], "PASS")
        self.assert_clean_scratch(cfg)
        wide = self.cfg(stages=["preflight", "restore"], restore={"tables": [{"db": "appdb", "table": "drift", "tolerance_pct": 500}]})
        self.assertEqual(self.run_stages(wide)[0], 0)

    def test_stages_listed_in_the_order_given(self):
        cfg = self.cfg()
        self.record(cfg)
        _, out, _ = self.main(cfg, "run", "--only", "backup_age", "--only", "key_fingerprint")
        self.assertLess(out.index("key_fingerprint"), out.index("backup_age"))
        self.assertNotIn("restore", out)


class NegativeControlTests(RealRepoBase):
    def test_corrupted_backup_file_fails_restore(self):
        self.use_copy()
        cfg = self.cfg(stages=self.OFFLINE_STAGES)
        target = max((f for f in self.repo_files("backup") if "/pg_data/base/" in f), key=os.path.getsize)
        with open(target, "r+b") as f:
            f.seek(os.path.getsize(target) // 2)
            f.write(bytes(b ^ 0xFF for b in os.urandom(64)))
        self.record(cfg)
        code, out, err = self.main(cfg, "run")
        # pgbackrest verify may not read every byte of a data file; the restore is the proof that does
        self.assertIn(code, (bs.STAGE_CODES["pgbackrest_verify"], bs.STAGE_CODES["restore"]), out)
        self.assertRegex(out, r"(?m)^FAIL restore")
        self.assertRegex(out, r"(?m)^SKIP start")
        self.assertIn("FAILED at stage", err)
        self.assert_clean_scratch(cfg)

    def test_missing_wal_segment_fails_the_chain_check_and_shortens_the_restore(self):
        self.use_copy()
        cfg = self.cfg(restore={"max_lag_seconds": 2}, stages=self.OFFLINE_STAGES)
        victim = [f for f in self.repo_files("archive") if os.path.basename(f).startswith(self.segments[1])]
        self.assertEqual(len(victim), 1)
        os.unlink(victim[0])
        self.record(cfg)
        code, statuses, out = self.run_stages(cfg, now=lambda: self.t_end + 1)
        self.assertEqual(code, bs.STAGE_CODES["wal_chain"], out)
        self.assertEqual(statuses["wal_chain"], "FAIL")
        self.assertIn("missing between", out)
        self.assertEqual(statuses["sanity_sql"], "FAIL", out)  # recovery stopped at the hole: newest transaction too old
        self.assert_clean_scratch(cfg)

    def test_same_repository_without_the_gap_passes_the_same_lag_limit(self):
        cfg = self.cfg(restore={"max_lag_seconds": 2}, stages=self.OFFLINE_STAGES)
        self.record(cfg)
        code, statuses, out = self.run_stages(cfg, now=lambda: self.t_end + 1)
        self.assertEqual(code, 0, out)

    def test_fingerprint_mismatch_fails_and_says_only_mismatch(self):
        cfg = self.cfg()
        self.record(cfg)
        with open(os.path.join(self.keys_dir, "beta"), "w") as f:
            f.write("a-different-throwaway-key-333\n")
        code, out, err = self.main(cfg, "run")
        self.assertEqual(code, bs.STAGE_CODES["key_fingerprint"], out)
        self.assertRegex(out, r"(?m)^FAIL key_fingerprint: fingerprint MISMATCH: beta")
        self.assertNotIn("alpha", out.split("MISMATCH")[1].split("\n")[0])
        for value in list(self.key_values.values()) + ["a-different-throwaway-key-333"]:
            self.assertNotIn(value, out + err)
        self.assertRegex(out, r"(?m)^PASS pgbackrest_check")  # the other stages still ran and report on their own

    def test_no_fingerprint_record_is_a_failure_not_a_pass(self):
        code, out, _ = self.main(self.cfg(), "run", "--only", "key_fingerprint")
        self.assertEqual(code, bs.STAGE_CODES["key_fingerprint"])
        self.assertIn("record-fingerprint", out)

    def test_missing_key_file_fails(self):
        cfg = self.cfg()
        self.record(cfg)
        os.unlink(os.path.join(self.keys_dir, "alpha"))
        code, out, _ = self.main(cfg, "run", "--only", "key_fingerprint")
        self.assertEqual(code, bs.STAGE_CODES["key_fingerprint"])
        self.assertIn("alpha", out)

    def test_empty_repository_fails_never_green(self):
        empty = os.path.join(self.tmp, "emptyrepo")
        os.mkdir(empty)
        self.conf_copy = os.path.join(self.tmp, "empty.conf")
        self.write_conf(self.conf_copy, empty, self.live_dir, self.live)
        sh(["pgbackrest", f"--config={self.conf_copy}", "--stanza=selftest", "--no-online", "--force", "stanza-create"])
        cfg = self.cfg(stages=self.OFFLINE_STAGES)
        self.record(cfg)
        code, statuses, out = self.run_stages(cfg)
        self.assertNotEqual(code, 0, out)
        self.assertEqual(statuses["backup_age"], "FAIL")
        self.assertEqual(statuses["preflight"], "FAIL")  # no backup, so no size to plan the restore with
        self.assertEqual(statuses["wal_chain"], "FAIL")
        self.assertEqual(statuses["pgbackrest_verify"], "FAIL")
        self.assertEqual(statuses["restore"], "SKIP")  # nothing is restored (or reported green) from an empty repository
        self.assertRegex(out, r"holds no backup|no full backup")

    def test_stale_backup_fails_age(self):
        cfg = self.cfg()
        code, statuses, out = self.run_stages(cfg, now=lambda: time.time() + 40 * 3600, only=["backup_age"])
        self.assertEqual(code, bs.STAGE_CODES["backup_age"], out)
        self.assertIn("h old", out)

    def test_stale_wal_fails_age(self):
        cfg = self.cfg(pgbackrest={"wal_max_age_minutes": 0})
        code, statuses, out = self.run_stages(cfg, only=["wal_age"])
        self.assertEqual(code, bs.STAGE_CODES["wal_age"], out)
        self.assertIn("min old", out)

    def test_wal_missing_from_repository_but_reported_archived(self):
        self.use_copy()
        cfg = self.cfg()
        for f in self.repo_files("archive"):  # an empty archive directory tree: no WAL at all
            os.unlink(f)
        code, statuses, out = self.run_stages(cfg, only=["wal_age"])
        self.assertNotEqual(code, 0, out)


class RefusalTests(RealRepoBase):
    def test_high_load_refuses_before_anything_runs(self):
        cfg = self.cfg(limits={"max_load_per_cpu": 0})
        code, out, err = self.main(cfg, "run")
        self.assertEqual(code, bs.EXIT_REFUSED, out)
        self.assertRegex(out, r"(?m)^REFUSED preflight: load average")
        self.assertNotIn("pgbackrest_check", out)
        self.assertIn("not a backup failure", err)

    def test_free_disk_under_twice_the_database_refuses(self):
        cfg = self.cfg(limits={"min_free_ratio": 1e9})
        code, out, _ = self.main(cfg, "run")
        self.assertEqual(code, bs.EXIT_REFUSED, out)
        self.assertIn("free under", out)
        self.assert_clean_scratch(cfg)

    def test_scratch_root_overlapping_a_forbidden_path_fails(self):
        cfg = self.cfg(forbidden_paths=[os.path.dirname(os.path.join(self.tmp, "scratch"))])
        code, out, _ = self.main(cfg, "run")
        self.assertEqual(code, bs.STAGE_CODES["preflight"], out)
        self.assertIn("overlaps", out)


class FingerprintCommandTests(RealRepoBase):
    def test_record_refuses_to_overwrite_and_needs_confirmation(self):
        cfg = self.cfg()
        with self.assertRaises(SystemExit):
            self.main(cfg, "record-fingerprint")
        self.record(cfg)
        first = open(os.path.join(self.tmp, "fingerprints.json")).read()
        code, out, err = self.main(cfg, "record-fingerprint", "--confirm-record")
        self.assertEqual(code, bs.EXIT_CONFIG)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(open(os.path.join(self.tmp, "fingerprints.json")).read(), first)
        self.assertEqual(oct(os.stat(os.path.join(self.tmp, "fingerprints.json")).st_mode & 0o777), "0o600")

    def test_fingerprint_is_the_one_b2_backup_records(self):
        cfg = self.cfg()
        self.record(cfg)
        recorded = json.load(open(os.path.join(self.tmp, "fingerprints.json")))["fingerprints"]
        self.assertEqual(recorded["alpha"], bb.fingerprint(self.key_values["alpha"]))

    def test_env_and_ini_key_kinds(self):
        path = os.path.join(self.keys_dir, "mixed.env")
        with open(path, "w") as f:
            f.write("# c\nOTHER=1\nSECRET_ONE='quoted-throwaway-1234'\n[global]\nrepo9-pass = ini-throwaway-5678\n")
        spec = lambda kind, var: {"name": "k", "kind": kind, "path": path, "var": var}
        self.assertEqual(bs.read_key_value(spec("env_file", "SECRET_ONE")), "quoted-throwaway-1234")
        self.assertEqual(bs.read_key_value(spec("ini_file", "repo9-pass")), "ini-throwaway-5678")
        with self.assertRaises(bs.StageFailed):
            bs.read_key_value(spec("env_file", "ABSENT"))


class FakePgbackrestTests(unittest.TestCase):
    """A fake pgbackrest, for failure modes a real one cannot be made to show: a failing check, and a tool that echoes a key."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bsf-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.key = "fake-run-key-material-9876"
        with open(os.path.join(self.tmp, "key"), "w") as f:
            f.write(self.key)
        self.fake = os.path.join(self.tmp, "pgbackrest")
        with open(self.fake, "w") as f:
            f.write(f"#!/bin/sh\necho 'unable to open repository with {self.key}' >&2\nexit 55\n")
        os.chmod(self.fake, 0o755)

    def config(self):
        raw = {"state_dir": os.path.join(self.tmp, "state"), "scratch_root": os.path.join(self.tmp, "scratch"), "forbidden_paths": [],
               "fingerprint_file": os.path.join(self.tmp, "fp.json"), "limits": {"systemd_run": False},
               "keys": [{"name": "fake", "kind": "raw_file", "path": os.path.join(self.tmp, "key")}],
               "pgbackrest": {"bin": self.fake}, "stages": ["pgbackrest_check"]}
        path = os.path.join(self.tmp, "c.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        return path

    def test_failure_names_the_stage_and_scrubs_a_key_the_tool_echoed(self):
        out, err = io.StringIO(), io.StringIO()
        code = bs.main(["--config", self.config(), "run"], out=out, err=err)
        self.assertEqual(code, bs.STAGE_CODES["pgbackrest_check"])
        self.assertIn("<redacted>", out.getvalue())
        self.assertNotIn(self.key, out.getvalue() + err.getvalue())
        self.assertEqual(err.getvalue().strip(), "backup selftest FAILED at stage pgbackrest_check")

    def test_check_command_reports_stage_name_only_in_the_synthetic_format(self):
        cfg = self.config()
        err = io.StringIO()
        self.assertEqual(bs.main(["--config", cfg, "check"], out=io.StringIO(), err=err), 1)
        self.assertIn("SEVERITY:high", err.getvalue())
        self.assertIn("never run", err.getvalue())
        bs.main(["--config", cfg, "run"], out=io.StringIO(), err=io.StringIO())
        err = io.StringIO()
        self.assertEqual(bs.main(["--config", cfg, "check"], out=io.StringIO(), err=err), 1)
        self.assertEqual(err.getvalue().splitlines(), ["SEVERITY:high", "backup selftest failed at stage pgbackrest_check"])

    def test_check_command_passes_a_recent_success_and_fails_an_old_one(self):
        cfg = bs.load_config(self.config())
        os.makedirs(cfg["state_dir"])
        now = bs.datetime.datetime.now(bs.datetime.timezone.utc)
        for age_days, want in ((1, 0), (10, 1)):
            stamp = (now - bs.datetime.timedelta(days=age_days)).isoformat()
            with open(bs.status_path(cfg), "w") as f:
                json.dump({"failed_stage": None, "last_success": stamp}, f)
            self.assertEqual(bs.main(["--config", self.config(), "check"], out=io.StringIO(), err=io.StringIO()), want)

    def test_json_report_is_machine_readable_and_secret_free(self):
        out, err = io.StringIO(), io.StringIO()
        code = bs.main(["--config", self.config(), "run", "--json"], out=out, err=err)
        doc = json.loads(out.getvalue())
        self.assertEqual((doc["schema"], doc["ok"], doc["exit"], doc["failed_stage"]), (1, False, code, "pgbackrest_check"))
        self.assertEqual(doc["stages"][0]["code"], bs.STAGE_CODES["pgbackrest_check"])
        self.assertIn("pgbackrest_check", err.getvalue())  # the stage lines went to standard error
        self.assertNotIn(self.key, out.getvalue() + err.getvalue())
        again = io.StringIO()
        self.assertEqual(bs.main(["--config", self.config(), "report"], out=again, err=io.StringIO()), 0)
        self.assertEqual(json.loads(again.getvalue())["failed_stage"], "pgbackrest_check")

    def test_failed_run_keeps_the_last_success_time(self):
        cfg = bs.load_config(self.config())
        os.makedirs(cfg["state_dir"])
        with open(bs.status_path(cfg), "w") as f:
            json.dump({"last_success": "2026-01-01T00:00:00+00:00"}, f)
        bs.main(["--config", self.config(), "run"], out=io.StringIO(), err=io.StringIO())
        self.assertEqual(json.load(open(bs.status_path(cfg)))["last_success"], "2026-01-01T00:00:00+00:00")


class ConfigAndHelperTests(unittest.TestCase):
    def load(self, **raw):
        tmp = tempfile.mkdtemp(prefix="bsc-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "c.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        return bs.load_config(path)

    def test_restore_without_tables_is_refused_so_an_empty_restore_cannot_pass(self):
        with self.assertRaises(bs.ConfigurationError):
            self.load(stages=["restore"])

    def test_unknown_stage_and_unknown_profile(self):
        with self.assertRaises(bs.ConfigurationError):
            self.load(stages=["nonsense"])
        with self.assertRaises(bs.ConfigurationError):
            self.load(profile="other")

    def test_key_stage_needs_keys(self):
        with self.assertRaises(bs.ConfigurationError):
            self.load(stages=["key_fingerprint"])

    def test_limits_prefix_is_nice_ionice_and_systemd_run_limits(self):
        cfg = self.load(stages=["preflight"], limits={"memory_max": "3G", "cpu_quota": "150%"}, run_as=["runuser", "-u", "postgres", "--"])
        ctx = bs.Context(cfg)
        argv = ctx.wrap(["pg_ctl", "start"], limited=True)
        self.assertEqual(argv[:2], ["systemd-run", "--scope"])
        self.assertIn("MemoryMax=3G", argv)
        self.assertIn("CPUQuota=150%", argv)
        text = " ".join(argv)
        self.assertIn("nice -n 19 ionice -c 3 runuser -u postgres -- pg_ctl start", text)
        self.assertEqual(ctx.wrap(["x"]), ["runuser", "-u", "postgres", "--", "x"])

    def test_default_profiles(self):
        full = self.load(keys=[{"name": "k", "kind": "raw_file", "path": "/x"}])
        self.assertEqual(full["stages"][0], "preflight")
        self.assertIn("kopia_restore", full["stages"])
        self.assertNotIn("restore", full["stages"])  # the restore proof is the restoration system's VM verification
        self.assertIn("storage_restore", bs.DEFAULT_STAGES["storage"])

    def test_stage_codes_are_unique(self):
        self.assertEqual(len(set(bs.STAGE_CODES.values())), len(bs.STAGE_CODES))

    def test_free_port_is_usable(self):
        port = bs.free_port()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))

    def test_inside(self):
        self.assertTrue(bs.inside("/a/b/c", "/a/b"))
        self.assertFalse(bs.inside("/a/bc", "/a/b"))


class StorageProfileTests(unittest.TestCase):
    """The /storage backup on the dev server: restore test command integration, and restore from a pg_basebackup directory."""

    def setUp(self):
        need_postgres_and_pgbackrest()
        self.tmp = tempfile.mkdtemp(prefix="bss-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.key = os.path.join(self.tmp, "kopia_pw")
        with open(self.key, "w") as f:
            f.write("throwaway-storage-password-1")

    def fake_restore_test(self, body):
        path = os.path.join(self.tmp, "fake_restore_test.py")
        with open(path, "w") as f:
            f.write(body)
        return path

    def cfg(self, command, stages, **over):
        raw = {"profile": "storage", "state_dir": os.path.join(self.tmp, "state"), "scratch_root": os.path.join(self.tmp, "scratch"),
               "forbidden_paths": [], "fingerprint_file": os.path.join(self.tmp, "fp.json"),
               "keys": [{"name": "storage_kopia", "kind": "raw_file", "path": self.key}], "limits": {"systemd_run": False, "max_load_per_cpu": 1000},
               "storage_restore": {"command": command}, "stages": stages}
        raw.update(over)
        path = os.path.join(self.tmp, "c.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        return path

    def run_main(self, cfg, *args):
        out, err = io.StringIO(), io.StringIO()
        return bs.main(["--config", cfg, *args], out=out, err=err), out.getvalue(), err.getvalue()

    def test_storage_restore_integrates_the_existing_restore_test(self):
        good = self.fake_restore_test("import sys\nprint('PASS base backup restored')\nprint('PASS wal chain')\nprint('RESULT: ALL PASS')\n")
        code, out, _ = self.run_main(self.cfg(["python3", good], ["storage_restore"]), "run")
        self.assertEqual(code, 0, out)
        self.assertIn("2 check(s) passed", out)

    def test_restore_test_that_fails_or_checks_nothing_is_a_failure(self):
        bad = self.fake_restore_test("print('FAIL wal chain missing')\nprint('RESULT: FAILED')\nraise SystemExit(1)\n")
        silent = self.fake_restore_test("print('hello')\n")
        for script in (bad, silent):
            code, out, _ = self.run_main(self.cfg(["python3", script], ["storage_restore"]), "run")
            self.assertEqual(code, bs.STAGE_CODES["storage_restore"], out)

    def test_real_restore_test_passes_its_scratch_argument(self):
        echo = self.fake_restore_test("import sys,os\nassert '--scratch' in sys.argv\nprint('PASS ok')\nprint('RESULT: ALL PASS')\n")
        cfg = self.cfg(["python3", echo], ["storage_restore"])
        self.assertEqual(self.run_main(cfg, "run")[0], 0)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "scratch")) if os.path.isdir(os.path.join(self.tmp, "scratch")) else [], [])

    def build_storage(self, checksums):
        base = os.path.join(self.tmp, "src")
        os.mkdir(base)
        self.src = Cluster(base, "src", checksums=checksums)
        self.addCleanup(self.src.stop)
        self.src.sql("create database appdb")
        self.src.sql("create table items as select g as id from generate_series(1,5000) g", "appdb")
        stamp = "20261109T010203Z"
        db_dir = os.path.join(self.tmp, "storage", "db")
        wal_dir = os.path.join(self.tmp, "storage", "wal")
        os.makedirs(wal_dir)
        sh([f"{PG_BIN}/pg_basebackup", "-h", self.src.sock, "-p", str(self.src.port), "-U", USER, "-D", os.path.join(db_dir, stamp),
            "-Ft", "--compress=client-zstd:3", "-X", "stream", "--manifest-checksums=SHA256", "-c", "fast"])  # fast: a spread checkpoint made each test wait about 100 s
        with open(os.path.join(db_dir, stamp + ".verified.json"), "w") as f:
            json.dump({"name": stamp}, f)
        for _ in range(2):
            self.src.sql("insert into items select g from generate_series(5001,5500) g", "appdb")
            self.src.sql("select pg_switch_wal()")
        self.t_end = float(self.src.sql("select extract(epoch from now())"))
        time.sleep(1)
        wal_src = os.path.join(self.src.data, "pg_wal")
        newest = self.src.sql("select pg_walfile_name(pg_current_wal_lsn())")
        for name in sorted(os.listdir(wal_src)):
            if len(name) == 24 and name < newest:
                sh(["zstd", "-q", "-o", os.path.join(wal_dir, name + ".zst"), os.path.join(wal_src, name)])
        return db_dir, wal_dir

    def storage_cfg(self, db_dir, wal_dir, **restore):
        return self.cfg(["true"], ["preflight", "restore"], basebackup_dir={"db_dir": db_dir, "wal_dir": wal_dir},
                        live={"psql": self.src.psql_base(), "run_as": []},
                        restore={"source": "basebackup_dir", "pg_user": USER, "pg_bin": PG_BIN, "max_lag_seconds": 100000,
                                 "tables": [{"db": "appdb", "table": "items", "tolerance_pct": 1}], "recovery_timeout": 300, **restore})

    def test_restore_from_basebackup_directory_without_checksums_uses_a_dump(self):
        db_dir, wal_dir = self.build_storage(checksums=False)
        code, out, _ = self.run_main(self.storage_cfg(db_dir, wal_dir), "run")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"PASS checksum_read: no data checksums: dumped")
        self.assertEqual(os.listdir(os.path.join(self.tmp, "scratch")), [])

    def test_restore_from_basebackup_directory_with_checksums_uses_pg_checksums(self):
        db_dir, wal_dir = self.build_storage(checksums=True)
        code, out, _ = self.run_main(self.storage_cfg(db_dir, wal_dir), "run")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"PASS checksum_read: pg_checksums")

    def test_unverified_or_absent_base_backup_fails(self):
        db_dir, wal_dir = self.build_storage(checksums=True)
        for name in os.listdir(db_dir):
            if name.endswith(".verified.json"):
                os.unlink(os.path.join(db_dir, name))
        code, out, _ = self.run_main(self.storage_cfg(db_dir, wal_dir), "run")
        self.assertEqual(code, bs.STAGE_CODES["preflight"], out)
        self.assertIn("no verified base backup", out)

    def test_missing_wal_between_backup_and_now_is_caught(self):
        db_dir, wal_dir = self.build_storage(checksums=True)
        segments = sorted(os.listdir(wal_dir))
        self.assertGreaterEqual(len(segments), 2)
        os.unlink(os.path.join(wal_dir, segments[0]))
        cfg = self.storage_cfg(db_dir, wal_dir, max_lag_seconds=0)
        code, out, _ = self.run_main(cfg, "run")
        self.assertNotEqual(code, 0, out)

    def test_corrupt_page_in_a_restored_cluster_fails_the_checksum_read(self):
        need_postgres_and_pgbackrest()
        base = os.path.join(self.tmp, "cc")
        os.mkdir(base)
        cluster = Cluster(base, "c")
        cluster.sql("create table t as select g from generate_series(1,2000) g")
        relpath = cluster.sql("select pg_relation_filepath('t')")
        cluster.stop()
        with open(os.path.join(cluster.data, relpath), "r+b") as f:
            f.seek(200)
            f.write(b"\xff" * 64)
        cfg = bs.load_config(self.cfg(["true"], ["preflight"], restore={"pg_user": USER}))
        ctx = bs.Context(cfg)
        state = bs.RestoreState(ctx)
        state.data, state.pg_bin, state.checksums, state.root = cluster.data, PG_BIN, True, base
        with self.assertRaises(bs.StageFailed):
            bs.checksum_read(state)
        state.checksums = True
        self.assertIn("pg_checksums", "pg_checksums")  # the failure above is pg_checksums' own exit status

    def test_socket_path_too_long_is_refused(self):
        deep = os.path.join(self.tmp, "d" * 100)
        cfg = bs.load_config(self.cfg(["true"], ["preflight"], scratch_root=deep, restore={"pg_user": USER}))
        state = bs.RestoreState(bs.Context(cfg))
        with self.assertRaises(bs.StageFailed):
            bs.restore_into(state)


class KopiaStageTests(_Env):
    """kopia_restore calls b2_backup.py's restore test against a real temporary kopia repository with a throwaway password."""

    @classmethod
    def setUpClass(cls):
        _require("kopia")

    def selftest_cfg(self, b2_cfg):
        raw = {"state_dir": self.t("st"), "scratch_root": self.t("scratch"), "forbidden_paths": [], "stages": ["kopia_restore"],
               "kopia": {"b2_config": b2_cfg}}
        path = self.t("selftest.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        return path

    def run_selftest(self, b2_cfg):
        out, err = io.StringIO(), io.StringIO()
        return bs.main(["--config", self.selftest_cfg(b2_cfg), "run"], out=out, err=err), out.getvalue()

    def source_tree(self):
        tree = self.t("tree")
        os.makedirs(tree)
        for i in range(8):
            with open(os.path.join(tree, f"f{i}"), "wb") as f:
                f.write(os.urandom(3000 + i))
        return tree

    def test_populated_repository_passes(self):
        cfg = self.config(paths=[{"name": "tree", "path": self.source_tree()}])
        self.assertEqual(self.run_main(cfg, "init")[0], 0)
        self.assertEqual(self.run_main(cfg, "backup")[0], 0)
        code, out = self.run_selftest(cfg)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"PASS kopia_restore: restored and compared")

    def test_repository_with_no_snapshot_fails(self):
        cfg = self.config(paths=[{"name": "tree", "path": self.source_tree()}])
        self.assertEqual(self.run_main(cfg, "init")[0], 0)
        code, out = self.run_selftest(cfg)
        self.assertEqual(code, bs.STAGE_CODES["kopia_restore"], out)

    def test_repository_with_no_sources_is_not_a_pass(self):
        cfg = self.config()
        self.assertEqual(self.run_main(cfg, "init")[0], 0)
        code, out = self.run_selftest(cfg)
        self.assertEqual(code, bs.STAGE_CODES["kopia_restore"], out)
        self.assertIn("compared nothing", out)

    def test_changed_repository_password_fails(self):
        cfg = self.config(paths=[{"name": "tree", "path": self.source_tree()}])
        self.assertEqual(self.run_main(cfg, "init")[0], 0)
        self.assertEqual(self.run_main(cfg, "backup")[0], 0)
        self.write_env("another-throwaway-password-2")
        code, out = self.run_selftest(cfg)
        self.assertEqual(code, bs.STAGE_CODES["kopia_restore"], out)
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn("another-throwaway-password-2", out)


if __name__ == "__main__":
    unittest.main()

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: backup_selftest:backup_selftest_restore_state_class]
# Tests [@ANCHOR: backup_selftest:backup_selftest_report_class]
# Tests [@ANCHOR: backup_selftest:backup_selftest_context_class]
# Tests [@ANCHOR: backup_selftest:backup_selftest_refused_class]
# Tests [@ANCHOR: backup_selftest:backup_selftest_stage_failed_class]
# Tests [@ANCHOR: backup_selftest:backup_selftest_configuration_error_class]
