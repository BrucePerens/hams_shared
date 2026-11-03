#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests of the tenant-archive off-box copy (real kopia, filesystem repository).

Split out of test_b2_backup.py (2026-11-03): that one file took 240-355 s, and pytest's `--dist loadfile` keeps a
file on one worker, so it was the floor under every test chunk. Same tests, same helpers (imported from
test_b2_backup.py), now spread over files that run in parallel.
"""

import datetime
import hashlib
import json
import os
import shutil

import b2_backup as bb
from test_b2_backup import _Env, _load, _require


class TenantArchiveOffBoxTests(_Env):
    """Tests [@ANCHOR: b2_backup:tenant_archive_freshness] and [@ANCHOR: b2_backup:restore_tenant_archives]:
    the tenant archives reach the (encrypted) repository, come back byte for byte, and a missing upload is flagged."""

    @classmethod
    def setUpClass(cls):
        _require("kopia")

    def make_set(self, tenant, stamp, payload=b"dump-bytes"):
        directory = self.t("archives", tenant, stamp)
        os.makedirs(directory)
        files = {}
        for name, body in (("db.dump", payload), ("filestore.tar.gz", b"tar-" + payload)):
            with open(os.path.join(directory, name), "wb") as f:
                f.write(body)
            files[name] = {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
        with open(os.path.join(directory, "MANIFEST.json"), "w") as f:
            json.dump({"tenant": tenant, "created": stamp, "files": files}, f)
        return directory

    def setUp(self):
        super().setUp()
        self.make_set("perens_com", "20261004T074141Z")
        self.make_set("parking", "20261004T074140Z", b"other")
        self.cfg_path = self.config(paths=[{"name": "tenant_archives", "path": self.t("archives"), "optional": True}])
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 0, out + err)
        self.cfg = bb.load_config(self.cfg_path)

    def entry(self):
        return [r for r in _load(self.cfg.status_path)["sources"] if r["source"] == "tenant_archives"][0]

    def test_backup_records_which_archive_set_it_uploaded(self):
        self.assertEqual(self.entry()["newest_archive_set"], "20261004T074141Z")
        self.assertTrue(self.entry()["ok"])

    def test_restore_downloads_the_copy_and_every_checksum_matches(self):
        dest = self.t("restored")
        code, out, err = self.run_main(self.cfg_path, "restore-tenant-archives", "--dest", dest)
        self.assertEqual(code, 0, out + err)
        self.assertIn("restored 2 archive sets (4 files, all checksums match), newest 20261004T074141Z", out)
        self.assertIn("--backup-root", out)
        with open(os.path.join(dest, "perens_com", "20261004T074141Z", "db.dump"), "rb") as f:
            self.assertEqual(f.read(), b"dump-bytes")
        # A destination that exists is refused, so a restore can never overwrite a live directory.
        code, _, err = self.run_main(self.cfg_path, "restore-tenant-archives", "--dest", dest)
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_a_damaged_copy_is_detected(self):
        sets, files, problems = bb.verify_archive_sets(self.t("archives"))
        self.assertEqual((sets, files, problems), (2, 4, []))
        with open(self.t("archives", "perens_com", "20261004T074141Z", "db.dump"), "ab") as f:
            f.write(b"x")
        os.remove(self.t("archives", "parking", "20261004T074140Z", "filestore.tar.gz"))
        _, _, problems = bb.verify_archive_sets(self.t("archives"))
        self.assertTrue(any("db.dump: checksum mismatch" in p for p in problems))
        self.assertTrue(any("filestore.tar.gz: missing" in p for p in problems))

    def test_weekly_restore_test_downloads_and_verifies_the_tenant_archives(self):
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.cfg.restore_status_path)
        self.assertEqual(report["tenant_archives"]["sets"], 2)
        self.assertFalse(os.path.exists(self.cfg.scratch_dir))

    def test_check_flags_an_archive_set_that_never_reached_the_repository(self):
        self.run_main(self.cfg_path, "restore-test")
        now = bb.now_utc()
        fresh = (now - datetime.timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
        self.make_set("perens_com", fresh)
        # Made two hours ago and not uploaded yet: the next nightly run will take it, no alert.
        self.assertEqual(bb.cmd_check(self.cfg, bb.Runner(), now=now), 0)
        # Still not uploaded 40 hours later while the rest of the backup stays green: that is the alert.
        old = (now - datetime.timedelta(hours=40)).strftime("%Y%m%dT%H%M%SZ")
        shutil.rmtree(self.t("archives", "perens_com", fresh))
        self.make_set("perens_com", old)
        with self.assertRaises(bb.BackupError) as ctx:
            bb.cmd_check(self.cfg, bb.Runner(), now=now)
        self.assertIn(f"tenant archive set {old} is 40 hours old and not off-box", str(ctx.exception))
        # After a real upload (a partial run is enough) the check is green again.
        code, out, err = self.run_main(self.cfg_path, "backup", "--only", "tenant_archives")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.entry()["newest_archive_set"], old)
        self.assertEqual(bb.cmd_check(self.cfg, bb.Runner(), now=now), 0)

    def test_check_flags_a_skipped_or_failed_tenant_archive_source(self):
        status = _load(self.cfg.status_path)
        status["sources"] = [r for r in status["sources"] if r["source"] != "tenant_archives"]
        bb.write_json(self.cfg.status_path, status)
        self.run_main(self.cfg_path, "restore-test")
        old = (bb.now_utc() - datetime.timedelta(hours=50)).strftime("%Y%m%dT%H%M%SZ")
        self.make_set("parking", old)
        with self.assertRaises(bb.BackupError) as ctx:
            bb.cmd_check(self.cfg, bb.Runner())
        self.assertIn("holds no good tenant_archives snapshot", str(ctx.exception))

    def test_no_archives_directory_is_not_an_alert(self):
        shutil.rmtree(self.t("archives"))
        self.assertEqual(bb.tenant_archive_problems(self.cfg, bb.Runner(), bb.now_utc()), [])

    def test_partial_backup_never_advances_the_nightly_freshness(self):
        before = _load(self.cfg.status_path)["finished"]
        self.run_main(self.cfg_path, "backup", "--only", "tenant_archives")
        self.assertEqual(_load(self.cfg.status_path)["finished"], before)
        code, _, err = self.run_main(self.cfg_path, "backup", "--only", "nope")
        self.assertEqual(code, 1)
        self.assertIn("names no configured source", err)
