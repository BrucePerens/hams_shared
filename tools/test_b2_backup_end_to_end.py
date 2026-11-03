#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
End-to-end tests of b2_backup.py against the real kopia binary and a real PostgreSQL database.

Split out of test_b2_backup.py (2026-11-03): that one file took 240-355 s, and pytest's `--dist loadfile` keeps a
file on one worker, so it was the floor under every test chunk. Same tests, same helpers (imported from
test_b2_backup.py), now spread over files that run in parallel.
"""

import datetime
import hashlib
import json
import os
import shutil
import subprocess

import b2_backup as bb
from test_b2_backup import PASSWORD, _Env, _load, _require


class EndToEndTests(_Env):
    """Tests [@ANCHOR: b2_backup:init], [@ANCHOR: b2_backup:backup], [@ANCHOR: b2_backup:restore_test],
    [@ANCHOR: b2_backup:check] and [@ANCHOR: b2_backup:password_fingerprint]
    with real kopia and real PostgreSQL."""

    @classmethod
    def setUpClass(cls):
        for binary in ("kopia", "pg_dump", "pg_restore", "psql", "createdb", "dropdb"):
            _require(binary)
        cls.database = f"b2bk_test_{os.getpid()}"
        subprocess.run(["createdb", cls.database], check=True)
        subprocess.run(["psql", "-q", "-d", cls.database, "-c",
                        "create table t(i int); insert into t select generate_series(1,100)"], check=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["dropdb", "--if-exists", cls.database], check=False)

    def setUp(self):
        super().setUp()
        # An Odoo-style content-addressed filestore: each file is named by the SHA-1 of its content.
        self.filestore = self.t("filestore")
        for i in range(12):
            body = os.urandom(2000 + i)
            digest = hashlib.sha1(body).hexdigest()  # burn-ignore-legacy-protocol-hash: Odoo filestore naming
            os.makedirs(os.path.join(self.filestore, digest[:2]), exist_ok=True)
            with open(os.path.join(self.filestore, digest[:2], digest), "wb") as f:
                f.write(body)
        # Odoo's checklist/ bookkeeping: SHA-1-looking names whose content is NOT that hash.
        for i in range(12):
            name = hashlib.sha1(b"attachment-%d" % i).hexdigest()  # burn-ignore-legacy-protocol-hash: Odoo naming
            os.makedirs(os.path.join(self.filestore, "checklist", name[:2]), exist_ok=True)
            with open(os.path.join(self.filestore, "checklist", name[:2], name), "wb") as f:
                f.write(b"not the hash of this")
        self.etc = self.t("etc")
        os.makedirs(os.path.join(self.etc, "keys"))
        # A symlink such as letsencrypt's live/privkey.pem -> ../archive/... (here a dangling one, as in a copy).
        os.symlink("../archive/privkey1.pem", os.path.join(self.etc, "keys", "privkey.pem"))
        with open(os.path.join(self.etc, "keys", "daemon.key"), "w") as f:
            f.write("daemon-key\n")
        with open(os.path.join(self.etc, "keys", "b2_backup.env"), "w") as f:
            f.write("KOPIA_PASSWORD=must-not-be-backed-up\n")
        self.cfg_path = self.config(
            databases=[self.database],
            paths=[{"name": "filestore_hams_prod", "path": self.filestore, "content_addressed": True},
                   {"name": "etc", "path": self.etc, "exclude": ["keys/b2_backup.env"]}],
            restore_test={"sample_files": 5, "verify_percent": 100, "database": True})

    def init_and_backup(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 0, out + err)
        return out

    def kopia(self, *args):
        cfg = bb.load_config(self.cfg_path)
        env = bb.kopia_env(cfg, {"KOPIA_PASSWORD": PASSWORD})
        return subprocess.run(["kopia"] + list(args), env=env, capture_output=True, text=True, check=True).stdout

    def test_full_cycle_backup_restore_test_and_check(self):
        out = self.init_and_backup()
        self.assertIn(f"ok   db_{self.database}", out)
        status = _load(self.t("state", "backup_status.json"))
        self.assertTrue(status["ok"])
        self.assertEqual({s["source"] for s in status["sources"]},
                         {"filestore_hams_prod", "etc", f"db_{self.database}"})
        # The password and B2 key file is excluded; the other key is there.
        cfg = bb.load_config(self.cfg_path)
        env = bb.kopia_env(cfg, {"KOPIA_PASSWORD": PASSWORD})
        listing = self.kopia("snapshot", "list", self.etc, "--json")
        root = json.loads(listing)[-1]["rootEntry"]["obj"]
        files = bb._relative_files(cfg, env, root, bb.Runner())
        self.assertIn("keys/daemon.key", files)
        self.assertNotIn("keys/b2_backup.env", files)
        # Nothing plaintext is left behind in staging, and the repository holds none of the content.
        self.assertFalse(os.path.exists(os.path.join(self.t("state"), "staging", f"db_{self.database}")))
        # Retention is applied.
        policy = self.kopia("policy", "show", "--global", "--json")
        self.assertEqual(json.loads(policy)["retention"]["keepDaily"], 14)
        self.assertEqual(json.loads(policy)["retention"]["keepMonthly"], 12)
        # Restore test restores real files and the database, then cleans up.
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.t("state", "restore_test_status.json"))
        self.assertTrue(report["ok"])
        self.assertEqual(report["database"]["database"], self.database)
        self.assertGreater(report["database"]["tables"], 0)
        self.assertTrue(all(s["verified"] > 0 for s in report["samples"]))
        self.assertFalse(os.path.exists(self.t("state", "restore-scratch")))
        scratch_dbs = subprocess.run(["psql", "-At", "-d", "postgres", "-c",
                                      f"select datname from pg_database where datname like 'b2restoretest_%{self.database}'"],
                                     capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(scratch_dbs, "")
        self.assertEqual(self.run_main(self.cfg_path, "check")[0], 0)

    def test_restore_test_samples_every_file_so_checklist_and_symlinks_cannot_fail_it(self):
        raw = _load(self.cfg_path)
        raw["restore_test"]["sample_files"] = 500
        with open(self.cfg_path, "w") as f:
            json.dump(raw, f)
        self.init_and_backup()
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.t("state", "restore_test_status.json"))
        self.assertTrue(report["ok"], report.get("problems"))
        by = {s["source"]: s for s in report["samples"]}
        self.assertEqual(by["filestore_hams_prod"]["sampled"], 12)

    def test_second_run_is_incremental_and_same_day_reruns_replace_rather_than_pile_up(self):
        self.init_and_backup()
        with open(os.path.join(self.filestore, "late"), "w") as f:
            f.write("new file")
        self.assertEqual(self.run_main(self.cfg_path, "backup")[0], 0)
        snaps = json.loads(self.kopia("snapshot", "list", self.filestore, "--json"))
        # Retention keeps one snapshot per day, so a rerun the same day supersedes the earlier one.
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[-1]["stats"]["nonCachedFiles"], 1, "only the new file is read")
        self.assertEqual(snaps[-1]["stats"]["cachedFiles"], 24, "unchanged files must not be re-read")
        self.assertEqual(snaps[-1]["rootEntry"]["summ"]["files"], 25)

    def test_changed_password_file_stops_the_run(self):
        self.init_and_backup()
        self.write_env("a-different-password")
        code, _, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 1)
        self.assertIn("differs from the one recorded at init", err)
        code, _, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 1)
        self.assertIn("differs", err)

    def test_init_cannot_run_twice(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        code, _, err = self.run_main(self.cfg_path, "init")
        self.assertEqual(code, 1)
        self.assertIn("already initialised", err)

    def test_backup_before_init_fails(self):
        code, _, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 1)
        self.assertIn("run `b2_backup.py init` first", err)

    def test_a_missing_source_fails_the_run_but_the_others_are_still_backed_up(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        shutil.rmtree(self.filestore)
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 1)
        self.assertIn("filestore_hams_prod", err)
        self.assertIn("ok   etc", out)
        status = _load(self.t("state", "backup_status.json"))
        self.assertFalse(status["ok"])

    def test_a_failed_dump_never_becomes_a_snapshot(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        cfg_path = self.config(databases=["no_such_database_here"])
        code, out, _ = self.run_main(cfg_path, "backup")
        self.assertEqual(code, 1)
        self.assertIn("FAIL db_no_such_database_here", out)
        cfg = bb.load_config(cfg_path)
        env = bb.kopia_env(cfg, {"KOPIA_PASSWORD": PASSWORD})
        sources = [s for s in bb.build_sources(cfg) if s.kind == "db"]
        self.assertEqual(bb.list_snapshots(cfg, env, sources[0], bb.Runner()), [])

    def test_restore_test_catches_a_filestore_file_whose_content_does_not_match_its_name(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        for name in [n for n in os.listdir(self.filestore) if n != "checklist"]:
            directory = os.path.join(self.filestore, name)
            for entry in os.listdir(directory):
                with open(os.path.join(directory, entry), "wb") as f:
                    f.write(b"silently corrupted")
        self.assertEqual(self.run_main(self.cfg_path, "backup")[0], 0)
        code, _, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 1)
        self.assertIn("wrong content", err)
        self.assertFalse(_load(self.t("state", "restore_test_status.json"))["ok"])

    def test_check_flags_a_stale_or_failed_backup(self):
        self.init_and_backup()
        self.run_main(self.cfg_path, "restore-test")
        cfg = bb.load_config(self.cfg_path)
        self.assertEqual(bb.cmd_check(cfg, bb.Runner()), 0)
        later = bb.now_utc() + datetime.timedelta(hours=40)
        with self.assertRaises(bb.BackupError) as ctx:
            bb.cmd_check(cfg, bb.Runner(), now=later)
        self.assertIn("backup is 40 hours old", str(ctx.exception))
        status = _load(cfg.status_path)
        status["ok"] = False
        bb.write_json(cfg.status_path, status)
        with self.assertRaises(bb.BackupError) as ctx:
            bb.cmd_check(cfg, bb.Runner())
        self.assertIn("last backup failed", str(ctx.exception))

    def test_check_before_any_run_fails(self):
        code, _, err = self.run_main(self.cfg_path, "check")
        self.assertEqual(code, 1)
        self.assertIn("no backup has ever been recorded", err)
