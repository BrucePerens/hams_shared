#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests of the QSO-recordings backup (real kopia, filesystem repository).

Split out of test_b2_backup.py (2026-11-03): that one file took 240-355 s, and pytest's `--dist loadfile` keeps a
file on one worker, so it was the floor under every test chunk. Same tests, same helpers (imported from
test_b2_backup.py), now spread over files that run in parallel.
"""

import datetime
import os
import shutil
import subprocess

import b2_backup as bb
from test_b2_backup import PASSWORD, _Env, _load, _require


class QsoRecordingsBackupTests(_Env):
    """Tests [@ANCHOR: b2_backup:restore_recordings]: the Simulated Band recordings reach the repository without their
    in-progress *.tmp files, are kept forever, and a sample of the newest ones comes back with identical sha256."""

    @classmethod
    def setUpClass(cls):
        _require("kopia")

    NAMES = ["aaaa", "bbbb", "cccc"]

    def setUp(self):
        super().setUp()
        self.rec = self.t("qso_recordings")
        os.makedirs(self.rec)
        now = datetime.datetime.now().timestamp()
        for index, name in enumerate(self.NAMES):
            for ext, body in ((".opus", os.urandom(3000 + index)), (".json", b'{"n": %d}' % index)):
                path = os.path.join(self.rec, name + ext)
                with open(path, "wb") as f:
                    f.write(body)
                os.utime(path, (now - 3600 * (10 - index),) * 2)  # all finalised well before the snapshot
        with open(os.path.join(self.rec, "dddd.opus.tmp"), "wb") as f:  # in progress: must never be snapshotted
            f.write(b"partial")
        self.cfg_path = self.config(
            paths=[{"name": "qso_recordings", "path": self.rec, "optional": True, "exclude": ["*.tmp"], "keep_forever": True}],
            restore_test={"sample_files": 5, "verify_percent": 100, "database": False, "recordings_files": 4})
        self.cfg = bb.load_config(self.cfg_path)
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 0, out + err)

    def kopia(self, *args):
        env = bb.kopia_env(self.cfg, {"KOPIA_PASSWORD": PASSWORD})
        return subprocess.run(["kopia"] + list(args), env=env, capture_output=True, text=True, check=True).stdout

    def test_in_progress_files_are_not_snapshotted(self):
        listing = self.kopia("ls", "-r", bb.latest_snapshot(self.cfg, bb.kopia_env(self.cfg, {"KOPIA_PASSWORD": PASSWORD}),
                                                            bb.build_sources(self.cfg)[0], bb.Runner())["rootEntry"]["obj"])
        self.assertIn("aaaa.opus", listing)
        self.assertIn("aaaa.json", listing)
        self.assertNotIn(".tmp", listing)

    def test_policy_keeps_every_snapshot_forever(self):
        argv = bb.keep_forever_policy_argv(self.cfg, self.rec)
        for flag in ("--keep-latest", "--keep-hourly", "--keep-daily", "--keep-weekly", "--keep-monthly", "--keep-annual"):
            self.assertIn(f"{flag}={bb.KEEP_EVERYTHING}", argv)
        self.assertFalse([a for a in argv if "delete" in a or "expire" in a or "prune" in a])
        shown = self.kopia("policy", "show", self.rec)
        self.assertIn(str(bb.KEEP_EVERYTHING), shown)
        self.assertNotIn(" 14 ", shown.split("Retention:")[1].split("Scheduling")[0].replace("(defined for this target)", ""))

    def test_status_records_the_snapshot_time_for_the_readiness_check(self):
        entry = [r for r in _load(self.cfg.status_path)["sources"] if r["source"] == "qso_recordings"][0]
        self.assertTrue(entry["ok"])
        self.assertTrue(entry["snapshot_time"])

    def test_restore_test_compares_sha256_of_the_newest_recordings(self):
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.cfg.restore_status_path)["recordings"]
        self.assertEqual((report["sampled"], report["verified"], report["missing_live"], report["changed_since"]), (4, 4, 0, 0))
        self.assertFalse(os.path.exists(self.cfg.scratch_dir))
        for name in self.NAMES:  # no recording name reaches the output
            self.assertNotIn(name, out + err)

    def test_a_restored_recording_that_differs_from_the_live_file_fails(self):
        live = os.path.join(self.rec, "cccc.opus")  # the newest by mtime is aaaa? make cccc newest and altered
        past = os.path.getmtime(live)
        with open(live, "r+b") as f:
            f.write(b"X")
        os.utime(live, (past, past))
        os.utime(live, (past, past))
        newest = datetime.datetime.now().timestamp() - 600
        os.utime(live, (newest, newest))
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 1)
        self.assertIn("sha256 mismatch", err)

    def test_a_deleted_live_recording_is_counted_not_failed(self):
        os.remove(os.path.join(self.rec, "cccc.opus"))
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.cfg.restore_status_path)["recordings"]
        self.assertEqual(report["missing_live"], 1)
        self.assertEqual(report["verified"], 3)

    def test_an_empty_directory_is_a_clean_snapshot_and_restore_test(self):
        for name in os.listdir(self.rec):
            os.remove(os.path.join(self.rec, name))
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(_load(self.cfg.restore_status_path)["recordings"]["sampled"], 0)

    def test_absent_directory_is_skipped_like_any_optional_source(self):
        shutil.rmtree(self.rec)
        self.assertEqual(bb.build_sources(self.cfg), [])
